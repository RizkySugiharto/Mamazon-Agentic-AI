"""
Data layer: spreadsheet reading, comma-split Drive link parsing, roster,
Angel Card registry, Bukti Tambahan and Evidence Vault tabs, plus fuzzy sheet
name resolution.

The root cause of problem #1 in the planning document is that Google Forms joins
multiple file uploads with ", " inside a single cell. `split_links()` handles
that; every downstream module uses `Submission.file_ids`, never the raw cell.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import config
from .models import Submission
from .sheets_client import (
    SheetSource,
    SheetSourceError,
    build_sheet_source,
    to_datetime,
    to_text,
)
from .utils import (
    best_name_match,
    extract_drive_file_id,
    normalize_name,
    normalize_sheet_name,
    parse_dt,
    sheet_similarity,
    split_links,
)


# ─── Column aliases ─────────────────────────────────────────────────────────
# Keys are canonical field names; values are normalized header variants.
COLUMN_ALIASES: dict[str, set[str]] = {
    "timestamp": {
        "timestamp", "waktu", "tanggal", "tanggal submit", "waktu pengisian",
        "timestamp submission", "submitted at", "waktu submit", "date",
    },
    "nama": {"nama lengkap", "nama", "name", "full name", "nama panitia"},
    "nim": {"nim", "nrp", "nomor induk mahasiswa"},
    "divisi": {"divisi", "division", "departemen", "department"},
    "ig": {
        "username instagram", "instagram", "ig", "username ig",
        "akun instagram", "username", "ig username", "instagram username",
    },
    "link": {
        "link bukti", "link bukti (google drive)", "bukti", "link",
        "upload bukti", "link google drive", "link drive", "file upload",
        "link bukti google drive", "bukti (link drive)",
    },
    "catatan": {"catatan", "notes", "keterangan", "note", "catatan pj"},
    "status": {"status", "status angel card", "status kartu"},
    "tanggal_beli": {"tanggal pembelian", "tanggal beli", "tgl pembelian"},
    "bukti_transfer": {"bukti transfer", "link bukti transfer"},
    "catatan_validasi": {"catatan validasi", "validasi"},
    "submission": {"submission", "nama submission", "tugas", "sheet"},
    "deskripsi": {"deskripsi", "description", "keterangan"},
    "pj": {"pj", "penanggung jawab", "validator"},
    "tugas": {"id tugas", "tugas", "task", "task id"},
    "pelanggaran": {"jenis pelanggaran", "pelanggaran", "violation"},
    "denda": {"denda", "fine", "total denda"},
    "evidence": {"evidence link", "link evidence", "link bukti"},
}


def normalize_header(text: Any) -> str:
    if text is None:
        return ""
    value = str(text).lower()
    value = re.sub(r"[_\-/\\]+", " ", value)
    value = re.sub(r"[^a-z0-9 ]", "", value)
    return re.sub(r"\s+", " ", value).strip()


def map_headers(headers: Sequence[Any]) -> dict[str, int]:
    """Map canonical field -> column index using the alias table."""
    mapping: dict[str, int] = {}
    normalized = [normalize_header(h) for h in headers]

    for field_name, aliases in COLUMN_ALIASES.items():
        # Exact alias match first, then substring containment.
        for index, header in enumerate(normalized):
            if header in aliases and field_name not in mapping:
                mapping[field_name] = index
        if field_name in mapping:
            continue
        for index, header in enumerate(normalized):
            if not header:
                continue
            if any(alias in header or header in alias for alias in aliases if len(alias) > 3):
                mapping[field_name] = index
                break
    return mapping


def _cell(row: Sequence[Any], index: int | None) -> Any:
    if index is None or index >= len(row):
        return None
    return row[index]


def _text(row: Sequence[Any], index: int | None) -> str:
    return to_text(_cell(row, index))


def _datetime(row: Sequence[Any], index: int | None):
    """Date-aware cell read: handles datetime, Sheets serial numbers, strings."""
    return to_datetime(_cell(row, index))


# Fields that must survive the trip from Sheets serial numbers to datetimes.
DATETIME_FIELDS = {"timestamp", "tanggal_beli"}


# ─── Resolution types ───────────────────────────────────────────────────────
@dataclass
class SheetResolution:
    query: str
    matches: list[tuple[str, float]] = field(default_factory=list)

    @property
    def resolved(self) -> str | None:
        if len(self.matches) == 1:
            return self.matches[0][0]
        return None

    @property
    def exact(self) -> bool:
        return bool(self.matches) and self.matches[0][1] >= 0.999

    @property
    def ambiguous(self) -> bool:
        if len(self.matches) < 2:
            return False
        return (self.matches[0][1] - self.matches[1][1]) < 0.06

    @property
    def not_found(self) -> bool:
        return not self.matches

    def options(self, limit: int = 5) -> list[str]:
        return [name for name, _ in self.matches[:limit]]


@dataclass
class TaskResolution:
    sheet_name: str
    task_id: str | None
    source: str = "none"          # rulebase | config | fuzzy | none
    warnings: list[str] = field(default_factory=list)


@dataclass
class RosterEntry:
    nama: str
    nim: str = ""
    divisi: str = ""
    ig: str = ""


@dataclass
class AngelEntry:
    nama: str
    divisi: str = ""
    status: str = "ACTIVE"
    tanggal_beli: datetime | None = None
    bukti: str = ""
    catatan: str = ""

    @property
    def active(self) -> bool:
        return self.status.strip().upper() in {"ACTIVE", "AKTIF", "YES", "Y", "TRUE"}


@dataclass
class SheetInfo:
    name: str
    rows: int
    multi_link: int
    no_link: int
    task_id: str | None
    task_source: str
    is_submission: bool


# ─── Data layer ─────────────────────────────────────────────────────────────
class DataLayer:
    """
    Read-only access to the submission workbook.

    Works against a local .xlsx or a live Google Sheet; the source is chosen by
    SHEET_SOURCE (or injected directly for tests).
    """

    def __init__(
        self,
        path: Path | None = None,
        rulebase: dict | None = None,
        sheet_task_map: dict[str, str | None] | None = None,
        source: SheetSource | None = None,
        spreadsheet_id: str | None = None,
    ) -> None:
        self.source = source or build_sheet_source(
            spreadsheet_id=spreadsheet_id, path=path
        )
        self.path = getattr(self.source, "path", None)
        self.rulebase = rulebase or {}
        self.sheet_task_map = (
            sheet_task_map if sheet_task_map is not None else config.SHEET_TO_TASK
        )
        self._cache: dict[str, list[Submission]] = {}

    @property
    def label(self) -> str:
        """Human-readable source name for CLI/embeds."""
        return self.source.label or self.source.name

    @property
    def warnings(self) -> list[str]:
        """Active source fallback warnings, including resource and snapshot time."""
        return list(getattr(self.source, "warnings", []))

    def close(self) -> None:
        self._cache.clear()
        self.source.close()

    def __enter__(self) -> "DataLayer":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def sheet_names(self) -> list[str]:
        return list(self.source.sheet_names())

    def submission_sheets(self) -> list[str]:
        return [
            name for name in self.sheet_names() if not config.is_registry_sheet(name)
        ]

    def find_sheet(self, *candidates: str) -> str | None:
        """
        Locate a registry tab by name, tolerating variants.

        "Roster" vs "Roster Panitia", "Angel Card Registry" vs "Angel Card" —
        the workbook decides the exact wording, so match on containment.
        """
        normalized = [normalize_sheet_name(c) for c in candidates if c]
        names = self.sheet_names()
        for name in names:
            current = normalize_sheet_name(name)
            if current in normalized:
                return name
        for name in names:
            current = normalize_sheet_name(name)
            if any(candidate and candidate in current for candidate in normalized):
                return name
        return None

    # ── generic row reader ─────────────────────────────────────────────────
    def read_rows(self, sheet_name: str) -> tuple[list[str], list[list[Any]]]:
        """Return (headers, data rows). Detects the header row within 5 rows."""
        rows = self.source.rows(sheet_name)
        if not rows:
            return [], []

        header_index = 0
        best_hits = -1
        for index, row in enumerate(rows[:5]):
            hits = len(map_headers(row))
            if hits > best_hits:
                best_hits, header_index = hits, index
        headers = [str(h) if h is not None else "" for h in rows[header_index]]
        return headers, rows[header_index + 1 :]

    def read_table(self, sheet_name: str) -> list[dict[str, str]]:
        headers, rows = self.read_rows(sheet_name)
        mapping = map_headers(headers)
        output: list[dict[str, str]] = []
        for row in rows:
            if not any(value not in (None, "") for value in row):
                continue
            record: dict[str, str] = {}
            for field_name, index in mapping.items():
                if field_name in DATETIME_FIELDS:
                    moment = _datetime(row, index)
                    record[field_name] = (
                        moment.strftime("%Y-%m-%d %H:%M:%S")
                        if moment
                        else _text(row, index)
                    )
                else:
                    record[field_name] = _text(row, index)
            record["_raw"] = [_text(row, i) for i in range(len(headers))]
            output.append(record)
        return output

    # ── submissions ────────────────────────────────────────────────────────
    def read_submissions(
        self, sheet_name: str, force_reload: bool = False
    ) -> list[Submission]:
        cache_parsed = self.source.name != "gsheets"
        if cache_parsed and not force_reload and sheet_name in self._cache:
            return self._cache[sheet_name]

        headers, rows = self.read_rows(sheet_name)
        mapping = map_headers(headers)
        if "nama" not in mapping:
            raise ValueError(
                f"Sheet '{sheet_name}' has no recognizable name column. "
                f"Headers seen: {headers}"
            )

        submissions: list[Submission] = []
        for row_index, row in enumerate(rows, start=2):
            nama = _text(row, mapping.get("nama"))
            if not nama:
                continue
            raw_link = _text(row, mapping.get("link"))
            links = split_links(raw_link)
            file_ids: list[str] = []
            for link in links:
                file_id = extract_drive_file_id(link)
                if file_id and file_id not in file_ids:
                    file_ids.append(file_id)

            submission = Submission(
                row_index=row_index,
                nama=nama,
                nim=_text(row, mapping.get("nim")),
                divisi=_text(row, mapping.get("divisi")),
                ig_username=_text(row, mapping.get("ig")),
                raw_link_cell=raw_link,
                links=links,
                file_ids=file_ids,
                timestamp=_datetime(row, mapping.get("timestamp")),
                sheet_name=sheet_name,
                raw={
                    "catatan": _text(row, mapping.get("catatan")),
                    "headers": headers,
                },
            )
            submissions.append(submission)

        if cache_parsed:
            self._cache[sheet_name] = submissions
        return submissions

    # ── sheet -> task resolution ───────────────────────────────────────────
    def task_sheet_index(self) -> dict[str, str]:
        """normalized(task.sheet) -> task id, from the rulebase."""
        index: dict[str, str] = {}
        for key in ("tugas_pp", "tugas_buzzer"):
            for task in self.rulebase.get(key, []) or []:
                sheet = task.get("sheet")
                if sheet:
                    index[normalize_sheet_name(sheet)] = task["id"]
        return index

    def resolve_task(self, sheet_name: str) -> TaskResolution:
        resolution = TaskResolution(sheet_name=sheet_name, task_id=None)
        normalized = normalize_sheet_name(sheet_name)

        rulebase_index = self.task_sheet_index()
        if normalized in rulebase_index:
            resolution.task_id = rulebase_index[normalized]
            resolution.source = "rulebase"
        elif sheet_name in self.sheet_task_map:
            resolution.task_id = self.sheet_task_map[sheet_name]
            resolution.source = "config"
        else:
            for key, task_id in rulebase_index.items():
                if sheet_similarity(sheet_name, key) >= 0.9:
                    resolution.task_id = task_id
                    resolution.source = "fuzzy"
                    break

        # Report conflicts between the two sources instead of silently picking.
        mapped = self.sheet_task_map.get(sheet_name)
        if resolution.task_id and mapped and mapped != resolution.task_id:
            resolution.warnings.append(
                f"Sheet '{sheet_name}' maps to {resolution.task_id} via "
                f"{resolution.source} but config.SHEET_TO_TASK says {mapped}"
            )

        if not resolution.task_id:
            resolution.warnings.append(
                f"No rulebase task matched sheet '{sheet_name}'. "
                "Validation will run with generic checks only."
            )
        return resolution

    def resolve_sheet(self, query: str, threshold: float = 0.62) -> SheetResolution:
        """
        Fuzzy-match a user supplied sheet name against submission sheets.

        Returns every candidate above `threshold`, sorted. Callers must handle
        ambiguity (never guess) — see validator.validate_sheet.
        """
        resolution = SheetResolution(query=query)
        if not query or not str(query).strip():
            return resolution

        sheets = self.submission_sheets()
        normalized_query = normalize_sheet_name(query)

        for name in sheets:
            if normalize_sheet_name(name) == normalized_query:
                return SheetResolution(query=query, matches=[(name, 1.0)])

        scored = [
            (name, sheet_similarity(query, name))
            for name in sheets
        ]
        scored = [pair for pair in scored if pair[1] >= threshold]
        scored.sort(key=lambda pair: pair[1], reverse=True)
        resolution.matches = scored
        return resolution

    # ── roster & registries ────────────────────────────────────────────────
    def load_roster(self) -> list[RosterEntry]:
        sheet = self.find_sheet(config.ROSTER_SHEET, "roster", "roster panitia")
        if sheet is None:
            return []
        entries: list[RosterEntry] = []
        for record in self.read_table(sheet):
            nama = record.get("nama", "")
            if not nama:
                continue
            entries.append(
                RosterEntry(
                    nama=nama,
                    nim=record.get("nim", ""),
                    divisi=record.get("divisi", ""),
                    ig=record.get("ig", ""),
                )
            )
        return entries

    def find_person(self, query: str, threshold: float = 0.82):
        """Return (RosterEntry, score) for the closest roster match."""
        roster = self.load_roster()
        if not roster:
            return None, 0.0
        name, score = best_name_match(query, [entry.nama for entry in roster], threshold)
        if not name:
            return None, 0.0
        for entry in roster:
            if entry.nama == name:
                return entry, score
        return None, 0.0

    def load_angel_registry(self) -> list[AngelEntry]:
        entries: list[AngelEntry] = []
        sheet = self.find_sheet(config.ANGEL_CARD_SHEET, "angel card", "angel")
        if sheet is not None:
            for record in self.read_table(sheet):
                nama = record.get("nama", "")
                if not nama:
                    continue
                entries.append(
                    AngelEntry(
                        nama=nama,
                        divisi=record.get("divisi", ""),
                        status=record.get("status", "ACTIVE") or "ACTIVE",
                        tanggal_beli=parse_dt(record.get("tanggal_beli"), default_time=(0, 0)),
                        bukti=record.get("bukti_transfer", ""),
                        catatan=record.get("catatan_validasi", ""),
                    )
                )
        return apply_angel_overrides(entries)

    def load_bukti_tambahan(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        sheet = self.find_sheet(config.BUKTI_TAMBAHAN_SHEET, "bukti tambahan")
        if sheet is not None:
            for record in self.read_table(sheet):
                if not record.get("ig") and not record.get("submission"):
                    continue
                rows.append(
                    {
                        "timestamp": parse_dt(record.get("timestamp")),
                        "submission": record.get("submission", ""),
                        "ig": record.get("ig", ""),
                        "deskripsi": record.get("deskripsi", ""),
                        "link": record.get("link", ""),
                        "pj": record.get("pj", ""),
                    }
                )

        # Entries uploaded through Discord live in an append-only JSONL file.
        log_path = config.OUTPUT_DIR / "bukti_tambahan.jsonl"
        if log_path.exists():
            import json as _json

            try:
                with open(log_path, encoding="utf-8") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            entry = _json.loads(line)
                        except _json.JSONDecodeError:
                            continue
                        rows.append(
                            {
                                "timestamp": parse_dt(entry.get("timestamp")),
                                "submission": entry.get("submission", ""),
                                "ig": entry.get("ig", ""),
                                "deskripsi": entry.get("deskripsi", ""),
                                "link": entry.get("link", ""),
                                "pj": entry.get("pj", ""),
                            }
                        )
            except OSError:
                pass
        return rows

    def load_evidence_vault(self) -> list[dict[str, Any]]:
        if config.EVIDENCE_VAULT_SHEET not in self.sheet_names():
            return []
        rows: list[dict[str, Any]] = []
        for record in self.read_table(config.EVIDENCE_VAULT_SHEET):
            if not record.get("nama"):
                continue
            rows.append(
                {
                    "tanggal": parse_dt(record.get("timestamp") or record.get("tanggal_beli")),
                    "nama": record.get("nama", ""),
                    "divisi": record.get("divisi", ""),
                    "tugas": record.get("tugas", ""),
                    "pelanggaran": record.get("pelanggaran", ""),
                    "denda": int(float(record.get("denda") or 0)),
                    "link": record.get("evidence", ""),
                    "catatan": record.get("catatan", ""),
                }
            )
        return rows

    # ── aggregate info for /list-sheets & CLI ──────────────────────────────
    def describe_sheets(self) -> list[SheetInfo]:
        infos: list[SheetInfo] = []
        for name in self.sheet_names():
            is_submission = not config.is_registry_sheet(name)
            if not is_submission:
                infos.append(
                    SheetInfo(name=name, rows=0, multi_link=0, no_link=0,
                              task_id=None, task_source="registry", is_submission=False)
                )
                continue
            try:
                submissions = self.read_submissions(name)
            except Exception:
                continue
            task = self.resolve_task(name)
            infos.append(
                SheetInfo(
                    name=name,
                    rows=len(submissions),
                    multi_link=sum(1 for s in submissions if s.multi_link),
                    no_link=sum(1 for s in submissions if not s.has_evidence),
                    task_id=task.task_id,
                    task_source=task.source,
                    is_submission=True,
                )
            )
        return infos

    def dataset_stats(self) -> dict[str, Any]:
        infos = [info for info in self.describe_sheets() if info.is_submission]
        rows = sum(info.rows for info in infos)
        multi = sum(info.multi_link for info in infos)
        return {
            "sheets": len(infos),
            "rows": rows,
            "multi_link": multi,
            "multi_ratio": round(multi / rows, 3) if rows else 0.0,
            "no_link": sum(info.no_link for info in infos),
            "panitia": len(self.load_roster()),
            "angel_cards": len(self.load_angel_registry()),
        }


# ─── Module level helpers (stateless convenience) ───────────────────────────
ANGEL_OVERRIDE_PATH = config.OUTPUT_DIR / "angel_card_overrides.jsonl"


def append_angel_override(
    nama: str,
    divisi: str = "",
    tanggal_beli: datetime | None = None,
    status: str = "ACTIVE",
    catatan: str = "",
    actor: str = "",
    removed: bool = False,
) -> None:
    """
    Record an /add-angel or /remove-angel change.

    The registry sheet stays read-only (per the rulebase: only BPH + Koor Spade
    may write). Changes go to an append-only overlay that is merged on load, so
    every edit is attributable and reversible.
    """
    import json as _json

    ensure_dir(ANGEL_OVERRIDE_PATH.parent)
    entry = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "nama": nama,
        "divisi": divisi,
        "status": status,
        "tanggal_beli": tanggal_beli.strftime("%Y-%m-%d") if tanggal_beli else "",
        "catatan": catatan,
        "actor": actor,
        "removed": removed,
    }
    with open(ANGEL_OVERRIDE_PATH, "a", encoding="utf-8") as handle:
        handle.write(_json.dumps(entry, ensure_ascii=False) + "\n")


def apply_angel_overrides(entries: list[AngelEntry]) -> list[AngelEntry]:
    import json as _json

    if not ANGEL_OVERRIDE_PATH.exists():
        return entries

    by_key: dict[str, AngelEntry] = {normalize_name(entry.nama): entry for entry in entries}
    order: list[str] = [normalize_name(entry.nama) for entry in entries]

    try:
        with open(ANGEL_OVERRIDE_PATH, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    record = _json.loads(line)
                except _json.JSONDecodeError:
                    continue
                key = normalize_name(record.get("nama", ""))
                if not key:
                    continue
                if record.get("removed"):
                    by_key.pop(key, None)
                    continue
                entry = AngelEntry(
                    nama=record.get("nama", ""),
                    divisi=record.get("divisi", ""),
                    status=record.get("status", "ACTIVE") or "ACTIVE",
                    tanggal_beli=parse_dt(record.get("tanggal_beli"), default_time=(0, 0)),
                    catatan=record.get("catatan", ""),
                )
                if key not in by_key:
                    order.append(key)
                by_key[key] = entry
    except OSError:
        return entries

    return [by_key[key] for key in order if key in by_key]


def ensure_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def group_duplicates(submissions: Iterable[Submission]) -> dict[tuple[str, str], list[Submission]]:
    """Group rows by (name, nim) — duplicates mean resubmit or spam."""
    grouped: dict[tuple[str, str], list[Submission]] = {}
    for submission in submissions:
        key = (normalize_name(submission.nama), submission.nim.strip())
        grouped.setdefault(key, []).append(submission)
    return grouped


def dedupe_submissions(
    submissions: Sequence[Submission],
) -> tuple[list[Submission], list[str]]:
    """
    Keep the latest row per (name, nim) and describe what was dropped.

    Google Forms appends on every resubmit, so the last timestamp wins.
    """
    grouped = group_duplicates(submissions)
    kept: list[Submission] = []
    notes: list[str] = []

    for key, rows in grouped.items():
        if len(rows) == 1:
            kept.append(rows[0])
            continue
        ordered = sorted(
            rows,
            key=lambda item: item.timestamp or datetime.min,
        )
        latest = ordered[-1]
        kept.append(latest)
        stamps = ", ".join(
            (item.timestamp.strftime("%d/%m %H:%M") if item.timestamp else "?")
            for item in ordered
        )
        notes.append(
            f"{latest.nama} submitted {len(rows)}x ({stamps}); using the latest row "
            f"{latest.row_index}"
        )

    kept.sort(key=lambda item: item.row_index)
    return kept, notes
