"""
Bukti Tambahan handler: PJ uploads a story screenshot to #bukti-tambahan with a
caption in the format

    [Submission] | [IG] | [Deskripsi]

The bot validates the format and the submission name against the rulebase,
stores the entry in output/bukti_tambahan.jsonl (append-only, so it survives
restarts), and the validator cross-references it during C7.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from . import config
from .data_layer import DataLayer
from .utils import (
    normalize_handle,
    parse_dt,
    sheet_similarity,
    truncate,
    wib_now,
)

BUKTI_LOG_PATH = config.OUTPUT_DIR / "bukti_tambahan.jsonl"


@dataclass
class ParsedBukti:
    submission: str
    ig: str
    deskripsi: str
    valid: bool = True
    error: str = ""


@dataclass
class BuktiResult:
    ok: bool
    message: str
    entry: dict[str, Any] | None = None
    suggestions: list[str] | None = None


def parse_caption(caption: str | None) -> ParsedBukti:
    """Parse 'PP Feed 26 Oktober | @starfours_ | Story 24h masih live'."""
    text = (caption or "").strip()
    if not text:
        return ParsedBukti("", "", "", valid=False, error="caption kosong")

    parts = [part.strip() for part in re.split(r"\s*[|•]\s*", text) if part.strip()]
    if len(parts) >= 3:
        return ParsedBukti(parts[0], parts[1], " | ".join(parts[2:]))

    # Fallback: find an @handle anywhere and treat the rest as the description.
    handle = re.search(r"@[0-9A-Za-z_.]{2,}", text)
    if handle and len(parts) >= 2:
        submission = text[: handle.start()].strip(" |-")
        deskripsi = text[handle.end() :].strip(" |-")
        if submission:
            return ParsedBukti(submission, handle.group(0), deskripsi)

    return ParsedBukti(
        parts[0] if parts else "",
        "",
        "",
        valid=False,
        error="format tidak lengkap (butuh: Submission | @ig | Deskripsi)",
    )


def append_bukti(entry: dict[str, Any], path: Path | None = None) -> None:
    target = Path(path or BUKTI_LOG_PATH)
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "a", encoding="utf-8") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_bukti_log(path: Path | None = None) -> list[dict[str, Any]]:
    target = Path(path or BUKTI_LOG_PATH)
    if not target.exists():
        return []
    rows: list[dict[str, Any]] = []
    with open(target, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                rows.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return rows


class BuktiTambahanHandler:
    def __init__(self, data: DataLayer | None = None, path: Path | None = None) -> None:
        self.data = data or DataLayer(rulebase=_rulebase())
        self.path = Path(path or BUKTI_LOG_PATH)
        self.rulebase = _rulebase()

    # ── validation ─────────────────────────────────────────────────────────
    def submission_options(self) -> list[str]:
        sheets = [
            info.name for info in self.data.describe_sheets() if info.is_submission
        ]
        return sheets

    def resolve_submission(self, name: str) -> tuple[str | None, list[str]]:
        options = self.submission_options()
        scored = sorted(
            ((option, sheet_similarity(name, option)) for option in options),
            key=lambda pair: pair[1],
            reverse=True,
        )
        if scored and scored[0][1] >= 0.9:
            return scored[0][0], []
        return None, [option for option, score in scored[:4] if score > 0.4]

    def handle(
        self,
        caption: str,
        link: str,
        pj: str,
        actor: str = "",
        timestamp: datetime | None = None,
    ) -> BuktiResult:
        parsed = parse_caption(caption)
        if not parsed.valid:
            return BuktiResult(
                ok=False,
                message=(
                    "⚠️ Format bukti tambahan tidak lengkap.\n\n"
                    "Format: `[Submission] | [IG] | [Deskripsi]`\n"
                    "Contoh: `PP Feed 26 Oktober 2026 | @starfours_ | Story 24h masih live`"
                ),
            )

        submission, suggestions = self.resolve_submission(parsed.submission)
        if submission is None:
            listing = "\n".join(f"• {option}" for option in suggestions) or "• (kosong)"
            return BuktiResult(
                ok=False,
                message=(
                    f"❌ Submission '{parsed.submission}' tidak ditemukan.\n\n"
                    f"Tugas yang tersedia:\n{listing}\n\n"
                    "Mohon perbaiki nama submission dan upload ulang."
                ),
                suggestions=suggestions,
            )

        handle = normalize_handle(parsed.ig)
        if not handle:
            return BuktiResult(
                ok=False,
                message="❌ Username Instagram tidak valid. Contoh: @starfours_",
            )

        entry = {
            "timestamp": (timestamp or wib_now()).isoformat(timespec="seconds"),
            "submission": submission,
            "ig": handle,
            "deskripsi": truncate(parsed.deskripsi, 300),
            "link": link,
            "pj": pj or actor,
        }
        if hasattr(self.data, "append_bukti_tambahan"):
            try:
                self.data.append_bukti_tambahan(entry)
            except Exception:
                pass
        append_bukti(entry, self.path)

        return BuktiResult(
            ok=True,
            message=(
                "✅ Bukti tambahan diterima!\n\n"
                f"📋 Submission: {submission}\n"
                f"👤 IG: @{handle}\n"
                f"📝 Deskripsi: {entry['deskripsi'] or '-'}\n\n"
                "Bukti ini akan dipakai saat validasi C7 (Story 24 jam)."
            ),
            entry=entry,
        )

    def list_for_sheet(self, sheet_name: str) -> list[dict[str, Any]]:
        rows = [
            row
            for row in (self.data.load_bukti_tambahan() + read_bukti_log(self.path))
            if sheet_similarity(str(row.get("submission", "")), sheet_name) >= 0.9
        ]
        return rows


def _rulebase() -> dict[str, Any]:
    return {}
