"""
Discord interface for the Mamazon Spade Agent.

Responsibilities (planning §4.2, workflow §1):
  • Slash commands with role gating (permission matrix in config.PERMISSIONS)
  • Heavy validation runs off the event loop with a live progress embed
  • Approval gate: blast drafts never reach #denda without a koor click
  • Auto-read #blast-tugas, ingest #bukti-tambahan uploads, answer #ai-chat
  • Mirror every action into #audit-log (Record principle)

Run:  python -m src.discord_bot
"""

from __future__ import annotations

import asyncio
import logging
import io
import re
from threading import Event
from contextlib import suppress
from datetime import datetime, timedelta
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from . import config
from .blast_reader import (
    BlastContextStore,
    ProcessedMessageCache,
    looks_like_blast,
    parse_blast_message,
)
from .bukti_tambahan import BuktiTambahanHandler
from .chat_agent import (
    ConversationStore, conversational_answer as chat_answer,
    check_explanations, fine_groups, friendly_date,
)

logger = logging.getLogger(__name__)
from .data_layer import DataLayer, append_angel_override
from .sheets_client import SheetAccessError, SheetSourceError
from .evidence_vault import EvidenceVault
from .models import (
    BlastEntry,
    BlastStatus,
    ResultStatus,
    ValidationBatch,
    ValidationResult,
)
from .output_generator import BlastStore, OutputGenerator
from .utils import (
    best_name_match,
    channel_mention,
    format_discord_message,
    format_rupiah,
    fmt_dt,
    name_similarity,
    parse_dt,
    truncate,
    wib_now,
)
from .validator import SheetAmbiguousError, SheetNotFoundError, Validator
from .agent import knowledge

COLOR_OK = discord.Color.from_str("#2ECC71")
COLOR_WARN = discord.Color.from_str("#F1C40F")
COLOR_BAD = discord.Color.from_str("#E74C3C")
COLOR_INFO = discord.Color.from_str("#3498DB")

STATUS_ICON = {
    ResultStatus.VERIFIED: "✅",
    ResultStatus.FLAGGED: "❌",
    ResultStatus.EXEMPT: "🎫",
    ResultStatus.PENDING_MANUAL_REVIEW: "⚠️",
}


def _chunks(text: str, limit: int = 1900) -> list[str]:
    """Split on line boundaries, hard-splitting any single line that is too long."""
    parts: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            parts.append(current)
            current = line
        else:
            current += line
    if current:
        parts.append(current)
    return parts


class Paginator(discord.ui.View):
    def __init__(self, pages: list[discord.Embed], author_id: int) -> None:
        super().__init__(timeout=300)
        self.pages = pages
        self.index = 0
        self.author_id = author_id
        self._sync()

    def _sync(self) -> None:
        self.prev.disabled = self.index == 0
        self.next.disabled = self.index >= len(self.pages) - 1
        self.counter.label = f"{self.index + 1}/{len(self.pages)}"

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                "Halaman ini milik pemanggil command.", ephemeral=True
            )
            return False
        return True

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def prev(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = max(0, self.index - 1)
        self._sync()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)

    @discord.ui.button(label="1/1", style=discord.ButtonStyle.secondary, disabled=True)
    async def counter(self, interaction: discord.Interaction, button: discord.ui.Button):
        pass

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.index = min(len(self.pages) - 1, self.index + 1)
        self._sync()
        await interaction.response.edit_message(embed=self.pages[self.index], view=self)


class BlastApprovalView(discord.ui.View):
    """
    Persistent Approve / Hold / Reject buttons.

    custom_id = blast:<blast_id>:<action> so the buttons keep working after a
    restart (re-registered in setup_hook from the blast store).
    """

    def __init__(self, bot: "MamazonBot", blast_id: str, disabled: bool = False) -> None:
        super().__init__(timeout=None)
        self.bot = bot
        self.blast_id = blast_id
        self.add_item(self._button("✅ Approve & Send", discord.ButtonStyle.success, "approve", disabled))
        self.add_item(self._button("⏸️ Hold", discord.ButtonStyle.secondary, "hold", disabled))
        self.add_item(self._button("❌ Reject", discord.ButtonStyle.danger, "reject", disabled))

    def _button(self, label: str, style: discord.ButtonStyle, action: str, disabled: bool):
        button = discord.ui.Button(
            label=label,
            style=style,
            custom_id=f"blast:{self.blast_id}:{action}",
            disabled=disabled,
        )

        async def callback(interaction: discord.Interaction) -> None:
            await self.bot.handle_blast_action(interaction, self.blast_id, action)

        button.callback = callback
        return button


class ConfirmView(discord.ui.View):
    def __init__(self, author_id: int) -> None:
        super().__init__(timeout=120)
        self.author_id = author_id
        self.value: bool | None = None

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return interaction.user.id == self.author_id

    @discord.ui.button(label="Confirm", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = True
        await interaction.response.defer()
        self.stop()

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.value = False
        await interaction.response.defer()
        self.stop()


class ChatActionView(discord.ui.View):
    """Confirm / Cancel gate for a planner-produced pending action.

    A mutating action is never dispatched automatically: the original requester
    must click Confirm and their roles are re-checked at click time against the
    action's permission. Buttons are stripped from the source message via
    ``interaction.message.edit(view=None)`` (NOT ``interaction.response``), so
    the interaction stays unacknowledged for the target command method to
    acknowledge itself.
    """

    def __init__(self, bot: "MamazonBot", action: dict, requester_id: int) -> None:
        super().__init__(timeout=180)
        self.bot = bot
        self.action = action
        self.requester_id = requester_id
        self._used = False
        self._expired = False

    async def on_timeout(self) -> None:
        self._expired = True

    async def _deny(self, interaction: discord.Interaction, reason: str) -> None:
        await interaction.response.send_message(reason, ephemeral=True)

    @staticmethod
    async def _disable(interaction: discord.Interaction) -> None:
        # Edit the source message, not the interaction response: this leaves
        # interaction.response free for the dispatched command to acknowledge.
        if interaction.message is not None:
            with suppress(Exception):
                await interaction.message.edit(view=None)

    @discord.ui.button(label="Konfirmasi", style=discord.ButtonStyle.danger)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self._used or self._expired:
            await self._deny(interaction, "Aksi ini sudah kedaluwarsa atau sudah diproses.")
            return
        if interaction.user.id != self.requester_id:
            await self._deny(interaction, "Hanya peminta aksi yang boleh mengonfirmasi.")
            return
        if not self.bot.has_permission(interaction.user, self.action.get("permission", "")):
            await self._deny(interaction, "Role Anda tidak berhak menjalankan aksi ini.")
            return
        self._used = True
        await self._disable(interaction)
        await self.bot.execute_chat_action(interaction, self.action)

    @discord.ui.button(label="Batal", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self._used or self._expired:
            await self._deny(interaction, "Aksi ini sudah kedaluwarsa atau sudah diproses.")
            return
        is_requester = interaction.user.id == self.requester_id
        if not is_requester and not self.bot.has_permission(interaction.user, "blast"):
            await self._deny(interaction, "Hanya peminta aksi atau koor/BPH yang boleh membatalkan.")
            return
        self._used = True
        await self._disable(interaction)
        await interaction.response.send_message(
            "Aksi dibatalkan; tidak ada data yang diubah.", ephemeral=True
        )


class SheetPicker(discord.ui.Select):
    def __init__(self, bot: "MamazonBot", options: list[str], force: bool, divisi: str = "") -> None:
        super().__init__(
            placeholder="Pilih sheet yang dimaksud…",
            options=[discord.SelectOption(label=option[:100], value=option) for option in options[:25]],
        )
        self.bot, self.force, self.divisi = bot, force, divisi

    async def callback(self, interaction: discord.Interaction) -> None:
        if not await self.bot.guard(interaction, "validate"):
            return
        await interaction.response.defer(thinking=True)
        await self.bot.validate_and_report(interaction, self.values[0], self.force, self.divisi)


class SheetPickerView(discord.ui.View):
    def __init__(self, bot: "MamazonBot", options: list[str], force: bool, author_id: int, divisi: str = "") -> None:
        super().__init__(timeout=180)
        self.author_id = author_id
        self.add_item(SheetPicker(bot, options, force, divisi))

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id == self.author_id:
            return True
        await interaction.response.send_message("Pilihan ini milik pemanggil command; gunakan `/validate` sendiri ya.", ephemeral=True)
        return False


class ValidationCancelView(discord.ui.View):
    def __init__(self, bot: "MamazonBot", run_key: str, run: dict) -> None:
        super().__init__(timeout=None)
        self.bot, self.run_key, self.run = bot, run_key, run

    @discord.ui.button(label="Batalkan validasi", style=discord.ButtonStyle.danger)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not await self.bot.guard(interaction, "validate"):
            return
        if interaction.user.id != self.run["author_id"] and not self.bot.has_permission(interaction.user, "blast"):
            await interaction.response.send_message("Hanya pemulai validasi atau koor/BPH yang boleh membatalkan.", ephemeral=True)
            return
        if self.bot._validation_runs.get(self.run_key) is not self.run:
            await interaction.response.send_message("Validasi ini sudah berakhir.", ephemeral=True)
            return
        if self.run["state"].get("stage") == "write":
            await interaction.response.send_message("Hasil sedang disimpan; sudah melewati titik pembatalan aman.", ephemeral=True)
            return
        self.run["cancel"].set()
        button.disabled = True
        await interaction.response.edit_message(view=self)
        await interaction.followup.send(
            "Pembatalan diterima. Menunggu panggilan API yang sedang berjalan selesai; "
            "hasil parsial tidak akan disimpan. Tugas tetap terkunci sampai worker berhenti.", ephemeral=True,
        )
        await self.bot.audit_mirror(f"Validasi {self.run['sheet']} diminta batal oleh {interaction.user}")


# ─── Bot ────────────────────────────────────────────────────────────────────
class MamazonBot(commands.Bot):
    def __init__(self) -> None:
        intents = discord.Intents.default()
        intents.message_content = True
        intents.members = True
        super().__init__(command_prefix="!", intents=intents)

        # The spreadsheet source may be a live Google Sheet. If it cannot be
        # opened (missing id, not shared), keep the process importable and let
        # main() refuse to start with a readable message instead of a traceback.
        self.startup_error = ""
        try:
            self.data = DataLayer()
        except Exception as exc:
            self.startup_error = f"{type(exc).__name__}: {exc}"
            from .sheets_client import XlsxSource

            self.data = DataLayer(source=XlsxSource(config.DUMMY_DATA_PATH))
        self.validator = Validator(data_layer=self.data)
        self.outputs = OutputGenerator()
        self.blasts = BlastStore()
        self.evidence = EvidenceVault(self.outputs)
        self.bukti = BuktiTambahanHandler(self.data)
        self.contexts = BlastContextStore()
        self.processed = ProcessedMessageCache()
        self._cleanup_task: asyncio.Task | None = None
        self._validation_runs: dict[str, dict] = {}
        self._conversations = ConversationStore()
        self._chat_busy: set[tuple] = set()
        self._blast_action_lock = asyncio.Lock()
        self._blast_sending: set[str] = set()
        from .voice_transcriber import VoiceTranscriber
        self.voice_transcriber = VoiceTranscriber()
        # Populated eagerly so /validate autocomplete works on the first keystroke.
        try:
            self._sheet_names: list[str] = self.data.submission_sheets()
        except Exception:
            self._sheet_names = []

    @property
    def rulebase(self) -> dict:
        return self.validator.rulebase

    async def close(self) -> None:
        try:
            from .mcp_client import get_mcp_client
            get_mcp_client().close()
        except Exception:
            pass
        await super().close()

    # ── lifecycle ──────────────────────────────────────────────────────────
    async def setup_hook(self) -> None:
        self._sheet_names = await asyncio.to_thread(self.data.submission_sheets)
        for record in self.blasts.list_all():
            if record.status in (BlastStatus.DRAFT, BlastStatus.HELD, BlastStatus.APPROVED):
                self.add_view(BlastApprovalView(self, record.blast_id))

        guild = (
            discord.Object(id=int(config.DISCORD_GUILD_ID))
            if config.DISCORD_GUILD_ID
            else None
        )
        try:
            if guild:
                self.tree.copy_global_to(guild=guild)
                synced = await self.tree.sync(guild=guild)
            else:
                synced = await self.tree.sync()
            print(f"[bot] {len(synced)} slash commands synced", flush=True)
        except Exception as exc:
            print(f"[bot] command sync failed: {exc}", flush=True)

    async def on_ready(self) -> None:
        print(
            f"[bot] logged in as {self.user} — {len(self.guilds)} guild(s): "
            + ", ".join(guild.name for guild in self.guilds),
            flush=True,
        )
        await self.audit_mirror(f"🤖 Bot online sebagai {self.user}")
        if self._cleanup_task is None or self._cleanup_task.done():
            self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    async def _cleanup_loop(self) -> None:
        """Image TTL sweeper — 24h retention, checked every 6 hours."""
        while True:
            try:
                await asyncio.sleep(6 * 3600)
                def clean_images():
                    with self.validator._adapter_lock:
                        return self.validator.images.cleanup()
                report = await asyncio.to_thread(clean_images)
                if report["removed"]:
                    print(
                        f"[cleanup] {report['removed']} image dihapus, "
                        f"{report['freed'] / 1024:.0f} KB dibebaskan"
                    )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                print(f"[cleanup] gagal: {exc}")

    # ── permissions ────────────────────────────────────────────────────────
    @staticmethod
    def member_roles(member: Any) -> set[str]:
        roles = getattr(member, "roles", None) or []
        return {role.name.strip().lower() for role in roles}

    def has_permission(self, member: Any, command: str) -> bool:
        if getattr(member, "guild_permissions", None) and member.guild_permissions.administrator:
            return True
        roles = self.member_roles(member)
        if config.ROLE_BPH.lower() in roles:
            return True
        allowed = config.PERMISSIONS.get(command, ())
        return any(role.lower() in roles for role in allowed)

    async def guard(self, interaction: discord.Interaction, command: str) -> bool:
        if self.has_permission(interaction.user, command):
            return True
        allowed = config.PERMISSIONS.get(command, ())
        pretty = ", ".join(f"@{role}" for role in allowed) or "role khusus"
        message = (
            f"🔒 Command `/{command}` hanya untuk {pretty}."
            if allowed
            else f"🔒 Command `/{command}` tidak tersedia untuk role Anda."
        )
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)
        self.outputs.audit(
            action="denied",
            actor=str(interaction.user),
            target=f"/{command}",
            result="DENIED",
            rationale="role tidak mencukupi",
        )
        await self.audit_mirror(f"🔒 {interaction.user} ditolak `/{command}`")
        return False

    # ── channels ───────────────────────────────────────────────────────────
    def channel(self, key: str) -> discord.abc.Messageable | None:
        _, channel_id = config.CHANNEL_ENV_MAP.get(key, ("", ""))
        if not channel_id:
            return None
        try:
            return self.get_channel(int(channel_id))
        except (TypeError, ValueError):
            return None

    async def audit_mirror(self, text: str) -> None:
        channel = self.channel("audit_log")
        if channel is None:
            return
        try:
            await channel.send(f"`{wib_now():%Y-%m-%d %H:%M:%S}` {truncate(text, 1800)}")
        except Exception:
            pass

    # ── /validate ──────────────────────────────────────────────────────────
    async def validate_cmd(
        self, interaction: discord.Interaction, sheet: str, force: bool, divisi: str = ""
    ) -> None:
        if not await self.guard(interaction, "validate"):
            return
        await interaction.response.defer(thinking=True)
        await self.validate_and_report(interaction, sheet, force, divisi)

    def validation_key(self, sheet: str) -> str:
        task_id = self.data.resolve_task(sheet).task_id
        return f"task:{task_id}" if task_id else f"sheet:{sheet.strip().casefold()}"

    async def validate_and_report(
        self, interaction: discord.Interaction, sheet: str, force: bool, divisi: str = ""
    ) -> None:
        if not await self.guard(interaction, "validate"):
            return
        # Resolve aliases before locking, so two spellings cannot start two runs.
        try:
            canonical, _ = await asyncio.to_thread(self.validator._resolve_sheet, sheet)
        except SheetNotFoundError as exc:
            await self._send_sheet_not_found(interaction, exc)
            return
        except SheetAmbiguousError as exc:
            await interaction.edit_original_response(
                embed=discord.Embed(
                    title="Sheet ambigu", description="Pilih sheet yang dimaksud:\n" + "\n".join(exc.options[:25]), color=COLOR_WARN,
                ),
                view=SheetPickerView(self, exc.options, force, interaction.user.id, divisi),
            )
            return
        except Exception as exc:
            await self._safe_edit(interaction, content=f"Tidak bisa membaca sheet: {exc}", view=None)
            return

        run_key = self.validation_key(canonical)
        # No await between checking and reserving: atomic on the Discord loop.
        if run_key in getattr(self, "_blast_sending", set()):
            await self._safe_edit(interaction, content="Blast tugas ini sedang dikirim. Tunggu pengiriman selesai sebelum revalidasi.", view=None)
            return
        if run_key in self._validation_runs:
            existing = self._validation_runs[run_key]
            link = existing.get("message")
            await self._safe_edit(
                interaction, content=f"**{canonical}** masih dalam proses validasi "
                f"(divisi: {existing['divisi'] or 'semua'}). Tunggu selesai atau batalkan run tersebut."
                + (f"\n[Proses berjalan]({link.jump_url})" if link else ""), view=None,
            )
            return
        state = {"stage": "resolve", "current": 0, "total": 0,
                 "message": f"{canonical} | divisi: {divisi or 'semua'}"}
        run = {"cancel": Event(), "state": state, "author_id": interaction.user.id,
               "sheet": canonical, "divisi": divisi, "message": None}
        self._validation_runs[run_key] = run
        view = ValidationCancelView(self, run_key, run)
        stop = asyncio.Event()
        pump = None
        worker = None

        def progress(stage: str, current: int, total: int, message: str) -> None:
            state.update(stage=stage, current=current, total=total, message=message)

        try:
            original = await interaction.edit_original_response(embed=self._progress_embed(state), view=view)
            channel = getattr(interaction, "channel", None)
            run["message"] = channel.get_partial_message(original.id) if hasattr(channel, "get_partial_message") else original
            pump = asyncio.create_task(self._pump_progress(run, view, stop))
            worker = asyncio.create_task(asyncio.to_thread(
                self.validator.validate, canonical, force, None, progress, str(interaction.user),
                divisi=divisi, cancel_event=run["cancel"], resolved_sheet=canonical,
            ))
            # Shield ensures cancellation of the command cannot abandon a live thread.
            batch = await asyncio.shield(worker)
            stop.set()
            if pump:
                await pump
            if run["message"]:
                with suppress(discord.HTTPException):
                    await run["message"].edit(view=None)
            if batch.cancelled:
                await self._safe_edit(
                    interaction, embed=discord.Embed(
                        title="Validasi dibatalkan", description=f"{canonical} · divisi: {divisi or 'semua'}\n"
                        "Hasil parsial dibuang. Laporan, denda, dan draft blast sebelumnya tidak berubah.", color=COLOR_WARN,
                    ), view=None,
                )
                await self.audit_mirror(f"Validasi {canonical} dibatalkan; tidak ada hasil parsial disimpan.")
            else:
                await self._report_batch(interaction, batch)
        except asyncio.CancelledError:
            run["cancel"].set()
            if worker:
                with suppress(Exception):
                    await asyncio.shield(worker)
            raise
        except Exception as exc:
            stop.set()
            if pump:
                with suppress(Exception):
                    await pump
            await self._safe_edit(interaction, embed=discord.Embed(
                title="Validasi gagal", description=truncate(f"{type(exc).__name__}: {exc}", 3500), color=COLOR_BAD,
            ), view=None)
        finally:
            stop.set()
            if pump:
                with suppress(Exception):
                    await pump
            if run["message"]:
                with suppress(discord.HTTPException):
                    await run["message"].edit(view=None)
            view.stop()
            if self._validation_runs.get(run_key) is run:
                del self._validation_runs[run_key]

    async def _pump_progress(self, run: dict, view: ValidationCancelView, stop: asyncio.Event) -> None:
        while not stop.is_set():
            try:
                await asyncio.wait_for(stop.wait(), timeout=2.5)
                return
            except asyncio.TimeoutError:
                pass
            try:
                # Regular message edit: survives the 15-minute interaction window.
                state = dict(run["state"])
                if run["cancel"].is_set():
                    state["message"] = "Pembatalan diminta; menunggu API selesai. Hasil parsial akan dibuang."
                view.cancel.disabled = run["cancel"].is_set() or state.get("stage") == "write"
                await run["message"].edit(embed=self._progress_embed(state), view=view)
            except discord.HTTPException:
                return

    @staticmethod
    def _progress_embed(state: dict) -> discord.Embed:
        total = state.get("total") or 0
        current = state.get("current") or 0
        ratio = (current / total) if total else 0
        filled = int(ratio * 12)
        bar = "█" * filled + "░" * (12 - filled)
        embed = discord.Embed(
            title="⏳ Memvalidasi…",
            description=f"`{bar}` {current}/{total}\n{truncate(state.get('message', ''), 120)}",
            color=COLOR_INFO,
        )
        embed.set_footer(text=f"tahap: {state.get('stage', '-')}")
        return embed

    async def _send_sheet_not_found(
        self, interaction: discord.Interaction, exc: SheetNotFoundError
    ) -> None:
        listing = "\n".join(f"• {option}" for option in exc.options[:15])
        await interaction.edit_original_response(
            embed=discord.Embed(
                title="❌ Sheet tidak ditemukan",
                description=(
                    f"Sheet **{exc.query}** tidak ada di workbook.\n\n"
                    f"Sheet tersedia:\n{listing}\n\n"
                    "Gunakan `/list-sheets` untuk daftar lengkap."
                ),
                color=COLOR_BAD,
            )
        )

    @staticmethod
    async def _safe_edit(interaction: discord.Interaction, **kwargs) -> None:
        """
        Edit the deferred response, or post to the channel if the interaction
        token expired (a slow model can exceed the 15-minute window).
        """
        try:
            await interaction.edit_original_response(**kwargs)
        except discord.HTTPException:
            channel = getattr(interaction, "channel", None)
            if channel is not None:
                payload = dict(kwargs)
                attachments = payload.pop("attachments", None)
                if attachments:
                    payload["files"] = [item for item in attachments if isinstance(item, discord.File)]
                await channel.send(**payload)

    async def _report_batch(
        self, interaction: discord.Interaction, batch: ValidationBatch
    ) -> None:
        summary = self.summary_embed(batch)
        channel_key = (
            "buzzer_validation" if batch.task_type == "BUZZER" else "pp_validation"
        )
        target = self.channel(channel_key)

        # Build the CSV once; discord.File objects cannot be reused after a send.
        payload: bytes | None = None
        filename = f"validasi_{re.sub(r'[^a-z0-9]+', '_', batch.sheet_name.lower())}.csv"
        if batch.total <= 300:
            payload = self.outputs.results_csv_bytes(batch.results)

        posted_to_channel = False
        if target is not None:
            try:
                await target.send(
                    embed=summary,
                    file=discord.File(io.BytesIO(payload), filename=filename)
                    if payload
                    else None,
                )
                posted_to_channel = True
            except Exception:
                posted_to_channel = False

        if posted_to_channel:
            await self._safe_edit(
                interaction,
                embed=discord.Embed(
                    title="✅ Validasi selesai",
                    description=(
                        f"{batch.sheet_name}: {batch.total} submissions, "
                        f"{len(batch.flagged)} denda, "
                        f"{format_rupiah(batch.total_denda)}\n\n"
                        f"Hasil diposting di {target.mention}. Detail per panitia "
                        "dikirim di bawah (hanya Anda yang bisa lihat)."
                    ),
                    color=COLOR_OK if not batch.flagged else COLOR_WARN,
                )
            )
            if payload:
                with suppress(discord.HTTPException):
                    await interaction.followup.send(
                        file=discord.File(io.BytesIO(payload), filename=filename),
                        ephemeral=True,
                    )
        else:
            await self._safe_edit(
                interaction,
                embed=summary,
                attachments=[discord.File(io.BytesIO(payload), filename=filename)]
                if payload
                else [],
            )

        pages = self.detail_embeds(batch)
        if pages:
            with suppress(discord.HTTPException):
                await interaction.followup.send(
                    embed=pages[0],
                    view=Paginator(pages, interaction.user.id) if len(pages) > 1 else discord.utils.MISSING,
                    ephemeral=True,
                )

        chatbot_report = self.format_validation_chatbot_report(batch, self.outputs)
        for chunk in _chunks(chatbot_report):
            with suppress(discord.HTTPException):
                await interaction.followup.send(chunk, ephemeral=False)

        await self._post_evidence(batch)
        await self.audit_mirror(
            f"🔎 {interaction.user} `/validate` {batch.sheet_name} → "
            f"{batch.total} checked, {len(batch.flagged)} flagged, "
            f"{format_rupiah(batch.total_denda)}"
        )

    @staticmethod
    def summary_embed(batch: ValidationBatch) -> discord.Embed:
        color = COLOR_OK
        if batch.pending:
            color = COLOR_WARN
        if batch.flagged:
            color = COLOR_BAD

        embed = discord.Embed(
            title=f"📋 Hasil Validasi: {batch.sheet_name}",
            description=(
                f"**{batch.total}** submissions · **{len(batch.verified)}** AMAN · "
                f"**{len(batch.flagged)}** DENDA · **{len(batch.exempt)}** EXEMPT"
            ),
            color=color,
        )
        embed.add_field(name="Task", value=batch.task_id or "-", inline=True)
        embed.add_field(name="Tipe", value=batch.task_type, inline=True)
        embed.add_field(name="Divisi", value=batch.divisi or "Semua", inline=True)
        embed.add_field(name="Total denda", value=format_rupiah(batch.total_denda), inline=True)
        embed.add_field(
            name="Ketentuan",
            value=(
                f"rulebase + blast `{batch.blast_message_id}`"
                if batch.blast_context_used
                else "rulebase only"
            ),
            inline=True,
        )
        embed.add_field(name="Review manual", value=str(len(batch.pending)), inline=True)
        embed.add_field(name="Skip (sudah valid)", value=str(batch.skipped_existing), inline=True)

        if batch.warnings:
            embed.add_field(
                name="⚠️ Warnings",
                value=truncate("\n".join(f"• {warning}" for warning in batch.warnings[:4]), 1000),
                inline=False,
            )
        embed.set_footer(
            text=f"Durasi {batch.duration_seconds}s · sumber: rules markdown + blast_context.json"
        )
        return embed

    @staticmethod
    def format_validation_chatbot_report(batch: ValidationBatch, outputs: OutputGenerator) -> str:
        """Format validation batch into a warm, natural Indonesian chatbot reply without markdown tables."""
        lines: list[str] = [
            f"Halo! Validasi untuk sheet **{batch.sheet_name}** ({batch.task_id or 'tugas'}) sudah selesai aku proses.",
            "Berikut rekap lengkapnya ya:",
            "",
            "**Anggota Lolos (Kualifikasi / AMAN / EXEMPT):**",
        ]

        qualified = [r for r in batch.results if r.status in (ResultStatus.VERIFIED, ResultStatus.EXEMPT)]
        if qualified:
            for res in qualified:
                sub = res.submission
                status_label = "AMAN" if res.status == ResultStatus.VERIFIED else f"EXEMPT (Angel Card: {res.angel_card_note or 'aktif'})"
                lines.append(f"• **{sub.nama}** ({sub.divisi or '-'}) — Status: {status_label}")
        else:
            lines.append("• Tidak ada anggota yang lolos pada batch ini.")

        lines.extend(["", "**Anggota Terkena Denda (Melanggar / FLAGGED):**"])
        if batch.flagged:
            for res in batch.flagged:
                sub = res.submission
                current_denda = res.total_denda
                total_accumulated = outputs.denda_total(sub.nama)
                lines.append(f"• **{sub.nama}** ({sub.divisi or '-'})")
                for violation in res.violations:
                    lines.append(
                        f"  › `{violation.check_id}` {violation.detail} "
                        f"(-{format_rupiah(violation.denda)})"
                    )
                lines.append(
                    f"  › Denda tugas ini: **{format_rupiah(current_denda)}** | "
                    f"Akumulasi total denda aktif: **{format_rupiah(total_accumulated)}**"
                )
        else:
            lines.append("• Keren banget! Tidak ada anggota yang terkena denda pada tugas ini.")

        lines.extend([
            "",
            f"Total diperiksa: **{batch.total}** anggota "
            f"({len(batch.verified)} AMAN, {len(batch.exempt)} EXEMPT, {len(batch.flagged)} DENDA).",
            f"Total denda tugas ini: **{format_rupiah(batch.total_denda)}**.",
            "Kalau kamu butuh bantuan atau ingin cek rincian lainnya, tinggal panggil aku ya!",
        ])
        return "\n".join(lines)

    @staticmethod
    def detail_embeds(batch: ValidationBatch, per_page: int = 4) -> list[discord.Embed]:
        interesting = [
            result
            for result in batch.results
            if result.status != ResultStatus.VERIFIED
        ]
        interesting.sort(key=lambda result: -result.total_denda)
        if not interesting:
            return []

        pages: list[discord.Embed] = []
        for start in range(0, len(interesting), per_page):
            embed = discord.Embed(
                title=f"Detail — {batch.sheet_name}",
                color=COLOR_INFO,
            )
            for result in interesting[start : start + per_page]:
                icon = STATUS_ICON.get(result.status, "•")
                lines = [
                    f"**{icon} {result.submission.nama}** — "
                    f"{result.submission.divisi or '-'} · "
                    f"{format_rupiah(result.total_denda)}"
                ]
                if result.status == ResultStatus.EXEMPT:
                    lines.append(f"🎫 {truncate(result.angel_card_note, 120)}")
                for check in result.violations:
                    lines.append(
                        f"› `{check.check_id}` {truncate(check.detail, 120)} "
                        f"(-{format_rupiah(check.denda)})"
                    )
                for check in result.checks:
                    if check.status == "INCONCLUSIVE":
                        lines.append(f"› `{check.check_id}` ⚠️ {truncate(check.detail, 100)}")
                if result.submission.links:
                    lines.append(f"› bukti: {result.submission.links[0]}")
                embed.add_field(
                    name=f"@{result.submission.ig_username or '-'}",
                    value=truncate("\n".join(lines), 1000),
                    inline=False,
                )
            pages.append(embed)
        return pages[:20]

    async def _post_evidence(self, batch: ValidationBatch) -> None:
        channel = self.channel("evidence")
        if channel is None:
            return
        entries = self.evidence.build(batch)
        if not entries:
            return
        for entry in entries[:10]:
            embed = discord.Embed(
                title="🔍 EVIDENCE LOG",
                color=COLOR_BAD,
            )
            embed.add_field(name="Panitia", value=f"{entry.nama}\n{entry.divisi}", inline=True)
            embed.add_field(name="Tugas", value=f"{entry.task_id}\n{batch.sheet_name}", inline=True)
            embed.add_field(name="Denda", value=format_rupiah(entry.total_denda), inline=True)
            embed.add_field(
                name="Pelanggaran",
                value=truncate(
                    "\n".join(
                        f"• `{violation['rule_id']}` {violation['detail']}"
                        for violation in entry.violations
                    ),
                    1000,
                ),
                inline=False,
            )
            if entry.evidence_links:
                embed.add_field(name="Bukti", value=entry.evidence_links[0], inline=False)
            if entry.recurring:
                embed.add_field(
                    name="⚠️ RECURRING",
                    value=f"{entry.history_count}x pelanggaran — perlu perhatian koor",
                    inline=False,
                )
            embed.set_footer(text=f"{wib_now():%Y-%m-%d %H:%M:%S} WIB · Evidence Vault")
            try:
                await channel.send(embed=embed)
            except Exception:
                break

        escalations = self.evidence.escalations(entries)
        if escalations:
            names = ", ".join(entry.nama for entry in escalations)
            try:
                await channel.send(
                    f"⚠️ **@{config.ROLE_KOOR}** — {names} telah melanggar "
                    f"{config.RECURRING_THRESHOLD}x. Disarankan tindakan lanjut."
                )
            except Exception:
                pass

    # ── /blast + approval gate ─────────────────────────────────────────────
    async def blast_cmd(self, interaction: discord.Interaction, sheet: str, divisi: str = "") -> None:
        if not await self.guard(interaction, "blast"):
            return
        await interaction.response.defer(thinking=True)

        resolution = await asyncio.to_thread(self.data.resolve_sheet, sheet)
        if resolution.not_found or resolution.ambiguous:
            await self._send_sheet_not_found(
                interaction, SheetNotFoundError(sheet, resolution.options())
            )
            return
        sheet_name = resolution.resolved
        if self.validation_key(sheet_name) in self._validation_runs:
            await interaction.edit_original_response(content="Tugas ini masih divalidasi. Tunggu selesai sebelum membuat blast.")
            return
        task = self.data.resolve_task(sheet_name)

        entries = self._entries_from_report(sheet_name, task.task_id)
        if divisi.strip():
            key = " ".join(divisi.casefold().split())
            available = sorted({row.get('divisi', '') for row in self.outputs.rows_for_sheet(sheet_name)} - {''})
            if key not in {" ".join(name.casefold().split()) for name in available}:
                await interaction.edit_original_response(content=f"Divisi '{divisi}' tidak ada dalam hasil validasi. Tersedia: {', '.join(available) or 'belum ada'}")
                return
            entries = [entry for entry in entries if " ".join(entry.divisi.casefold().split()) == key]
        if not entries:
            await interaction.edit_original_response(
                embed=discord.Embed(
                    title="✅ Tidak ada denda",
                    description=(
                        f"Tidak ada denda tercatat untuk **{sheet_name}** "
                        f"(divisi: {divisi or 'semua'}).\n"
                        "Belum ada item denda yang dapat di-blast; ini bukan konfirmasi semua panitia sudah divalidasi."
                    ),
                    color=COLOR_OK,
                )
            )
            return

        record = self.blasts.create(
            sheet_name=sheet_name,
            entries=entries,
            task_id=task.task_id,
            created_by=str(interaction.user),
        )

        embed = discord.Embed(
            title=f"📢 BLAST DRAFT — {sheet_name}",
            description=(
                f"**{record.count}** panitia · **{format_rupiah(record.total_denda)}** total\n"
                f"Divisi: {divisi or 'semua'}\n\n"
                "Draft belum dikirim. Butuh approval koor."
            ),
            color=COLOR_WARN,
        )
        embed.add_field(name="Blast ID", value=f"`{record.blast_id}`", inline=False)
        preview = "\n".join(
            f"• {entry.nama} ({entry.divisi}) — {format_rupiah(entry.total_denda)}"
            for entry in entries[:8]
        )
        embed.add_field(
            name="Preview",
            value=truncate(preview + ("\n…" if len(entries) > 8 else ""), 1000),
            inline=False,
        )

        channel = self.channel("blast_drafts")
        if channel is None:
            await interaction.edit_original_response(embed=embed)
            await interaction.followup.send(
                "⚠️ Channel #blast-drafts belum dikonfigurasi — draft disimpan lokal "
                f"di `{config.BLAST_STORE_DIR}`.",
                ephemeral=True,
            )
        else:
            message = await channel.send(embed=embed, view=BlastApprovalView(self, record.blast_id))
            record.message_id = str(message.id)
            record.channel_id = str(channel.id)
            self.blasts.save(record)
            await interaction.edit_original_response(
                embed=discord.Embed(
                    title="📢 Draft blast dibuat",
                    description=(
                        f"{record.count} panitia, {format_rupiah(record.total_denda)}\n"
                        f"Blast ID: `{record.blast_id}`\n\n"
                        f"Review di {channel.mention} lalu Approve/Hold/Reject."
                    ),
                    color=COLOR_WARN,
                )
            )

        self.outputs.audit(
            action="blast_draft",
            actor=str(interaction.user),
            target=f"{sheet_name} / {record.blast_id}",
            result=f"{record.count} entries, {record.total_denda}",
            rationale="approval gate — belum dikirim",
        )
        await self.audit_mirror(
            f"📢 {interaction.user} `/blast` {sheet_name} → {record.blast_id} "
            f"({record.count} panitia, {format_rupiah(record.total_denda)})"
        )

    def _entries_from_report(self, sheet_name: str, task_id: str | None) -> list[BlastEntry]:
        rows = self.outputs.rows_for_sheet(sheet_name)
        active = [item for item in self.outputs.denda_rows() if item.get('sheet_name', '').casefold() == sheet_name.casefold()
                  and item.get('status') == ResultStatus.FLAGGED]
        entries: list[BlastEntry] = []
        for row in rows:
            if row.get("status") != ResultStatus.FLAGGED:
                continue
            rule_ids = [part for part in (row.get("violations") or "").split(",") if part and part != "-"]
            person_checks = {item.get('rule_id'): item for item in active
                             if item.get('nama_komitee', '').casefold() == row.get('nama', '').casefold()}
            rule_ids = [rule_id for rule_id in rule_ids if rule_id in person_checks]
            if not rule_ids:
                continue
            violations = [
                {
                    "rule_id": rule_id,
                    "detail": person_checks.get(rule_id, {}).get('jenis_pelanggaran') or truncate(row.get("violation_detail", ""), 200),
                    "denda": int(float(person_checks[rule_id].get('denda') or 0)) if rule_id in person_checks else self.validator.engine.check_denda(rule_id),
                }
                for rule_id in rule_ids
            ]
            entries.append(
                BlastEntry(
                    nama=row.get("nama", ""),
                    divisi=row.get("divisi", ""),
                    ig_username=row.get("ig_username", ""),
                    total_denda=sum(item['denda'] for item in violations),
                    violations=violations,
                    evidence_links=[link for link in (row.get("evidence_links") or "").split(" | ") if link][:3],
                    recurring=self.outputs.recurring_count(row.get("nama", "")),
                )
            )
        entries.sort(key=lambda entry: (entry.divisi, -entry.total_denda))
        return entries

    async def handle_blast_action(
        self, interaction: discord.Interaction, blast_id: str, action: str
    ) -> None:
        if self._blast_action_lock.locked():
            await interaction.response.send_message("Ada aksi blast yang sedang diproses. Tunggu selesai, lalu coba lagi.", ephemeral=True)
            return
        async with self._blast_action_lock:
            await self._handle_blast_action(interaction, blast_id, action)

    async def _handle_blast_action(
        self, interaction: discord.Interaction, blast_id: str, action: str
    ) -> None:
        if not await self.guard(interaction, "blast"):
            return
        record = self.blasts.get(blast_id)
        if record is None:
            await interaction.response.send_message(
                f"❌ Blast `{blast_id}` tidak ditemukan.", ephemeral=True
            )
            return
        if record.status == BlastStatus.SENT:
            await interaction.response.send_message(
                "Blast ini sudah terkirim.", ephemeral=True
            )
            return

        run_key = self.validation_key(record.sheet_name)
        if action == "approve":
            if record.status == BlastStatus.REJECTED:
                await interaction.response.send_message("Draft ini sudah ditolak. Buat draft baru lewat `/blast`.", ephemeral=True)
                return
            if run_key in self._validation_runs:
                await interaction.response.send_message("Tugas sedang divalidasi. Tunggu hasil final sebelum menyetujui blast.", ephemeral=True)
                return
            current = self._entries_from_report(record.sheet_name, record.task_id)
            wanted = {entry.nama.casefold() for entry in record.entries}
            current = [entry for entry in current if entry.nama.casefold() in wanted]
            def signature(entries):
                return sorted((entry.nama.casefold(), entry.divisi.casefold(), entry.total_denda,
                               tuple(sorted((item['rule_id'], item['denda'], item['detail']) for item in entry.violations)),
                               tuple(entry.evidence_links)) for entry in entries)
            if signature(current) != signature(record.entries):
                await interaction.response.send_message("Draft ini tidak cocok dengan hasil validasi terbaru. Buat ulang `/blast` dan review lagi; draft lama tidak akan dikirim.", ephemeral=True)
                return
            self._blast_sending.add(run_key)
        actor = str(interaction.user)
        if action == "approve":
            try:
                await interaction.response.defer()
                record = self.blasts.approve(blast_id, actor) or record
                await self._send_blast_to_denda(record)
                self.blasts.mark_sent(blast_id)
                for entry in record.entries:
                    self.outputs.mark_blasted(entry.nama, record.task_id or "", record.sheet_name,
                                              [item['rule_id'] for item in entry.violations])
            except Exception as exc:
                self.outputs.audit(action="blast_send_failed", actor=actor, target=record.sheet_name,
                                   result="NOT_SENT", rationale=truncate(str(exc), 300))
                await interaction.followup.send(
                    f"Pengiriman belum selesai ({record.sent_chunks} bagian terkirim). Status tidak ditandai SENT. "
                    "Periksa #denda dan konfigurasi channel; Approve lagi melanjutkan bagian yang belum terkirim.", ephemeral=True,
                )
                return
            finally:
                self._blast_sending.discard(run_key)
            result = (
                f"✅ Blast sent! {record.count} panitia notified. "
                f"Total {format_rupiah(record.total_denda)}."
            )
            self.outputs.audit(
                action="blast_approved",
                actor=actor,
                target=record.sheet_name,
                result=result,
                rationale="koor approval via Discord button",
                extra={"blast_id": blast_id, "total": record.total_denda},
            )
        elif action == "hold":
            await interaction.response.defer()
            record = self.blasts.hold(blast_id, actor) or record
            result = (
                "⏸️ Blast ditahan. Release kapan saja dengan "
                f"`/release-blast blast_id:{blast_id}`."
            )
            self.outputs.audit(
                action="blast_held", actor=actor, target=record.sheet_name,
                result=result, rationale="koor hold",
            )
        else:
            await interaction.response.defer()
            record = self.blasts.reject(blast_id, actor) or record
            result = "❌ Blast dibatalkan. Draft tidak dikirim."
            self.outputs.audit(
                action="blast_rejected", actor=actor, target=record.sheet_name,
                result=result, rationale="koor reject",
            )

        embed = discord.Embed(
            title=f"📢 {record.sheet_name}",
            description=(
                f"**{record.count}** panitia · **{format_rupiah(record.total_denda)}**\n\n"
                f"Status: **{record.status}**\n{result}"
            ),
            color=COLOR_OK if action == "approve" else COLOR_WARN if action == "hold" else COLOR_BAD,
        )
        embed.add_field(name="Blast ID", value=f"`{blast_id}`", inline=False)
        with suppress(discord.HTTPException):
            await interaction.message.edit(
                embed=embed, view=BlastApprovalView(self, blast_id, disabled=True)
            )

        await interaction.followup.send(result, ephemeral=True)
        await self.audit_mirror(f"{result} — {record.sheet_name} oleh {actor}")

    async def _send_blast_to_denda(self, record) -> None:
        channel = self.channel("denda")
        lines = [
            f"📢 **BLAST DENDA — {record.sheet_name}**",
            "",
            "Berikut daftar denda:",
            "",
        ]
        for index, entry in enumerate(record.entries, start=1):
            lines.append(
                f"{index}. **{entry.nama}** ({entry.divisi}) — "
                f"{format_rupiah(entry.total_denda)}"
            )
            for violation in entry.violations:
                lines.append(
                    f"   - {violation['rule_id']}: {truncate(violation['detail'], 120)} "
                    f"(-{format_rupiah(violation['denda'])})"
                )
            if entry.evidence_links:
                lines.append(f"   - Bukti: {entry.evidence_links[0]}")
            if entry.recurring >= config.RECURRING_THRESHOLD:
                lines.append(f"   - ⚠️ riwayat {entry.recurring}x pelanggaran")
            lines.append("")

        lines.extend(
            [
                f"**Total: {record.count} panitia, {format_rupiah(record.total_denda)}**",
                "",
                "Transfer: BCA 4731763318 a.n. Bertrand Immanuel Wijaya",
                "Kode: 04 (Denda PP) / 05 (Denda Sponsor)",
                "Deadline bayar: Setiap hari Sabtu pukul 23.59 WIB",
            ]
        )

        if channel is None:
            raise RuntimeError("Channel #denda belum tersedia")
        chunks = _chunks("\n".join(lines))
        for index, chunk in enumerate(chunks):
            if index < record.sent_chunks:
                continue
            await channel.send(chunk, allowed_mentions=discord.AllowedMentions.none())
            record.sent_chunks = index + 1
            self.blasts.save(record)

    # ── read-only commands ─────────────────────────────────────────────────
    async def help_cmd(self, interaction: discord.Interaction) -> None:
        embed = discord.Embed(
            title="Mamazon siap bantu", color=COLOR_INFO,
            description="Aku bantu urusan SPADE: ketentuan tugas, bukti, validasi dan rincian denda. "
            "Berikut command yang tersedia untuk role kamu. Sapaan dan ngobrol ringan juga boleh di #ai-chat.",
        )
        visible = [command for command in self.tree.get_commands()
                   if command.name == "help" or self.has_permission(interaction.user, command.name)]
        for start in range(0, len(visible), 8):
            embed.add_field(
                name="Command" if start == 0 else "Command lainnya",
                value=truncate("\n".join(f"`/{cmd.name}` — {cmd.description}" for cmd in visible[start:start + 8]), 1024),
                inline=False,
            )
        if self.has_permission(interaction.user, "validate"):
            embed.add_field(name="Mulai validasi", value="`/validate sheet:PP 29 Oktober divisi:Spade (Fresh Money)`\n"
                "Kosongkan divisi untuk semua. Satu tugas hanya boleh satu proses; tombol Batalkan ada di pesan progress.", inline=False)
        if self.has_permission(interaction.user, "denda-list"):
            embed.add_field(name="Cek denda", value="`/denda-list` menampilkan tugas, alasan, rincian biaya dan akumulasi. "
                "Panitia hanya bisa melihat denda sendiri (user ID harus terhubung ke roster oleh koor).", inline=False)
        ai_status = config.ai_provider_status()
        embed.add_field(
            name="AI provider",
            value=(
                f"Vision & chat: **{ai_status['provider']}** "
                f"(model: `{ai_status['model'] or '-'}`).\n"
                f"Chat rewriting: **{'aktif' if ai_status['chat_enabled'] else 'nonaktif'}**. "
                "Satu pengaturan untuk pembacaan bukti dan percakapan."
            ),
            inline=False,
        )
        embed.add_field(
            name="AI chat — kemampuan & batasan",
            value=(
                "Baca data command (read-only), detail Google Sheets bila role mengizinkan, "
                "memori percakapan jangka pendek & panjang, kalkulasi aman, dan pencarian web opsional. "
                "Perintah yang mengubah data (validate, blast, add/remove-angel, scan/hold/release-blast) "
                "hanya jalan setelah konfirmasi eksplisit lewat tombol, dengan role gate dan audit log. "
                "Blast tetap butuh approval koor sebelum dikirim ke #denda. "
                "Bot TIDAK menjalankan kode sembarangan (no arbitrary code execution); "
                "aturan tidak bisa diubah lewat chat."
            ),
            inline=False,
        )
        if not self.has_permission(interaction.user, "validate"):
            embed.add_field(name="AI chat", value="Saat ini akses #ai-chat bot terbatas pada role validator SPADE/koor/BPH.", inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    def add_freshness_warning(self, embed: discord.Embed) -> None:
        warnings = getattr(self.data.source, "freshness_warnings", [])
        if warnings:
            embed.add_field(name="Data snapshot saat kuota habis", value=truncate("\n".join(warnings), 1000), inline=False)

    def task_link_view(self, merged, guild_id: int | None) -> discord.ui.View | None:
        guild = str(guild_id or config.DISCORD_GUILD_ID or "")
        channel_id = config.DISCORD_BLAST_TUGAS_CHANNEL_ID
        if not guild.isdigit() or not channel_id.isdigit():
            return None
        view = discord.ui.View(timeout=None)
        base = f"https://discord.com/channels/{guild}/{channel_id}"
        view.add_item(discord.ui.Button(label="Buka #blast-tugas", style=discord.ButtonStyle.link, url=base))
        message_id = merged.blast.blast_message_id if merged.blast else ""
        if str(message_id).isdigit():
            view.add_item(discord.ui.Button(label=f"Lihat blast {merged.task_id}"[:80], style=discord.ButtonStyle.link, url=f"{base}/{message_id}"))
        return view

    async def list_active_cmd(self, interaction: discord.Interaction) -> None:
        if not await self.guard(interaction, "list-active"):
            return
        overview = self.validator.task_overview()
        embed = discord.Embed(
            title="📋 Status Tugas PP & Buzzer",
            description=f"Per: {wib_now():%d %B %Y}",
            color=COLOR_INFO,
        )
        buckets: dict[str, list[str]] = {
            "SELESAI": [], "PERLU_TINDAKAN": [], "PENDING": [], "OVERDUE": [],
        }
        for item in overview:
            key = "OVERDUE" if item["overdue"] else item["state"]
            buckets.setdefault(key, []).append(
                f"`{item['task_id']}` {fmt_dt(item['deadline'], with_time=False)} "
                f"— {item['rows']} rows, {item['flagged']} flagged"
            )
        labels = {
            "OVERDUE": "❌ TERLEWAT",
            "PERLU_TINDAKAN": "⚠️ PERLU TINDAKAN",
            "PENDING": "⏳ PENDING",
            "SELESAI": "✅ SELESAI",
        }
        for key in ("OVERDUE", "PERLU_TINDAKAN", "PENDING", "SELESAI"):
            if buckets.get(key):
                embed.add_field(
                    name=labels[key],
                    value=truncate("\n".join(buckets[key][:10]), 1000),
                    inline=False,
                )
        if len(embed.fields) == 0:
            embed.description = "ℹ️ Tidak ada tugas aktif saat ini."
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def check_angel_cmd(self, interaction: discord.Interaction) -> None:
        if not await self.guard(interaction, "check-angel"):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        registry = await asyncio.to_thread(self.data.load_angel_registry)
        if not registry:
            await interaction.followup.send(
                "Tidak ada data Angel Card.", ephemeral=True
            )
            return
        pages: list[discord.Embed] = []
        for start in range(0, len(registry), 10):
            embed = discord.Embed(
                title=f"🎫 Angel Card Registry ({len(registry)} entri)",
                color=COLOR_INFO,
            )
            for entry in registry[start : start + 10]:
                embed.add_field(
                    name=entry.nama,
                    value=(
                        f"{entry.divisi}\n{entry.status} · "
                        f"{fmt_dt(entry.tanggal_beli, with_time=False)}"
                    ),
                    inline=True,
                )
            pages.append(embed)
        for page in pages:
            self.add_freshness_warning(page)
        await interaction.followup.send(
            embed=pages[0],
            view=Paginator(pages, interaction.user.id) if len(pages) > 1 else discord.utils.MISSING,
            ephemeral=True,
        )

    async def add_angel_cmd(
        self, interaction: discord.Interaction, nama: str, tanggal: str, divisi: str
    ) -> None:
        if not await self.guard(interaction, "add-angel"):
            return
        parsed = parse_dt(tanggal, default_time=(0, 0))
        if parsed is None:
            await interaction.response.send_message(
                f"❌ Tanggal '{tanggal}' tidak valid. Format: YYYY-MM-DD (contoh 2026-10-03)",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)
        entry, score = await asyncio.to_thread(self.data.find_person, nama)
        if entry is None:
            roster = await asyncio.to_thread(self.data.load_roster)
            suggestions = sorted(
                ((candidate.nama, name_similarity(nama, candidate.nama)) for candidate in roster),
                key=lambda pair: pair[1],
                reverse=True,
            )[:3]
            listing = "\n".join(f"{index}. {name} ({score:.2f})" for index, (name, score) in enumerate(suggestions, start=1))
            await interaction.followup.send(
                f"❌ Nama '{nama}' tidak ditemukan di roster.\n\nMungkin maksud Anda:\n{listing}",
                ephemeral=True,
            )
            return

        try:
            await asyncio.to_thread(
                self.data.add_angel_card,
                nama=entry.nama,
                divisi=divisi or entry.divisi,
                tanggal_beli=parsed,
                status="ACTIVE",
                catatan=f"ditambahkan via Discord oleh {interaction.user}",
            )
        except (SheetAccessError, SheetSourceError) as exc:
            await interaction.followup.send(
                f"❌ Gagal memperbarui spreadsheet:\n{exc}",
                ephemeral=True,
            )
            return

        self.outputs.audit(
            action="angel_added",
            actor=str(interaction.user),
            target=entry.nama,
            result=f"{parsed:%Y-%m-%d}",
            rationale=f"roster match score {score:.2f}",
        )
        await interaction.followup.send(
            embed=discord.Embed(
                title="✅ Angel Card ditambahkan",
                description=(
                    f"👤 {entry.nama}\n"
                    f"🏷️ {divisi or entry.divisi}\n"
                    f"📅 {parsed:%d %B %Y}\n"
                    "🎫 Status: ACTIVE\n\n"
                    "Berlaku untuk PP & Buzzer yang BELUM di-blast saat tanggal tersebut.\n"
                    "Tidak berlaku untuk Sponsor."
                ),
                color=COLOR_OK,
            ),
            ephemeral=True,
        )
        await self.audit_mirror(f"🎫 {interaction.user} `/add-angel` {entry.nama} ({parsed:%Y-%m-%d})")

    async def remove_angel_cmd(self, interaction: discord.Interaction, nama: str) -> None:
        if not await self.guard(interaction, "remove-angel"):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        registry = await asyncio.to_thread(self.data.load_angel_registry)
        match, score = best_name_match(nama, [entry.nama for entry in registry], 0.8)
        if match is None:
            await interaction.followup.send(
                f"❌ '{nama}' tidak punya Angel Card aktif.", ephemeral=True
            )
            return

        view = ConfirmView(interaction.user.id)
        await interaction.edit_original_response(
            content=f"⚠️ Konfirmasi: hapus Angel Card untuk **{match}**?", view=view,
        )
        await view.wait()
        if not view.value:
            await interaction.edit_original_response(
                content="Dibatalkan.", view=None
            )
            return

        try:
            ok = await asyncio.to_thread(
                self.data.remove_angel_card,
                nama=match,
                catatan=f"dihapus via Discord oleh {interaction.user}",
            )
            if not ok:
                await interaction.edit_original_response(
                    content=f"❌ '{match}' tidak ditemukan di sheet Angel Card Registry.",
                    view=None,
                )
                return
        except (SheetAccessError, SheetSourceError) as exc:
            await interaction.edit_original_response(
                content=f"❌ Gagal memperbarui spreadsheet:\n{exc}",
                view=None,
            )
            return

        self.outputs.audit(
            action="angel_removed",
            actor=str(interaction.user),
            target=match,
            result="removed",
            rationale="koor request",
        )
        await interaction.edit_original_response(
            content=(
                f"✅ Angel Card untuk **{match}** telah dihapus. "
                "Panitia ini tunduk pada semua PP/Buzzer submission."
            ),
            view=None,
        )
        await self.audit_mirror(f"🎫 {interaction.user} `/remove-angel` {match}")

    async def roster_set_discord_cmd(
        self, interaction: discord.Interaction, nama: str, discord_id: str
    ) -> None:
        if not await self.guard(interaction, "roster-set-discord"):
            return
        clean_id = re.sub(r"[<@!>]", "", str(discord_id)).strip()
        if not clean_id.isdigit():
            await interaction.response.send_message(
                f"❌ Discord User ID '{discord_id}' tidak valid. Harus berupa ID angka (contoh: 123456789012345678).",
                ephemeral=True,
            )
            return

        await interaction.response.defer(thinking=True, ephemeral=True)
        try:
            entry, old_id = await asyncio.to_thread(
                self.data.update_roster_discord, nama, clean_id
            )
        except Exception as exc:
            await interaction.followup.send(f"❌ Gagal memperbarui roster: {exc}", ephemeral=True)
            return

        action_desc = f"diperbarui: `{old_id}` → `{clean_id}`" if old_id else f"dihubungkan ke `{clean_id}`"
        self.outputs.audit(
            action="roster_discord_set",
            actor=str(interaction.user),
            target=entry.nama,
            result=clean_id,
            rationale=f"old_id={old_id or '-'}",
        )
        await interaction.followup.send(
            embed=discord.Embed(
                title="✅ Discord ID Roster Disimpan",
                description=(
                    f"👤 **{entry.nama}**\n"
                    f"🆔 Discord ID: <@{clean_id}> (`{clean_id}`)\n"
                    f"🏷️ Divisi: {entry.divisi or '-'}\n"
                    f"Status: {action_desc}"
                ),
                color=COLOR_OK,
            ),
            ephemeral=True,
        )
        await self.audit_mirror(f"📋 {interaction.user} `/roster-set-discord` {entry.nama} -> {clean_id}")

    async def roster_remove_discord_cmd(
        self, interaction: discord.Interaction, nama: str, skip_confirm: bool = False
    ) -> None:
        if not await self.guard(interaction, "roster-remove-discord"):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        entry, score = await asyncio.to_thread(self.data.find_person, nama)
        if entry is None or score < 0.8:
            await interaction.followup.send(
                f"❌ Panitia '{nama}' tidak ditemukan di roster.", ephemeral=True
            )
            return
        if not entry.discord_id:
            await interaction.followup.send(
                f"ℹ️ **{entry.nama}** belum memiliki Discord User ID yang terdaftar di roster.",
                ephemeral=True,
            )
            return

        if not skip_confirm:
            view = ConfirmView(interaction.user.id)
            await interaction.edit_original_response(
                content=f"⚠️ Konfirmasi: hapus Discord ID (`{entry.discord_id}`) untuk **{entry.nama}**?",
                view=view,
            )
            await view.wait()
            if not view.value:
                await interaction.edit_original_response(
                    content="Penghapusan Discord ID dibatalkan.", view=None
                )
                return

        try:
            entry, old_id = await asyncio.to_thread(
                self.data.update_roster_discord, entry.nama, ""
            )
        except Exception as exc:
            msg = f"❌ Gagal menghapus Discord ID: {exc}"
            if interaction.response.is_done():
                await interaction.followup.send(msg, ephemeral=True)
            else:
                await interaction.edit_original_response(content=msg, view=None)
            return

        self.outputs.audit(
            action="roster_discord_removed",
            actor=str(interaction.user),
            target=entry.nama,
            result="removed",
            rationale=f"old_id={old_id}",
        )
        await interaction.edit_original_response(
            content=f"✅ Discord ID untuk **{entry.nama}** telah dihapus dari roster.",
            view=None,
        )
        await self.audit_mirror(f"📋 {interaction.user} `/roster-remove-discord` {entry.nama} (was {old_id})")

    async def denda_list_cmd(
        self, interaction: discord.Interaction, nama: str | None
    ) -> None:
        if not await self.guard(interaction, "denda-list"):
            return
        privileged = self.has_permission(interaction.user, "validate")
        await interaction.response.defer(thinking=True, ephemeral=True)

        if not privileged:
            resolver = getattr(self.data, "resolve_discord_user", None)
            if callable(resolver):
                entry = await asyncio.to_thread(resolver, interaction.user.id)
            else:
                bound_name = config.DISCORD_MEMBER_MAP.get(str(interaction.user.id), "")
                roster = await asyncio.to_thread(self.data.load_roster) if bound_name else []
                entry = next((person for person in roster if person.nama.strip().casefold() == bound_name.strip().casefold()), None)
            if entry is None:
                await interaction.followup.send(
                    "Role @panitia hanya dapat melihat denda sendiri. "
                    "Akun Discord kamu belum terhubung ke roster. Minta koor menghubungkan akun Discord kamu "
                    "lewat `/roster-set-discord` atau DISCORD_MEMBER_MAP; nickname tidak digunakan untuk izin akses.",
                    ephemeral=True,
                )
                return
            nama = entry.nama
        if privileged and nama:
            entry, _ = await asyncio.to_thread(self.data.find_person, nama)
            if entry:
                nama = entry.nama
        rows = self.outputs.denda_rows(nama)
        pages = self.fine_embeds(rows, nama)
        for page in pages:
            self.add_freshness_warning(page)
        await interaction.followup.send(
            embed=pages[0],
            view=Paginator(pages, interaction.user.id) if len(pages) > 1 else discord.utils.MISSING,
            ephemeral=True,
        )

    def fine_embeds(self, rows: list[dict], nama: str | None = None) -> list[discord.Embed]:
        engine = self.validator.engine
        total = sum(int(float(row.get("denda") or 0)) for row in rows)
        title = f"Denda — {nama}" if nama else "Rekap denda semua panitia"
        description = (
            f"**Total akumulasi: {format_rupiah(total)}** · {len(rows)} pelanggaran aktif\n"
            "Rincian berasal dari hasil validasi tercatat, bukan pembacaan bukti baru. "
            "FLAGGED: hasil validasi; BLASTED: sudah diumumkan; RESOLVED tidak dihitung. "
            "Total ini bukan konfirmasi saldo pembayaran."
        )
        fields = []
        people = sorted({row.get("nama_komitee", "-") for row in rows})
        for person in people:
            person_rows = [row for row in rows if row.get("nama_komitee", "-") == person]
            running = 0
            for group in fine_groups(person_rows, engine):
                for index, row in enumerate(group["rows"], 1):
                    amount = int(float(row.get("denda") or 0))
                    running += amount
                    check_id = row.get("rule_id", "-")
                    meta = engine.check_meta(check_id)
                    value = (
                        f"**{check_id} — {meta.get('nama', 'Pelanggaran')}**\n"
                        f"Kenapa: {row.get('jenis_pelanggaran') or 'Detail belum tercatat; minta koor meninjau bukti.'}\n"
                        f"Seharusnya: {truncate(meta.get('syarat') or 'Ikuti ketentuan blast asli.', 250)}\n"
                        f"Biaya: **{format_rupiah(amount)}** · Status: {row.get('status', '-')}\n"
                        f"Akumulasi {person}: **{format_rupiah(running)}**"
                    )
                    if index == len(group["rows"]):
                        value += f"\nSubtotal {group['sheet']}: **{format_rupiah(group['total'])}**"
                    if row.get("evidence_link"):
                        value += f"\nBukti: {row['evidence_link'].split(' | ')[0]}"
                    fields.append((f"{person} · {group['sheet']} ({group['task_id'] or '-'})", value))
        if not fields:
            return [discord.Embed(title=title, description="Tidak ada denda aktif tercatat. Kalau ada pertanyaan soal hasil, hubungi koor ya.", color=COLOR_OK)]
        pages = []
        for start in range(0, len(fields), 4):
            page = discord.Embed(title=truncate(title, 256), description=description, color=COLOR_WARN)
            for name, value in fields[start:start + 4]:
                page.add_field(name=truncate(name, 160), value=truncate(value, 750), inline=False)
            page.set_footer(text=f"Halaman {len(pages) + 1} · /ketentuan-pp atau /ketentuan-buzzer untuk syarat dan blast asli")
            pages.append(page)
        return pages

    async def resubmit_status_cmd(self, interaction: discord.Interaction, nama: str) -> None:
        if not await self.guard(interaction, "resubmit-status"):
            return
        report = self.validator.resubmit_status(nama)
        embed = discord.Embed(
            title=f"🔁 Status — {nama}",
            description=(
                f"Total denda: {format_rupiah(report['total_denda'])}\n"
                f"Riwayat pelanggaran: {report['recurring']}x"
            ),
            color=COLOR_WARN if report["violations"] else COLOR_OK,
        )
        for row in report["violations"][:10]:
            embed.add_field(
                name=f"{row.get('id_tugas', '-')} · {row.get('rule_id', '-')} · {row.get('status', '')}",
                value=truncate(row.get("jenis_pelanggaran", ""), 300),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def audit_log_cmd(self, interaction: discord.Interaction, filter: str | None) -> None:
        if not await self.guard(interaction, "audit-log"):
            return
        tanggal = user = nama = None
        for token in (filter or "").split():
            if ":" not in token:
                continue
            key, value = token.split(":", 1)
            if key == "tanggal":
                tanggal = value
            elif key == "user":
                user = value
            elif key == "nama":
                nama = value

        entries = self.outputs.read_audit(limit=20, tanggal=tanggal, user=user, nama=nama)
        if not entries:
            await interaction.response.send_message(
                "ℹ️ Tidak ada audit log yang match dengan filter.", ephemeral=True
            )
            return
        embed = discord.Embed(
            title="📜 Audit Log (20 terakhir)",
            description=f"Filter: {filter or 'tidak ada'}",
            color=COLOR_INFO,
        )
        for entry in entries:
            embed.add_field(
                name=f"[{entry.get('ts', '')[11:16]}] {entry.get('actor', '-')} · {entry.get('action', '-')}",
                value=truncate(
                    f"{entry.get('target', '')}\n{entry.get('result', '')}", 300
                ),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def ketentuan_cmd(
        self, interaction: discord.Interaction, task_id: str | None, kind: str
    ) -> None:
        if not await self.guard(interaction, kind):
            return
        engine = self.validator.engine

        if kind == "ketentuan-angel-card":
            await interaction.response.send_message(
                embed=self._angel_card_embed(), ephemeral=True
            )
            return

        task_type = "BUZZER" if kind == "ketentuan-buzzer" else "PP"
        if task_id:
            merged = engine.merge(task_id.upper())
            if not merged.raw_task:
                ids = ", ".join(engine.task_ids(task_type))
                await interaction.response.send_message(
                    f"❌ Task ID '{task_id}' tidak ditemukan.\nTugas {task_type}: {ids}",
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                embed=self._task_embed(merged), view=self.task_link_view(merged, interaction.guild_id), ephemeral=True
            )
            return

        checks = engine.rulebase.get(
            "checks_pp" if task_type == "PP" else "checks_buzzer", []
        )
        table = engine.rulebase.get("denanda_denda", {})
        embed = discord.Embed(
            title=f"📋 Ketentuan {'Paid Promote (PP)' if task_type == 'PP' else 'Buzzer'}",
            color=COLOR_INFO,
        )
        embed.add_field(
            name="Denda",
            value=(
                f"• Tidak post/terlambat: {format_rupiah(table.get('tidak_post', 20000))}\n"
                f"• Per kesalahan posting: {format_rupiah(table.get('per_kesalahan_post', 5000))}\n"
                f"• Tidak kirim bukti: {format_rupiah(table.get('tidak_kirim_bukti', 20000))}"
            ),
            inline=False,
        )
        embed.add_field(
            name=f"{len(checks)} Checks",
            value=truncate(
                "\n".join(
                    f"`{check['id']}` {check.get('nama', '')} — "
                    f"{format_rupiah(int(check.get('denda') or 0))}"
                    for check in checks
                ),
                1000,
            ),
            inline=False,
        )
        embed.add_field(
            name="Tugas",
            value=truncate(", ".join(engine.task_ids(task_type)), 1000),
            inline=False,
        )
        embed.add_field(
            name="Channel Tugas",
            value=f"Pengumuman dan instruksi tugas {task_type} diposting di {channel_mention(config.DISCORD_BLAST_TUGAS_CHANNEL_ID)}.",
            inline=False,
        )
        embed.set_footer(text="Gunakan task_id untuk detail per tugas")
        await interaction.response.send_message(embed=embed, ephemeral=True)

    def _angel_card_embed(self) -> discord.Embed:
        angel = self.validator.rulebase.get("angel_card", {})
        harga = (
            ((self.validator.rulebase.get("denanda_denda") or {}).get("rekening") or {})
            .get("angel_card", {})
            .get("harga", 100000)
        )
        embed = discord.Embed(
            title="🎫 Ketentuan Angel Card",
            description=f"Harga: {format_rupiah(harga)}",
            color=COLOR_INFO,
        )
        embed.add_field(
            name="Membebaskan",
            value="\n".join(f"• {item}" for item in angel.get("pembebas", [])),
            inline=False,
        )
        embed.add_field(
            name="TIDAK membebaskan",
            value="• Sponsor (wajib tetap post)",
            inline=False,
        )
        embed.add_field(
            name="Syarat berlaku",
            value=truncate(
                "\n".join(f"• {item}" for item in angel.get("syarat_berlaku", [])), 1000
            ),
            inline=False,
        )
        embed.add_field(
            name="Masa berlaku", value=angel.get("masa_berlaku", "-"), inline=False
        )
        return embed

    def _task_embed(self, merged) -> discord.Embed:
        embed = discord.Embed(
            title=f"📋 Ketentuan {merged.task_id} ({merged.task_type})",
            color=COLOR_INFO,
        )
        tugas_channel = channel_mention(config.DISCORD_BLAST_TUGAS_CHANNEL_ID)
        embed.add_field(
            name="Channel Tugas",
            value=f"Materi dan blast asli diposting di {tugas_channel}.",
            inline=False,
        )
        embed.add_field(name="Platform", value=", ".join(merged.platforms) or "-", inline=True)
        embed.add_field(name="Jadwal post", value=friendly_date(merged.jadwal_post), inline=True)
        embed.add_field(name="Deadline", value=friendly_date(merged.deadline), inline=True)
        checks = check_explanations(self.validator.engine, merged.task_id)
        # Separate fields keep every check explanation within Discord limits.
        for index, line in enumerate(checks):
            embed.add_field(name="Perlu diperhatiin" if index == 0 else "Poin berikutnya", value=truncate(line, 1000), inline=False)
        embed.add_field(name="Singkatnya", value="Kerjakan semua poin di atas, pakai bukti yang jelas, "
            "dan submit sebelum deadline. Kalau ada bagian yang belum jelas, cocokkan dengan blast asli atau tanya koor.", inline=False)
        if not merged.blast or not str(merged.blast.blast_message_id).isdigit():
            embed.add_field(name="Blast asli", value=f"Pesan tugas belum terhubung. Cek {tugas_channel}; "
                "koor dapat menjalankan `/scan-blast` agar tombol pesan tugas tersedia.", inline=False)
        if merged.caption_reference:
            embed.add_field(
                name="Caption (wajib verbatim)",
                value=truncate(merged.caption_reference, 1000),
                inline=False,
            )
        if merged.tags:
            embed.add_field(name="Tags wajib", value=", ".join(merged.tags), inline=False)
        if merged.hashtags:
            embed.add_field(
                name="Hashtag", value=truncate(", ".join(merged.hashtags), 1000), inline=False
            )
        if merged.drive_link:
            embed.add_field(name="Media", value=merged.drive_link, inline=False)
        if merged.form_link:
            embed.add_field(name="Submit bukti", value=merged.form_link, inline=False)
        if merged.do_not_delete_until:
            embed.add_field(
                name="Feed jangan dihapus sampai",
                value=fmt_dt(merged.do_not_delete_until),
                inline=False,
            )
        if merged.blast:
            embed.add_field(
                name="Ketentuan tambahan (blast)",
                value=truncate("\n".join(merged.blast.notes) or merged.blast.blast_message_id, 1000),
                inline=False,
            )
        if merged.warnings:
            embed.add_field(
                name="⚠️ Catatan",
                value=truncate("\n".join(merged.warnings), 1000),
                inline=False,
            )
        return embed

    async def list_sheets_cmd(self, interaction: discord.Interaction) -> None:
        if not await self.guard(interaction, "list-sheets"):
            return
        await interaction.response.defer(thinking=True, ephemeral=True)
        infos = [info for info in await asyncio.to_thread(self.data.describe_sheets) if info.is_submission]
        lines = [
            f"`{info.name[:30]:30}` {info.rows:>3} rows · {info.multi_link:>3} multi · "
            f"{info.task_id or '—'}"
            for info in infos
        ]
        embed = discord.Embed(
            title="📑 Sheet tersedia",
            description=truncate("\n".join(lines), 3500),
            color=COLOR_INFO,
        )
        embed.set_footer(text=f"{len(infos)} submission sheets")
        self.add_freshness_warning(embed)
        await interaction.followup.send(embed=embed, ephemeral=True)

    # ── /scan-blast ────────────────────────────────────────────────────────
    async def scan_blast_cmd(self, interaction: discord.Interaction, days: int) -> None:
        if not await self.guard(interaction, "scan-blast"):
            return
        channel = self.channel("blast_tugas")
        if channel is None:
            await interaction.response.send_message(
                "❌ Channel #blast-tugas belum dikonfigurasi.", ephemeral=True
            )
            return
        await interaction.response.defer(thinking=True)

        cutoff = wib_now() - timedelta(days=max(1, days))
        found = 0
        stored = 0
        skipped = 0
        async for message in channel.history(limit=config.BLAST_SCAN_MAX_MESSAGES, after=cutoff):
            if message.author.bot:
                continue
            if not looks_like_blast(message.content):
                continue
            found += 1
            if str(message.id) in self.processed:
                skipped += 1
                continue
            parsed = parse_blast_message(
                message.content, self.rulebase, message.created_at.replace(tzinfo=None)
            )
            if not parsed.task_id:
                skipped += 1
                continue
            self.contexts.upsert(parsed.to_context(message_id=str(message.id)))
            self.processed.add(str(message.id))
            stored += 1
            with suppress(discord.HTTPException):
                await message.add_reaction("✅")

        self.contexts.reload()
        self.validator.reload_context()
        self.outputs.audit(
            action="scan_blast",
            actor=str(interaction.user),
            target=f"#{channel.name}",
            result=f"{found} found, {stored} stored, {skipped} skipped",
            rationale=f"last {days} days",
        )
        await interaction.edit_original_response(
            embed=discord.Embed(
                title="🔎 Scan #blast-tugas selesai",
                description=(
                    f"Pesan blast ditemukan: **{found}**\n"
                    f"Ketentuan baru disimpan: **{stored}**\n"
                    f"Skip (sudah diproses / tanpa task): **{skipped}**\n\n"
                    f"Total task dengan blast context: "
                    f"**{len(self.validator.blast_context)}**"
                ),
                color=COLOR_OK,
            )
        )
        await self.audit_mirror(
            f"🔎 {interaction.user} `/scan-blast` {days}d → {stored} stored"
        )

    # ── passive listeners ──────────────────────────────────────────────────
    async def on_message(self, message: discord.Message) -> None:
        if message.author.bot:
            return
        channel_id = str(message.channel.id)

        if channel_id and channel_id == config.DISCORD_BUKTI_TAMBAHAN_CHANNEL_ID:
            await self._handle_bukti(message)
            return

        if channel_id and channel_id == config.DISCORD_BLAST_TUGAS_CHANNEL_ID:
            await self._handle_blast_message(message)
            return

        if channel_id and channel_id == config.DISCORD_AI_CHAT_CHANNEL_ID:
            await self._handle_chat(message)
            return

        await self.process_commands(message)

    async def _handle_bukti(self, message: discord.Message) -> None:
        if not self.has_permission(message.author, "validate"):
            return
        attachments = [item for item in message.attachments if item.content_type and item.content_type.startswith("image")]
        link = attachments[0].url if attachments else (message.attachments[0].url if message.attachments else "")
        if not message.content.strip() and not link:
            return

        result = await asyncio.to_thread(
            self.bukti.handle,
            message.content,
            link,
            str(message.author),
            str(message.author),
            message.created_at.replace(tzinfo=None),
        )
        await message.reply(result.message, mention_author=False)
        if result.ok:
            self.outputs.audit(
                action="bukti_tambahan",
                actor=str(message.author),
                target=result.entry.get("submission", "") if result.entry else "",
                result="stored",
                rationale=result.entry.get("deskripsi", "") if result.entry else "",
            )
            await self.audit_mirror(
                f"📎 {message.author} upload bukti tambahan: "
                f"{result.entry.get('submission', '')} @{result.entry.get('ig', '')}"
            )

    async def _handle_blast_message(self, message: discord.Message) -> None:
        if not looks_like_blast(message.content):
            return
        if not self.has_permission(message.author, "blast"):
            return
        if str(message.id) in self.processed:
            return
        parsed = parse_blast_message(
            message.content,
            rulebase=None,
            message_date=message.created_at.replace(tzinfo=None),
            context_store=self.contexts,
        )
        if not parsed.task_id:
            warn_text = (
                "⚠️ Blast terdeteksi tapi task_id tidak bisa ditentukan. "
                "Sertakan kode tugas (mis. PP-03) atau nama sheet agar ketentuan "
                "tidak salah diterapkan."
            )
            try:
                await message.author.send(warn_text)
            except (discord.Forbidden, discord.HTTPException):
                with suppress(discord.HTTPException):
                    await message.reply(warn_text, mention_author=False, delete_after=10)
            return

        self.contexts.upsert(parsed.to_context(message_id=str(message.id)))
        self.processed.add(str(message.id))

        with suppress(discord.HTTPException):
            await message.add_reaction("✅")

        embed = discord.Embed(
            title=f"Kode Buzzer/Paid Promote: {parsed.task_id}",
            description=parsed.short_description,
            color=COLOR_OK if parsed.confidence >= 0.7 else COLOR_WARN,
        )
        if parsed.search_tags:
            embed.add_field(
                name="Tags Pencarian",
                value=" ".join(f"`{tag}`" for tag in parsed.search_tags),
                inline=False,
            )
        embed.set_footer(text="🔍 Cari tugas ini lewat chatbot #ai-chat dengan kode atau tags di atas")
        await message.reply(embed=embed, mention_author=False)

        private_response = (
            f"✅ Ketentuan tersimpan untuk **{parsed.task_id}** "
            f"(confidence {parsed.confidence:.0%}) — "
            f"tags {len(parsed.tags)}, hashtags {len(parsed.hashtags)}, "
            f"deadline {fmt_dt(parsed.deadline)}"
            + ("\n" + "\n".join(parsed.notes) if parsed.notes else "")
        )
        try:
            await message.author.send(private_response)
        except (discord.Forbidden, discord.HTTPException):
            with suppress(discord.HTTPException):
                await message.reply(
                    f"{message.author.mention} {private_response}\n*(Pesan konfirmasi ini otomatis terhapus dalam 10 detik)*",
                    mention_author=False,
                    delete_after=10,
                )

        try:
            from .agent import knowledge
            await asyncio.to_thread(knowledge.index_blast_task, parsed)
        except Exception as exc:
            logger.warning("Failed to index blast task into vector store: %s", exc)

        self.outputs.audit(
            action="blast_extracted",
            actor=str(message.author),
            target=parsed.task_id,
            result=f"confidence {parsed.confidence}",
            rationale="auto-read #blast-tugas",
        )
        await self.audit_mirror(
            f"📥 Blast {parsed.task_id} diekstrak dari #{message.channel.name} "
            f"({parsed.confidence:.0%})"
        )

    async def execute_chat_action(self, interaction: discord.Interaction, action: dict) -> None:
        """Dispatch a planner action after its Confirm button was clicked.

        The permission re-check already happened in ChatActionView.confirm; the
        target command methods keep their own guard() checks as defence in depth.
        This only maps the pending_action params onto the existing signatures.
        """
        command = action["command"]
        params = action.get("params") or {}

        # Record the confirmed execution, but never let an audit write failure
        # block the command itself.
        try:
            self.outputs.audit(
                action="chat_command_confirmed",
                actor=str(interaction.user),
                target=command,
                result="executed",
                rationale=truncate(action.get("description", ""), 300),
                extra={"params": params},
            )
        except Exception:
            pass

        usage = {
            "validate": "Gunakan `/validate sheet:<nama> [force:True|False] [divisi:<divisi>]`.",
            "blast": "Gunakan `/blast sheet:<nama> [divisi:<divisi>]`.",
            "add-angel": "Gunakan `/add-angel nama:<nama> tanggal:YYYY-MM-DD [divisi:<divisi>]`.",
            "remove-angel": "Gunakan `/remove-angel nama:<nama>`.",
            "scan-blast": "Gunakan `/scan-blast [days:7]`.",
            "hold-blast": "Gunakan `/hold-blast blast_id:<id> [reason:<alasan>]`.",
            "release-blast": "Gunakan `/release-blast blast_id:<id>`.",
            "roster-set-discord": "Gunakan `/roster-set-discord nama:<nama> discord_id:<id>`.",
            "roster-remove-discord": "Gunakan `/roster-remove-discord nama:<nama>`.",
        }

        def missing(*keys: str) -> bool:
            return any(not str(params.get(key, "") or "").strip() for key in keys)

        if command == "validate":
            if missing("sheet"):
                await interaction.response.send_message(usage["validate"], ephemeral=True)
                return
            await self.validate_cmd(
                interaction, params["sheet"],
                bool(params.get("force", False)), params.get("divisi", ""),
            )
        elif command == "blast":
            if missing("sheet"):
                await interaction.response.send_message(usage["blast"], ephemeral=True)
                return
            await self.blast_cmd(interaction, params["sheet"], params.get("divisi", ""))
        elif command == "add-angel":
            if missing("nama", "tanggal"):
                await interaction.response.send_message(usage["add-angel"], ephemeral=True)
                return
            await self.add_angel_cmd(
                interaction, params["nama"], params["tanggal"], params.get("divisi", ""),
            )
        elif command == "remove-angel":
            if missing("nama"):
                await interaction.response.send_message(usage["remove-angel"], ephemeral=True)
                return
            await self.remove_angel_cmd(interaction, params["nama"])
        elif command == "scan-blast":
            try:
                days = int(params.get("days", 7))
            except (TypeError, ValueError):
                days = 7
            await self.scan_blast_cmd(interaction, days)
        elif command == "hold-blast":
            if missing("blast_id"):
                await interaction.response.send_message(usage["hold-blast"], ephemeral=True)
                return
            if not await self.guard(interaction, "hold-blast"):
                return
            blast_id = params["blast_id"]
            reason = params.get("reason", "") or ""
            record = self.blasts.hold(blast_id, str(interaction.user), reason)
            if record is None:
                await interaction.response.send_message(
                    f"❌ Blast `{blast_id}` tidak ditemukan.", ephemeral=True
                )
                return
            self.outputs.audit(
                action="blast_held", actor=str(interaction.user),
                target=record.sheet_name, result="HELD", rationale=reason or "-",
            )
            await interaction.response.send_message(
                f"⏸️ Blast `{blast_id}` ditahan. "
                f"Release dengan `/release-blast blast_id:{blast_id}`.",
                ephemeral=True,
            )
            await self.audit_mirror(f"⏸️ {interaction.user} hold {blast_id}")
        elif command == "release-blast":
            if missing("blast_id"):
                await interaction.response.send_message(usage["release-blast"], ephemeral=True)
                return
            if not await self.guard(interaction, "release-blast"):
                return
            blast_id = params["blast_id"]
            record = self.blasts.release(blast_id, str(interaction.user))
            if record is None:
                await interaction.response.send_message(
                    f"❌ Blast `{blast_id}` tidak ditemukan.", ephemeral=True
                )
                return
            self.outputs.audit(
                action="blast_released", actor=str(interaction.user),
                target=record.sheet_name, result="DRAFT", rationale="released",
            )
            await interaction.response.send_message(
                f"▶️ Blast `{blast_id}` kembali ke DRAFT. Review di #blast-drafts.",
                ephemeral=True,
            )
        elif command == "roster-set-discord":
            if missing("nama", "discord_id"):
                await interaction.response.send_message(usage["roster-set-discord"], ephemeral=True)
                return
            await self.roster_set_discord_cmd(interaction, params["nama"], str(params["discord_id"]))
        elif command == "roster-remove-discord":
            if missing("nama"):
                await interaction.response.send_message(usage["roster-remove-discord"], ephemeral=True)
                return
            await self.roster_remove_discord_cmd(interaction, params["nama"], skip_confirm=True)
        else:
            await interaction.response.send_message(
                "Perintah tidak dikenali; aksi dibatalkan.", ephemeral=True
            )

    AUDIO_EXTENSIONS = {".ogg", ".mp3", ".wav", ".m4a", ".aac", ".flac", ".webm", ".oga"}

    @classmethod
    def is_audio_attachment(cls, attachment: discord.Attachment) -> bool:
        ct = (getattr(attachment, "content_type", "") or "").lower()
        if ct.startswith("audio/"):
            return True
        filename = (getattr(attachment, "filename", "") or "").lower()
        return any(filename.endswith(ext) for ext in cls.AUDIO_EXTENSIONS)

    @classmethod
    def get_audio_attachment(cls, message: discord.Message) -> discord.Attachment | None:
        is_voice = getattr(message.flags, "voice", False) or (
            callable(getattr(message, "is_voice_message", None)) and message.is_voice_message()
        )
        for att in getattr(message, "attachments", []):
            if is_voice or cls.is_audio_attachment(att):
                return att
        return None

    async def _handle_chat(self, message: discord.Message) -> None:
        if not self.has_permission(message.author, "validate"):
            return
        key = f"{message.channel.id}:{message.author.id}"
        if key in self._chat_busy:
            await message.reply("Sebentar ya, aku lagi baca pesanmu yang sebelumnya. Setelah jawabanku keluar, kita lanjut.", mention_author=False)
            return
        self._chat_busy.add(key)
        try:
            history = self._conversations.history(key)
            # Bot mentions should not turn a plain greeting into an out-of-scope question.
            content = re.sub(r"<@!?\d+>", "", message.content).strip()
            actor_id = str(message.author.id)
            channel_id = str(message.channel.id)
            user_id = str(message.author.id)

            audio_att = self.get_audio_attachment(message)
            transcript = ""
            if audio_att:
                if not config.VOICE_DETECTION_ENABLED:
                    await message.reply(
                        "Fitur pesan suara (voice note) saat ini sedang dinonaktifkan. Silakan kirim pertanyaanmu lewat teks ya.",
                        mention_author=False,
                    )
                    return

                if not config.NINEROUTER_API_KEY:
                    await message.reply(
                        "Aku mendeteksi pesan suara, tapi gateway 9Router belum dikonfigurasi (`NINEROUTER_API_KEY` belum diset). "
                        "Silakan gunakan pesan teks ya.",
                        mention_author=False,
                    )
                    return

                size = getattr(audio_att, "size", 0)
                if size > 25 * 1024 * 1024:
                    await message.reply(
                        "Ukuran rekaman suara melebihi batas 25MB. Coba kirim rekaman yang lebih singkat ya.",
                        mention_author=False,
                    )
                    return

                try:
                    from .voice_transcriber import (
                        TranscriptionConfigError,
                        TranscriptionEmptyError,
                        TranscriptionRateLimitError,
                        VoiceDisabledError,
                    )
                    async with message.channel.typing():
                        audio_bytes = await audio_att.read()
                        if not audio_bytes:
                            await message.reply(
                                "File rekaman suara kosong atau tidak dapat diunduh. Coba kirim ulang ya.",
                                mention_author=False,
                            )
                            return
                        transcript = await asyncio.to_thread(
                            self.voice_transcriber.transcribe_bytes,
                            audio_bytes,
                            filename=audio_att.filename or "voice-message.ogg",
                            content_type=audio_att.content_type or "audio/ogg",
                        )
                except TranscriptionEmptyError:
                    await message.reply(
                        "Pesan suara tidak terdengar jelas atau tidak ada ucapan terdeteksi. Boleh diulang atau diketik lewat teks ya?",
                        mention_author=False,
                    )
                    return
                except TranscriptionRateLimitError:
                    await message.reply(
                        "Layanan transkripsi suara sedang sibuk (rate limit). Coba lagi sebentar lagi atau ketik lewat teks ya.",
                        mention_author=False,
                    )
                    return
                except VoiceDisabledError:
                    await message.reply(
                        "Fitur pesan suara (voice note) saat ini sedang dinonaktifkan. Silakan kirim pertanyaanmu lewat teks ya.",
                        mention_author=False,
                    )
                    return
                except TranscriptionConfigError:
                    await message.reply(
                        "Konfigurasi transkripsi suara bermasalah (`NINEROUTER_API_KEY` belum diset). Silakan gunakan pesan teks.",
                        mention_author=False,
                    )
                    return
                except Exception as exc:
                    logger.warning("Voice transcription failed: %s", exc)
                    await message.reply(
                        "Maaf, terjadi kendala saat memproses pesan suaramu. Coba kirim lewat teks ya.",
                        mention_author=False,
                    )
                    return

                if transcript:
                    content = f"{content} {transcript}".strip() if content else transcript
                    try:
                        self.outputs.audit(
                            action="voice_transcription",
                            actor=str(message.author),
                            target=audio_att.filename or "voice-note",
                            result="transcribed",
                            rationale=truncate(transcript, 100),
                        )
                    except Exception:
                        pass

            if not content:
                return

            try:
                from .agent.graph import DiscordAgentContext

                agent_ctx = DiscordAgentContext(
                    user_id=user_id,
                    channel_id=channel_id,
                    guild_id=str(getattr(message.guild, "id", "") if hasattr(message, "guild") else ""),
                    actor=str(message.author),
                    member=message.author,
                    validator=self.validator,
                    is_voice=bool(transcript),
                    voice_transcript=transcript,
                    voice_detection_enabled=config.VOICE_DETECTION_ENABLED,
                )
            except Exception:
                agent_ctx = None

            async with message.channel.typing():
                answer = await asyncio.to_thread(
                    _dispatch_chat, content, self.validator, actor_id, history,
                    member=message.author, channel_id=channel_id, user_id=user_id,
                    context=agent_ctx,
                )

            if answer.error:
                try:
                    self.outputs.audit(
                        action="chat_provider_failed",
                        actor=str(message.author),
                        target=truncate(content, 100),
                        rationale=answer.error,
                    )
                except Exception:
                    pass

            warnings = getattr(self.data.source, "freshness_warnings", [])
            formatted = format_discord_message(answer.text)
            if transcript:
                formatted = f"🎙️ *\"{transcript}\"*\n\n{formatted}"
            text = formatted + ("\n\nCatatan data: " + "\n".join(warnings) if warnings else "")
            pending = answer.pending_action
            if pending:
                # Single message carrying the plan text plus the confirm gate.
                # Keep it under Discord's 2000-char limit.
                cmd_name = str(pending.get("command") or "")
                plan_hint = (
                    f"\n\n> ⚠️ **Aksi disiapkan:** `{cmd_name}` ({pending.get('description', '')})"
                    if cmd_name and f"`{cmd_name}`" not in text
                    else ""
                )
                await message.reply(
                    truncate(text + plan_hint, 1900),
                    view=ChatActionView(self, pending, message.author.id),
                    mention_author=False,
                    allowed_mentions=discord.AllowedMentions.none(),
                )
            else:
                for chunk in _chunks(text):
                    await message.reply(chunk, mention_author=False, allowed_mentions=discord.AllowedMentions.none())

            # Store turn in conversation history for multi-turn context
            self._conversations.remember(key, content, formatted)
            if not answer.in_scope:
                self.outputs.audit(
                    action="chat_out_of_scope", actor=str(message.author),
                    target=truncate(content, 100), result="refused", rationale="scope guard",
                )
        except Exception:
            await message.reply("Aku belum bisa baca data tugas saat ini. Coba lagi nanti, atau hubungi koor kalau mendesak ya.", mention_author=False)
        finally:
            self._chat_busy.discard(key)


def _dispatch_chat(
    content,
    validator,
    actor_id,
    history,
    member=None,
    channel_id="",
    user_id="",
    context=None,
):
    """Entry point for #ai-chat.

    The LangGraph agent (local zvec retrieval + role-gated tools + 9Router)
    handles the conversation when enabled. Every failure falls back to the
    deterministic grounded path, which itself falls back to grounded facts
    when the gateway is down — a reply is never skipped.
    """
    if config.CHAT_AGENT_ENABLED and config.CHAT_ENABLED:
        try:
            from .agent.graph import answer_with_agent

            return answer_with_agent(
                content,
                validator,
                actor=actor_id,
                history=history,
                member=member,
                channel_id=channel_id,
                user_id=user_id,
                context=context,
            )
        except Exception:
            logger.warning("Agent path failed, using deterministic chat", exc_info=True)
    return chat_answer(content, validator, actor_id, history, member=member)


# ─── command registration ───────────────────────────────────────────────────
bot = MamazonBot()


@bot.tree.command(name="help", description="Panduan Mamazon dan command sesuai role kamu")
async def help_command(interaction: discord.Interaction):
    await bot.help_cmd(interaction)


@bot.tree.command(name="validate", description="Validasi sheet PP/Buzzer")
@app_commands.describe(sheet="Nama sheet (boleh disingkat)", force="Re-validasi yang sudah VERIFIED", divisi="Divisi dalam sheet; kosongkan untuk semua")
async def validate_command(interaction: discord.Interaction, sheet: str, force: bool = False, divisi: str = ""):
    await bot.validate_cmd(interaction, sheet, force, divisi)


@validate_command.autocomplete("sheet")
async def validate_autocomplete(interaction: discord.Interaction, current: str):
    if not bot.has_permission(interaction.user, "validate"):
        return []
    try:
        names = await asyncio.to_thread(bot.data.submission_sheets)
    except Exception:
        return []
    return [app_commands.Choice(name=name[:100], value=name)
            for name in names if current.casefold() in name.casefold()][:25]


@validate_command.autocomplete("divisi")
async def division_autocomplete(interaction: discord.Interaction, current: str):
    if not bot.has_permission(interaction.user, "validate"):
        return []
    def read_divisions():
        query = getattr(interaction.namespace, "sheet", "") or ""
        if not query:
            return []
        canonical, _ = bot.validator._resolve_sheet(query)
        rows = bot.data.read_submissions(canonical)
        roster = bot.data.load_roster()
        by_name = {entry.nama.strip().casefold(): entry.divisi for entry in roster}
        return sorted({row.divisi.strip() or by_name.get(row.nama.strip().casefold(), "") for row in rows} - {""})
    try:
        choices = await asyncio.to_thread(read_divisions)
    except Exception:
        return []
    return [app_commands.Choice(name=name[:100], value=name)
            for name in choices if current.casefold() in name.casefold()][:25]


@bot.tree.command(name="blast", description="Buat draft blast denda (butuh approval koor)")
@app_commands.describe(sheet="Nama sheet", divisi="Batasi draft ke divisi; kosongkan untuk semua")
async def blast_command(interaction: discord.Interaction, sheet: str, divisi: str = ""):
    await bot.blast_cmd(interaction, sheet, divisi)


@bot.tree.command(name="hold-blast", description="Tahan blast yang sudah dibuat")
@app_commands.describe(blast_id="ID blast", reason="Alasan (opsional)")
async def hold_blast_command(interaction: discord.Interaction, blast_id: str, reason: str = ""):
    if not await bot.guard(interaction, "hold-blast"):
        return
    record = bot.blasts.hold(blast_id, str(interaction.user), reason)
    if record is None:
        await interaction.response.send_message(f"❌ Blast `{blast_id}` tidak ditemukan.", ephemeral=True)
        return
    bot.outputs.audit(
        action="blast_held", actor=str(interaction.user), target=record.sheet_name,
        result="HELD", rationale=reason or "-",
    )
    await interaction.response.send_message(
        f"⏸️ Blast `{blast_id}` ditahan. Release dengan `/release-blast blast_id:{blast_id}`.",
        ephemeral=True,
    )
    await bot.audit_mirror(f"⏸️ {interaction.user} hold {blast_id}")


@bot.tree.command(name="release-blast", description="Release blast yang ditahan")
@app_commands.describe(blast_id="ID blast")
async def release_blast_command(interaction: discord.Interaction, blast_id: str):
    if not await bot.guard(interaction, "release-blast"):
        return
    record = bot.blasts.release(blast_id, str(interaction.user))
    if record is None:
        await interaction.response.send_message(f"❌ Blast `{blast_id}` tidak ditemukan.", ephemeral=True)
        return
    bot.outputs.audit(
        action="blast_released", actor=str(interaction.user),
        target=record.sheet_name, result="DRAFT", rationale="released",
    )
    await interaction.response.send_message(
        f"▶️ Blast `{blast_id}` kembali ke DRAFT. Review di #blast-drafts.",
        ephemeral=True,
    )


@bot.tree.command(name="list-active", description="Status tugas PP/Buzzer")
async def list_active_command(interaction: discord.Interaction):
    await bot.list_active_cmd(interaction)


@bot.tree.command(name="check-angel", description="Lihat Angel Card Registry")
async def check_angel_command(interaction: discord.Interaction):
    await bot.check_angel_cmd(interaction)


@bot.tree.command(name="add-angel", description="Tambah Angel Card")
@app_commands.describe(nama="Nama panitia", tanggal="Tanggal pembelian (YYYY-MM-DD)", divisi="Divisi")
async def add_angel_command(interaction: discord.Interaction, nama: str, tanggal: str, divisi: str = ""):
    await bot.add_angel_cmd(interaction, nama, tanggal, divisi)


@bot.tree.command(name="remove-angel", description="Hapus Angel Card")
@app_commands.describe(nama="Nama panitia")
async def remove_angel_command(interaction: discord.Interaction, nama: str):
    await bot.remove_angel_cmd(interaction, nama)


@bot.tree.command(name="denda-list", description="Lihat denda")
@app_commands.describe(nama="Nama panitia (kosongkan untuk rekap)")
async def denda_list_command(interaction: discord.Interaction, nama: str | None = None):
    await bot.denda_list_cmd(interaction, nama)


@bot.tree.command(name="resubmit-status", description="Cek status resubmit panitia")
@app_commands.describe(nama="Nama panitia")
async def resubmit_status_command(interaction: discord.Interaction, nama: str):
    await bot.resubmit_status_cmd(interaction, nama)


@bot.tree.command(name="audit-log", description="Lihat audit log")
@app_commands.describe(filter="contoh: tanggal:2026-11-05 user:@spade")
async def audit_log_command(interaction: discord.Interaction, filter: str | None = None):
    await bot.audit_log_cmd(interaction, filter)


@bot.tree.command(name="ketentuan-pp", description="Ketentuan Paid Promote")
@app_commands.describe(task_id="ID tugas, mis. PP-03")
async def ketentuan_pp_command(interaction: discord.Interaction, task_id: str | None = None):
    await bot.ketentuan_cmd(interaction, task_id, "ketentuan-pp")


@bot.tree.command(name="ketentuan-buzzer", description="Ketentuan Buzzer")
@app_commands.describe(task_id="ID tugas, mis. BZ-06")
async def ketentuan_buzzer_command(interaction: discord.Interaction, task_id: str | None = None):
    await bot.ketentuan_cmd(interaction, task_id, "ketentuan-buzzer")


@bot.tree.command(name="ketentuan-angel-card", description="Ketentuan Angel Card")
async def ketentuan_angel_command(interaction: discord.Interaction):
    await bot.ketentuan_cmd(interaction, None, "ketentuan-angel-card")


@bot.tree.command(name="scan-blast", description="Scan #blast-tugas untuk ketentuan baru")
@app_commands.describe(days="Rentang hari ke belakang")
async def scan_blast_command(interaction: discord.Interaction, days: int = 7):
    await bot.scan_blast_cmd(interaction, days)


@bot.tree.command(name="list-sheets", description="Daftar sheet submission")
async def list_sheets_command(interaction: discord.Interaction):
    await bot.list_sheets_cmd(interaction)


@bot.tree.command(name="roster-set-discord", description="Tambah atau ubah Discord User ID panitia di Roster")
@app_commands.describe(nama="Nama panitia di roster", discord_id="Discord User ID (angka)")
async def roster_set_discord_command(interaction: discord.Interaction, nama: str, discord_id: str):
    await bot.roster_set_discord_cmd(interaction, nama, discord_id)


@bot.tree.command(name="roster-remove-discord", description="Hapus Discord User ID panitia di Roster")
@app_commands.describe(nama="Nama panitia di roster")
async def roster_remove_discord_command(interaction: discord.Interaction, nama: str):
    await bot.roster_remove_discord_cmd(interaction, nama)


# ─── entry point ────────────────────────────────────────────────────────────
def main() -> None:
    problems = config.validate_config(require_discord=True)
    if problems:
        print("Konfigurasi belum lengkap:")
        for problem in problems:
            print(f"  - {problem}")
        print("\nIsi `agent/.env` (lihat agent/.env.example) lalu jalankan ulang.")
        print("Diagnosa Google: `python -m src.main google-status`")
        print("Tanpa Discord, gunakan CLI: `python -m src.main validate --sheet ...`")
        raise SystemExit(1)

    if bot.startup_error:
        print("❌ Gagal membuka spreadsheet:")
        print(f"   {bot.startup_error}")
        print("\nCek dengan: python -m src.main google-status")
        raise SystemExit(1)

    # Centralized timezone-aware logging (clean console + verbose daily file logs)
    from .logging_config import setup_logging

    setup_logging()
    print("[bot] starting… (Ctrl+C to stop)", flush=True)
    # log_handler=None: we already configured logging; otherwise discord.py adds
    # a second handler and every line is printed twice.
    bot.run(config.DISCORD_BOT_TOKEN, log_handler=None)


if __name__ == "__main__":
    main()
