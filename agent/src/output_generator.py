"""
Output artefacts.

  verification_report.csv — one row per (panitia, task); upserted, so
                            re-validation replaces the previous verdict.
  violations_log.csv      — one row per (panitia, task, check); stateful.
  blast_draft.md          — human-readable draft, grouped per divisi.
  audit_log.jsonl         — append-only decision record (Sources / Record).
  blasts/<id>.json        — blast approval state machine (DRAFT/HELD/SENT/…).

Upsert instead of blind append: Google Forms resubmits must not create duplicate
rows, and a resubmit that fixes a violation has to flip FLAGGED -> VERIFIED.
"""

from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import asdict
from datetime import datetime
from functools import wraps
from pathlib import Path
from threading import RLock
from typing import Any, Iterable, Sequence

from . import config
from .models import (
    BlastEntry,
    BlastRecord,
    BlastStatus,
    ResultStatus,
    ValidationBatch,
    ValidationResult,
)
from .utils import ensure_dir, format_rupiah, fmt_dt, truncate, wib_now

# Shared across instances: validators and Discord commands use the same paths.
OUTPUT_LOCK = RLock()


def _serialized(function):
    @wraps(function)
    def locked(*args, **kwargs):
        with OUTPUT_LOCK:
            return function(*args, **kwargs)

    return locked


VERIFICATION_COLUMNS = [
    "validated_at",
    "sheet_name",
    "task_id",
    "nama",
    "nim",
    "divisi",
    "ig_username",
    "status",
    "violations",
    "violation_detail",
    "total_denda",
    "evidence_links",
    "evidence_count",
    "confidence",
    "angel_card",
    "blast_date",
    "notes",
]

VIOLATION_COLUMNS = [
    "tanggal",
    "nama_komitee",
    "divisi",
    "id_tugas",
    "sheet_name",
    "rule_id",
    "jenis_pelanggaran",
    "denda",
    "status",
    "evidence_link",
    "catatan",
]


def _atomic_write_csv(path: Path, columns: Sequence[str], rows: Iterable[dict]) -> None:
    ensure_dir(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(columns), extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({column: row.get(column, "") for column in columns})
    tmp.replace(path)


@_serialized
def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


class OutputGenerator:
    def __init__(
        self,
        output_dir: Path | None = None,
        verification_path: Path | None = None,
        violations_path: Path | None = None,
        blast_draft_path: Path | None = None,
        audit_path: Path | None = None,
    ) -> None:
        self.output_dir = ensure_dir(Path(output_dir or config.OUTPUT_DIR))
        self.verification_path = Path(verification_path or config.VERIFICATION_REPORT_PATH)
        self.violations_path = Path(violations_path or config.VIOLATIONS_LOG_PATH)
        self.blast_draft_path = Path(blast_draft_path or config.BLAST_DRAFT_PATH)
        self.audit_path = Path(audit_path or config.AUDIT_LOG_PATH)

    # ── verification report ────────────────────────────────────────────────
    def _result_row(self, result: ValidationResult) -> dict[str, Any]:
        submission = result.submission
        return {
            "validated_at": (result.validated_at or wib_now()).strftime("%Y-%m-%d %H:%M:%S"),
            "sheet_name": submission.sheet_name,
            "task_id": result.task_id or submission.task_id or "",
            "nama": submission.nama,
            "nim": submission.nim,
            "divisi": submission.divisi,
            "ig_username": submission.ig_username,
            "status": result.status,
            "violations": ",".join(result.violation_ids) or "-",
            "violation_detail": truncate(result.violation_summary, 900),
            "total_denda": result.total_denda,
            "evidence_links": " | ".join(submission.links),
            "evidence_count": len(submission.links),
            "confidence": round(result.facts.confidence, 2) if result.facts else 0,
            "angel_card": result.angel_card_note or ("YES" if result.angel_card_exempt else ""),
            "blast_date": result.blast_date.strftime("%Y-%m-%d") if result.blast_date else "",
            "notes": truncate("; ".join(result.notes), 600),
        }

    @_serialized
    def write_verification(self, results: Sequence[ValidationResult]) -> int:
        existing = read_csv(self.verification_path)
        incoming = {self._key(self._result_row(result)): self._result_row(result) for result in results}

        merged: list[dict[str, Any]] = []
        for row in existing:
            key = self._key(row)
            if key in incoming:
                merged.append(incoming.pop(key))
            else:
                merged.append(row)
        merged.extend(incoming.values())

        _atomic_write_csv(self.verification_path, VERIFICATION_COLUMNS, merged)
        return len(incoming)

    @staticmethod
    def _key(row: dict[str, Any]) -> tuple[str, str, str]:
        return (
            str(row.get("sheet_name", "")).strip().lower(),
            str(row.get("nama", "")).strip().lower(),
            str(row.get("nim", "")).strip(),
        )

    def existing_statuses(self, sheet_name: str) -> dict[tuple[str, str, str], str]:
        statuses: dict[tuple[str, str, str], str] = {}
        for row in read_csv(self.verification_path):
            if str(row.get("sheet_name", "")).strip().lower() != sheet_name.strip().lower():
                continue
            statuses[self._key(row)] = row.get("status", "")
        return statuses

    def rows_for_sheet(self, sheet_name: str) -> list[dict[str, str]]:
        return [
            row
            for row in read_csv(self.verification_path)
            if str(row.get("sheet_name", "")).strip().lower() == sheet_name.strip().lower()
        ]

    def results_csv_bytes(self, results: Sequence[ValidationResult]) -> bytes:
        """CSV payload for a Discord attachment (one batch only)."""
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=VERIFICATION_COLUMNS, extrasaction="ignore")
        writer.writeheader()
        for result in results:
            writer.writerow(self._result_row(result))
        return buffer.getvalue().encode("utf-8-sig")

    # ── violations log ─────────────────────────────────────────────────────
    @_serialized
    def write_violations(self, results: Sequence[ValidationResult]) -> int:
        existing = read_csv(self.violations_path)
        index: dict[tuple[str, str, str, str], dict[str, Any]] = {}
        for row in existing:
            index[self._violation_key(row)] = row

        written = 0
        today = wib_now().strftime("%Y-%m-%d")

        for result in results:
            submission = result.submission
            task_id = result.task_id or submission.task_id or ""
            if result.status in (
                ResultStatus.PENDING_MANUAL_REVIEW, ResultStatus.ERROR, ResultStatus.EXEMPT
            ):
                for key, previous in index.items():
                    if (
                        key[0] == submission.nama.strip().lower()
                        and key[1] == task_id
                        and key[3] == submission.sheet_name.strip().lower()
                        and previous.get("status") == ResultStatus.FLAGGED
                    ):
                        previous["status"] = "WITHDRAWN"
                        previous["catatan"] = truncate(
                            f"Charge withdrawn after revalidation: {result.status}", 300
                        )
                        previous["tanggal"] = today
                continue

            for check in (result.violations if result.status == ResultStatus.FLAGGED else []):
                key = (
                    submission.nama.strip().lower(),
                    task_id,
                    check.check_id,
                    submission.sheet_name.strip().lower(),
                )
                row = {
                    "tanggal": today,
                    "nama_komitee": submission.nama,
                    "divisi": submission.divisi,
                    "id_tugas": task_id,
                    "sheet_name": submission.sheet_name,
                    "rule_id": check.check_id,
                    "jenis_pelanggaran": truncate(f"{check.check_id}: {check.detail}", 300),
                    "denda": check.denda,
                    "status": ResultStatus.FLAGGED,
                    "evidence_link": " | ".join(submission.links[:3]),
                    "catatan": truncate("; ".join(result.notes), 300),
                }
                previous = index.get(key)
                if previous:
                    # Preserve sent charges and previously resolved history.
                    if previous.get("status") in {"BLASTED", "RESOLVED"}:
                        continue
                    index[key] = row
                else:
                    index[key] = row
                written += 1

            # A previously flagged check that now passes becomes RESOLVED.
            for check in result.checks:
                if check.status not in {"PASS", "NOT_APPLICABLE"}:
                    continue
                key = (
                    submission.nama.strip().lower(),
                    task_id,
                    check.check_id,
                    submission.sheet_name.strip().lower(),
                )
                previous = index.get(key)
                if previous and previous.get("status") == ResultStatus.FLAGGED:
                    previous["status"] = "RESOLVED"
                    previous["catatan"] = truncate(
                        f"resubmit verified: {check.detail}", 300
                    )
                    previous["tanggal"] = today

        _atomic_write_csv(self.violations_path, VIOLATION_COLUMNS, index.values())
        return written

    @staticmethod
    def _violation_key(row: dict[str, Any]) -> tuple[str, str, str, str]:
        return (
            str(row.get("nama_komitee", "")).strip().lower(),
            str(row.get("id_tugas", "")).strip(),
            str(row.get("rule_id", "")).strip(),
            str(row.get("sheet_name", "")).strip().lower(),
        )

    def violation_history(self, nama: str) -> list[dict[str, str]]:
        target = nama.strip().lower()
        return [
            row
            for row in read_csv(self.violations_path)
            if str(row.get("nama_komitee", "")).strip().lower() == target
        ]

    def recurring_count(self, nama: str) -> int:
        return sum(
            1
            for row in self.violation_history(nama)
            if row.get("status") in {ResultStatus.FLAGGED, "BLASTED", "RESOLVED"}
        )

    @_serialized
    def mark_blasted(self, nama: str, task_id: str, sheet_name: str | None = None, rule_ids: Sequence[str] | None = None) -> None:
        rows = read_csv(self.violations_path)
        changed = False
        for row in rows:
            if (
                str(row.get("nama_komitee", "")).strip().lower() == nama.strip().lower()
                and str(row.get("id_tugas", "")).strip() == task_id
                and row.get("status") == ResultStatus.FLAGGED
                and (sheet_name is None or str(row.get("sheet_name", "")).strip().casefold() == sheet_name.strip().casefold())
                and (rule_ids is None or row.get("rule_id") in rule_ids)
            ):
                row["status"] = "BLASTED"
                changed = True
        if changed:
            _atomic_write_csv(self.violations_path, VIOLATION_COLUMNS, rows)

    def denda_rows(self, nama: str | None = None, include_resolved: bool = False) -> list[dict[str, str]]:
        rows = read_csv(self.violations_path)
        if nama:
            target = nama.strip().lower()
            rows = [
                row
                for row in rows
                if str(row.get("nama_komitee", "")).strip().lower() == target
            ]
        if not include_resolved:
            rows = [
                row for row in rows
                if row.get("status") in {ResultStatus.FLAGGED, "BLASTED"}
            ]
        return rows

    def denda_total(self, nama: str) -> int:
        total = 0
        for row in self.denda_rows(nama):
            try:
                total += int(float(row.get("denda") or 0))
            except ValueError:
                continue
        return total

    # ── blast draft ────────────────────────────────────────────────────────
    def build_blast_entries(self, batch: ValidationBatch) -> list[BlastEntry]:
        entries: list[BlastEntry] = []
        for result in batch.results:
            if result.status != ResultStatus.FLAGGED or not result.violations:
                continue
            entries.append(
                BlastEntry(
                    nama=result.submission.nama,
                    divisi=result.submission.divisi,
                    ig_username=result.submission.ig_username,
                    total_denda=result.total_denda,
                    violations=[
                        {
                            "rule_id": check.check_id,
                            "detail": check.detail,
                            "denda": check.denda,
                        }
                        for check in result.violations
                    ],
                    evidence_links=result.submission.links[:3],
                    recurring=self.recurring_count(result.submission.nama),
                )
            )
        entries.sort(key=lambda entry: (entry.divisi, -entry.total_denda, entry.nama))
        return entries

    @_serialized
    def write_blast_draft(self, batch: ValidationBatch) -> tuple[Path, list[BlastEntry]]:
        entries = self.build_blast_entries(batch)
        rekening = (
            self._rekening_from_rulebase() or "BCA 4731763318 a.n. Bertrand Immanuel Wijaya"
        )
        lines: list[str] = [
            f"# 📢 BLAST DENDA — {batch.sheet_name}",
            "",
            f"**Task**: {batch.task_id or '-'}  |  **Validated**: {fmt_dt(batch.finished_at or wib_now())}",
            f"**Total**: {len(entries)} panitia, {format_rupiah(sum(e.total_denda for e in entries))}",
            "",
            f"Transfer: {rekening}  |  Kode: 04 (Denda PP) / 05 (Denda Sponsor)",
            "Deadline bayar: Setiap hari Sabtu pukul 23.59 WIB",
            "",
        ]

        if not entries:
            lines.append("_Tidak ada denda untuk sheet ini._")

        current_divisi = None
        for index, entry in enumerate(entries, start=1):
            if entry.divisi != current_divisi:
                current_divisi = entry.divisi
                lines.extend(["", f"## {current_divisi or 'Tanpa divisi'}", ""])
            lines.append(
                f"{index}. **{entry.nama}** (@{entry.ig_username or '-'}) — "
                f"{format_rupiah(entry.total_denda)}"
            )
            for violation in entry.violations:
                lines.append(
                    f"   - {violation['rule_id']}: {violation['detail']} "
                    f"(-{format_rupiah(violation['denda'])})"
                )
            if entry.evidence_links:
                lines.append(f"   - Bukti: {entry.evidence_links[0]}")
            if entry.recurring >= config.RECURRING_THRESHOLD:
                lines.append(
                    f"   - ⚠️ RIWAYAT: {entry.recurring}x pelanggaran — perlu perhatian koor"
                )

        lines.extend(
            [
                "",
                "---",
                "Draft ini belum dikirim. Butuh approval koor di #blast-drafts.",
            ]
        )

        text = "\n".join(lines)
        ensure_dir(self.blast_draft_path.parent)

        # One file per sheet (history) + the "latest" convenience copy.
        slug = re.sub(r"[^a-z0-9]+", "_", batch.sheet_name.lower()).strip("_")
        per_sheet = self.blast_draft_path.with_name(f"blast_draft_{slug}.md")
        per_sheet.write_text(text, encoding="utf-8")
        self.blast_draft_path.write_text(text, encoding="utf-8")
        return per_sheet, entries

    def _rekening_from_rulebase(self) -> str:
        try:
            from .agent.knowledge import load_fines_policy
            fines = load_fines_policy()
            return f"{fines.get('bank', '')} {fines.get('nomor', '')} a.n. {fines.get('atas_nama', '')}".strip()
        except Exception:
            return "BCA 4731763318 a.n. Bertrand Immanuel Wijaya"

    # ── audit log ──────────────────────────────────────────────────────────
    @_serialized
    def audit(
        self,
        action: str,
        actor: str = "system",
        target: str = "",
        result: str = "",
        rationale: str = "",
        rule_ids: Sequence[str] | None = None,
        confidence: float = 0.0,
        extra: dict[str, Any] | None = None,
        timestamp: datetime | None = None,
    ) -> dict[str, Any]:
        entry = {
            "ts": (timestamp or wib_now()).isoformat(timespec="seconds"),
            "actor": actor,
            "action": action,
            "target": target,
            "result": result,
            "rationale": rationale,
            "rule_ids": list(rule_ids or []),
            "confidence": round(confidence, 2),
        }
        if extra:
            entry["extra"] = extra
        ensure_dir(self.audit_path.parent)
        with open(self.audit_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        try:
            from .memory_store import get_memory_store
            get_memory_store().record_execution(
                action=action, actor=actor, target=target, result=result,
                rationale=rationale, extra=extra or {},
            )
        except Exception:
            pass
        return entry

    @_serialized
    def read_audit(
        self,
        limit: int = 20,
        tanggal: str | None = None,
        user: str | None = None,
        nama: str | None = None,
    ) -> list[dict[str, Any]]:
        if not self.audit_path.exists():
            return []
        entries: list[dict[str, Any]] = []
        with open(self.audit_path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if tanggal and not str(entry.get("ts", "")).startswith(tanggal):
                    continue
                if user and user.lower() not in str(entry.get("actor", "")).lower():
                    continue
                if nama and nama.lower() not in json.dumps(entry, ensure_ascii=False).lower():
                    continue
                entries.append(entry)
        return entries[-limit:]

    # ── batch convenience ──────────────────────────────────────────────────
    @_serialized
    def write_batch(self, batch: ValidationBatch, actor: str = "agent") -> dict[str, Any]:
        if batch.cancelled:
            return {
                "verification_rows": 0,
                "violation_rows": 0,
                "blast_draft": "",
                "blast_entries": [],
            }
        new_rows = self.write_verification(batch.results)
        violations = self.write_violations(batch.results)
        draft_path, entries = self.write_blast_draft(batch)
        try:
            from .memory_store import get_memory_store
            get_memory_store().save_validation_batch(batch, actor=actor)
        except Exception:
            pass
        self.audit(
            action="validate",
            actor=actor,
            target=batch.sheet_name,
            result=(
                f"{batch.total} checked, {len(batch.flagged)} flagged, "
                f"{len(batch.exempt)} exempt"
            ),
            rationale=(
                f"merged {'rules + blast_context' if batch.blast_context_used else 'rules only'}"
            ),
            rule_ids=sorted({check.check_id for r in batch.results for check in r.violations}),
            confidence=round(
                sum((r.facts.confidence if r.facts else 0) for r in batch.results)
                / max(1, batch.total),
                2,
            ),
            extra={
                "task_id": batch.task_id,
                "divisi": batch.divisi,
                "total_denda": batch.total_denda,
                "skipped_existing": batch.skipped_existing,
                "duration_seconds": batch.duration_seconds,
                "duplicates": batch.duplicates[:20],
                "warnings": batch.warnings,
            },
        )
        return {
            "verification_rows": new_rows,
            "violation_rows": violations,
            "blast_draft": str(draft_path),
            "blast_entries": entries,
        }


# ─── Blast state machine ────────────────────────────────────────────────────
class BlastStore:
    """
    Persist blast drafts so /hold-blast and /release-blast survive a restart.

    Layout:
      output/blasts/index.json          {blast_id: summary}
      output/blasts/<blast_id>.json     full record
    """

    def __init__(self, directory: Path | None = None) -> None:
        self.directory = ensure_dir(Path(directory or config.BLAST_STORE_DIR))
        self.index_path = self.directory / "index.json"

    # ── index ──────────────────────────────────────────────────────────────
    def _read_index(self) -> dict[str, Any]:
        if not self.index_path.exists():
            return {}
        try:
            return json.loads(self.index_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _write_index(self, index: dict[str, Any]) -> None:
        self.index_path.write_text(json.dumps(index, indent=2), encoding="utf-8")

    def _path(self, blast_id: str) -> Path:
        return self.directory / f"{blast_id}.json"

    # ── CRUD ───────────────────────────────────────────────────────────────
    def save(self, record: BlastRecord) -> BlastRecord:
        record.created_at = record.created_at or wib_now()
        self._path(record.blast_id).write_text(
            json.dumps(record.to_dict(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        index = self._read_index()
        index[record.blast_id] = {
            "blast_id": record.blast_id,
            "sheet_name": record.sheet_name,
            "status": record.status,
            "created_at": record.created_at.isoformat() if record.created_at else "",
            "count": record.count,
            "total_denda": record.total_denda,
        }
        self._write_index(index)
        return record

    def get(self, blast_id: str) -> BlastRecord | None:
        path = self._path(blast_id)
        if not path.exists():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        return BlastRecord.from_dict(data)

    def latest_for_sheet(self, sheet_name: str) -> BlastRecord | None:
        candidates = [
            record
            for record in self.list_all()
            if record.sheet_name.strip().lower() == sheet_name.strip().lower()
        ]
        if not candidates:
            return None
        return sorted(
            candidates,
            key=lambda record: record.created_at or datetime.min,
            reverse=True,
        )[0]

    def list_all(self) -> list[BlastRecord]:
        records: list[BlastRecord] = []
        for path in self.directory.glob("blast_*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                records.append(BlastRecord.from_dict(data))
            except (OSError, json.JSONDecodeError):
                continue
        return records

    def list_by_status(self, status: str) -> list[BlastRecord]:
        return [record for record in self.list_all() if record.status == status]

    # ── transitions ────────────────────────────────────────────────────────
    def create(
        self,
        sheet_name: str,
        entries: Sequence[BlastEntry],
        task_id: str | None = None,
        created_by: str = "",
        blast_id: str | None = None,
    ) -> BlastRecord:
        from .utils import blast_id_now

        candidate = blast_id or blast_id_now()
        # Two /blast calls in the same second must not overwrite each other.
        if self._path(candidate).exists():
            suffix = 2
            while self._path(f"{candidate}_{suffix}").exists():
                suffix += 1
            candidate = f"{candidate}_{suffix}"

        record = BlastRecord(
            blast_id=candidate,
            sheet_name=sheet_name,
            task_id=task_id,
            status=BlastStatus.DRAFT,
            created_at=wib_now(),
            created_by=created_by,
            entries=list(entries),
            total_denda=sum(entry.total_denda for entry in entries),
        )
        return self.save(record)

    def approve(self, blast_id: str, actor: str) -> BlastRecord | None:
        record = self.get(blast_id)
        if record is None:
            return None
        record.status = BlastStatus.APPROVED
        record.approved_by = actor
        record.approved_at = wib_now()
        return self.save(record)

    def mark_sent(self, blast_id: str, channel_id: str = "", message_id: str = "") -> BlastRecord | None:
        record = self.get(blast_id)
        if record is None:
            return None
        record.status = BlastStatus.SENT
        record.channel_id = channel_id or record.channel_id
        record.message_id = message_id or record.message_id
        return self.save(record)

    def hold(self, blast_id: str, actor: str, reason: str = "") -> BlastRecord | None:
        record = self.get(blast_id)
        if record is None:
            return None
        record.status = BlastStatus.HELD
        record.held_by = actor
        record.held_reason = reason
        return self.save(record)

    def release(self, blast_id: str, actor: str) -> BlastRecord | None:
        record = self.get(blast_id)
        if record is None:
            return None
        record.status = BlastStatus.DRAFT
        record.held_by = ""
        record.held_reason = ""
        return self.save(record)

    def reject(self, blast_id: str, actor: str) -> BlastRecord | None:
        record = self.get(blast_id)
        if record is None:
            return None
        record.status = BlastStatus.REJECTED
        record.rejected_by = actor
        return self.save(record)

    def summary(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for record in self.list_all():
            counts[record.status] = counts.get(record.status, 0) + 1
        return counts
