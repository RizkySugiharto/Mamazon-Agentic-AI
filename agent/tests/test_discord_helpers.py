"""Discord message formatting helpers (no gateway connection required)."""

from __future__ import annotations

from src.discord_bot import _chunks


class TestChunks:
    def test_short_text_single_chunk(self):
        assert _chunks("hello") == ["hello"]

    def test_respects_limit(self):
        text = "\n".join(f"baris {index}" for index in range(500))
        parts = _chunks(text, limit=1900)
        assert all(len(part) <= 1900 for part in parts)
        assert "".join(parts) == text

    def test_hard_splits_one_long_line(self):
        """A 5000-char single line must not become a 5000-char message."""
        parts = _chunks("x" * 5000, limit=1900)
        assert len(parts) == 3
        assert all(len(part) <= 1900 for part in parts)
        assert sum(len(part) for part in parts) == 5000

    def test_empty(self):
        assert _chunks("") == []


class TestNewBotFeatures:
    def test_help_registered_and_division_option(self):
        from src.discord_bot import bot
        assert bot.tree.get_command("help")
        names = [parameter.name for parameter in bot.tree.get_command("validate").parameters]
        assert "divisi" in names

    def test_fines_are_fully_paginated_with_task_and_accumulation(self, engine):
        from src.discord_bot import MamazonBot
        from types import SimpleNamespace
        dummy = SimpleNamespace(validator=SimpleNamespace(engine=engine))
        rows = [{"nama_komitee": "Member", "sheet_name": "PP 07 Oktober", "id_tugas": "",
                 "rule_id": "C4", "jenis_pelanggaran": "Caption tidak sesuai", "denda": "5000", "status": "FLAGGED"} for _ in range(17)]
        pages = MamazonBot.fine_embeds(dummy, rows, "Member")
        assert len(pages) == 5
        assert sum(len(page.fields) for page in pages) == 17
        rendered = "\n".join(str(page.to_dict()) for page in pages)
        assert "PP 07 Oktober" in rendered and "Caption tidak sesuai" in rendered
        assert "Rp 85.000" in rendered and "Subtotal" in rendered and "Akumulasi" in rendered
        assert all(len(page) <= 6000 for page in pages)

    def test_requirements_describe_checks_and_all_fit(self, engine):
        from src.discord_bot import MamazonBot
        from types import SimpleNamespace
        dummy = SimpleNamespace(validator=SimpleNamespace(engine=engine))
        for task_id in engine.task_ids():
            embed = MamazonBot._task_embed(dummy, engine.merge(task_id))
            assert len(embed.fields) <= 25
            assert len(embed) <= 6000
            assert all(len(field.value) <= 1024 for field in embed.fields)
        rendered = str(MamazonBot._task_embed(dummy, engine.merge("BZ-02")).to_dict())
        assert "B3" in rendered and "B7" in rendered and "Rabu" in rendered
        assert "Singkatnya" in rendered

    def test_task_links_never_fabricate_message_id(self, engine, monkeypatch):
        import asyncio
        from src import config
        from src.discord_bot import MamazonBot
        from src.models import BlastContext
        monkeypatch.setattr(config, "DISCORD_BLAST_TUGAS_CHANNEL_ID", "123")
        merged = engine.merge("BZ-02")
        async def check():
            view = MamazonBot.task_link_view(None, merged, 456)
            assert len(view.children) == 1
            merged.blast = BlastContext(task_id="BZ-02", blast_message_id="789")
            view = MamazonBot.task_link_view(None, merged, 456)
            assert view.children[1].url == "https://discord.com/channels/456/123/789"
            merged.blast.blast_message_id = "mock-id"
            assert len(MamazonBot.task_link_view(None, merged, 456).children) == 1
        asyncio.run(check())

    def test_cancel_rechecks_role_owner_and_commit_boundary(self):
        import asyncio
        from threading import Event
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from src.discord_bot import ValidationCancelView
        async def check():
            run = {"author_id": 10, "cancel": Event(), "state": {"stage": "vision"}, "sheet": "PP Test"}
            dummy = SimpleNamespace(_validation_runs={"test": run}, guard=AsyncMock(return_value=True),
                                    has_permission=lambda user, cmd: False, audit_mirror=AsyncMock())
            view = ValidationCancelView(dummy, "test", run)
            interaction = SimpleNamespace(user=SimpleNamespace(id=11), response=SimpleNamespace(send_message=AsyncMock(), edit_message=AsyncMock()),
                                          followup=SimpleNamespace(send=AsyncMock()))
            await view.cancel.callback(interaction)
            assert not run["cancel"].is_set()
            interaction.user.id = 10
            run["state"]["stage"] = "write"
            await view.cancel.callback(interaction)
            assert not run["cancel"].is_set()
            run["state"]["stage"] = "vision"
            await view.cancel.callback(interaction)
            assert run["cancel"].is_set() and view.cancel.disabled
        asyncio.run(check())

    def test_single_flight_aliases_and_release_after_cancel(self):
        import asyncio
        from threading import Event
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from src.discord_bot import MamazonBot
        from src.models import ValidationBatch
        async def check():
            entered = asyncio.Event()
            gate = Event()
            loop = asyncio.get_running_loop()
            def validate(sheet, *args, **kwargs):
                loop.call_soon_threadsafe(entered.set)
                gate.wait(timeout=5)
                assert kwargs["cancel_event"].is_set()
                return ValidationBatch(sheet_name=sheet, task_id="PP-03", task_type="PP", cancelled=True)
            dummy = SimpleNamespace(
                validator=SimpleNamespace(_resolve_sheet=lambda query: ("PP Feed 26 Oktober", None), validate=validate),
                guard=AsyncMock(return_value=True), data=SimpleNamespace(resolve_task=lambda sheet: SimpleNamespace(task_id="PP-03")),
                _validation_runs={}, _progress_embed=MamazonBot._progress_embed,
                _safe_edit=AsyncMock(), _report_batch=AsyncMock(), audit_mirror=AsyncMock(),
            )
            dummy.validation_key = lambda sheet: MamazonBot.validation_key(dummy, sheet)
            dummy._pump_progress = lambda run, view, stop: stop.wait()
            message = SimpleNamespace(edit=AsyncMock(), jump_url="https://discord.com/channels/1/2/3")
            def interaction(member):
                return SimpleNamespace(user=SimpleNamespace(id=member), channel=None,
                                       edit_original_response=AsyncMock(return_value=message))
            first = asyncio.create_task(MamazonBot.validate_and_report(dummy, interaction(10), "PP Feed", False, "Spade"))
            await entered.wait()
            await MamazonBot.validate_and_report(dummy, interaction(11), "PP Feed 26 Oktober", True, "Conan")
            assert len(dummy._validation_runs) == 1
            assert "masih dalam proses" in dummy._safe_edit.call_args.kwargs["content"]
            run = next(iter(dummy._validation_runs.values()))
            run["cancel"].set()
            gate.set()
            await first
            assert dummy._validation_runs == {}
            dummy._report_batch.assert_not_called()
        asyncio.run(check())

    def test_unbound_panitia_cannot_use_nickname_for_private_fines(self, monkeypatch):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock
        from src import config
        from src.discord_bot import MamazonBot
        monkeypatch.setattr(config, "DISCORD_MEMBER_MAP", {})
        async def check():
            dummy = SimpleNamespace(guard=AsyncMock(return_value=True), has_permission=lambda u, cmd: False,
                                    data=SimpleNamespace(load_roster=Mock(side_effect=AssertionError("unbound user should not read roster"))))
            interaction = SimpleNamespace(user=SimpleNamespace(id=10, display_name="Victim Name"),
                                          response=SimpleNamespace(defer=AsyncMock()), followup=SimpleNamespace(send=AsyncMock()))
            await MamazonBot.denda_list_cmd(dummy, interaction, "Victim Name")
            assert "DISCORD_MEMBER_MAP" in interaction.followup.send.call_args.args[0]
        asyncio.run(check())

    def test_safe_edit_converts_attachment_for_expired_token(self):
        import asyncio
        import discord
        import io
        from types import SimpleNamespace
        from unittest.mock import AsyncMock
        from src.discord_bot import MamazonBot
        async def check():
            response = SimpleNamespace(status=404, reason="Not Found")
            interaction = SimpleNamespace(edit_original_response=AsyncMock(side_effect=discord.HTTPException(response, "expired")),
                                          channel=SimpleNamespace(send=AsyncMock()))
            file = discord.File(io.BytesIO(b"test"), filename="report.csv")
            await MamazonBot._safe_edit(interaction, attachments=[file], content="result")
            assert interaction.channel.send.call_args.kwargs["files"] == [file]
            assert "attachments" not in interaction.channel.send.call_args.kwargs
        asyncio.run(check())

    def test_stale_draft_is_not_approved(self):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock
        from src.discord_bot import MamazonBot
        from src.models import BlastRecord, BlastEntry
        async def check():
            record = BlastRecord(blast_id="id", sheet_name="PP Test", task_id="PP-03",
                                 entries=[BlastEntry("Member", "Alpha", "", 5000,
                                    violations=[{"rule_id": "C4", "denda": 5000, "detail": "old failure"}])])
            dummy = SimpleNamespace(guard=AsyncMock(return_value=True), blasts=SimpleNamespace(get=lambda bid: record),
                                    validation_key=lambda s: "task:PP-03", _validation_runs={}, _blast_sending=set(),
                                    _entries_from_report=lambda *args: [])
            interaction = SimpleNamespace(user=SimpleNamespace(id=10), response=SimpleNamespace(send_message=AsyncMock()))
            await MamazonBot._handle_blast_action(dummy, interaction, "id", "approve")
            assert "terbaru" in interaction.response.send_message.call_args.args[0]
            assert not dummy._blast_sending
        asyncio.run(check())

    def test_send_failure_and_chunk_resume(self):
        import asyncio
        from types import SimpleNamespace
        from unittest.mock import AsyncMock, Mock
        from src.discord_bot import MamazonBot
        from src.models import BlastRecord, BlastEntry
        async def check():
            record = BlastRecord(blast_id="id", sheet_name="PP Test", task_id="PP-03",
                                 entries=[BlastEntry(f"Member {i}", "Alpha", "", 5000) for i in range(80)], total_denda=400000)
            channel = SimpleNamespace(send=AsyncMock(side_effect=[None, RuntimeError("offline")]))
            dummy = SimpleNamespace(channel=lambda key: channel, blasts=SimpleNamespace(save=Mock()))
            try:
                await MamazonBot._send_blast_to_denda(dummy, record)
            except RuntimeError:
                pass
            else:
                raise AssertionError("delivery failure must propagate")
            assert record.sent_chunks == 1
            first_chunk = channel.send.call_args_list[0].args[0]
            channel.send = AsyncMock()
            await MamazonBot._send_blast_to_denda(dummy, record)
            assert all(call.args[0] != first_chunk for call in channel.send.call_args_list)
            dummy.channel = lambda key: None
            try:
                await MamazonBot._send_blast_to_denda(dummy, record)
            except RuntimeError:
                pass
            else:
                raise AssertionError("missing channel must fail")
        asyncio.run(check())
