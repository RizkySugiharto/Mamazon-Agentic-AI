"""
Validator orchestrator.

One call to `Validator.validate("PP Feed 26 Oktober")` performs the full
pipeline from planning §7:

    resolve sheet -> resolve task -> merge rulebase + blast_context
      -> Angel Card check -> dedupe/resubmit -> download evidence
      -> Vision facts -> checks -> CSV / audit / blast draft

Errors are typed (`SheetNotFoundError`, `SheetAmbiguousError`) so the Discord
layer can render the exact unhappy flows from the workflow document.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from threading import Event, RLock
from typing import Any, Callable, Sequence

from . import config
from .blast_reader import BlastContextStore, load_contexts
from .data_layer import DataLayer, RosterEntry, dedupe_submissions
from .image_manager import ImageManager
from .models import (
    BlastContext,
    ResultStatus,
    Submission,
    ValidationBatch,
    ValidationResult,
    VisionFacts,
)
from .output_generator import OUTPUT_LOCK, OutputGenerator
from .rulebase_engine import LintIssue, MergedContext, RulebaseEngine, lint_rulebase
from .utils import (
    handle_similarity,
    normalize_handle,
    normalize_name,
    wib_now,
)
from .vision_checker import VisionHints, build_vision_backend

ProgressCallback = Callable[[str, int, int, str], None]


# ─── Errors ─────────────────────────────────────────────────────────────────
class ValidationError(Exception):
    pass


class SheetNotFoundError(ValidationError):
    def __init__(self, query: str, options: Sequence[str]) -> None:
        self.query = query
        self.options = list(options)
        super().__init__(f"Sheet '{query}' tidak ditemukan")


class SheetAmbiguousError(ValidationError):
    def __init__(self, query: str, options: Sequence[str], scores: dict[str, float] | None = None) -> None:
        self.query = query
        self.options = list(options)
        self.scores = scores or {}
        super().__init__(f"Sheet '{query}' ambigu ({len(self.options)} kandidat)")


class DivisionNotFoundError(ValidationError):
    def __init__(self, query: str, options: Sequence[str]) -> None:
        self.query = query
        self.options = list(options)
        super().__init__(
            f"Unknown division '{query}'. Available divisions: "
            + (", ".join(self.options) or "none")
        )


class _ValidationCancelled(Exception):
    pass


# ─── Validator ──────────────────────────────────────────────────────────────
class Validator:
    def __init__(
        self,
        data_layer: DataLayer | None = None,
        engine: RulebaseEngine | None = None,
        vision=None,
        images: ImageManager | None = None,
        outputs: OutputGenerator | None = None,
        blast_context: dict[str, BlastContext] | None = None,
    ) -> None:
        self.rulebase = _load_rulebase()
        self.data = data_layer or DataLayer(rulebase=self.rulebase)
        self.blast_context = (
            blast_context if blast_context is not None else load_contexts()
        )
        self.engine = engine or RulebaseEngine(self.rulebase, self.blast_context)
        self.engine.blast_context = self.blast_context
        self.vision = vision or build_vision_backend()
        self.images = images or ImageManager()
        self.outputs = outputs or OutputGenerator()
        # Different task workers share a Drive client/image index and Vision client.
        self._adapter_lock = RLock()

    # ── helpers ────────────────────────────────────────────────────────────
    def reload_context(self) -> dict[str, BlastContext]:
        self.blast_context = load_contexts()
        self.engine.blast_context = self.blast_context
        return self.blast_context

    def lint(self) -> list[LintIssue]:
        return lint_rulebase(self.rulebase)

    def _resolve_sheet(self, query: str) -> tuple[str, Any]:
        resolution = self.data.resolve_sheet(query)
        if resolution.not_found:
            options = [info.name for info in self.data.describe_sheets() if info.is_submission]
            raise SheetNotFoundError(query, options)
        if resolution.ambiguous or resolution.resolved is None:
            raise SheetAmbiguousError(
                query,
                resolution.options(),
                {name: score for name, score in resolution.matches},
            )
        return resolution.resolved, resolution

    def _hints(self, submission: Submission, merged: MergedContext) -> VisionHints:
        return VisionHints(
            task_id=merged.task_id,
            task_type=merged.task_type,
            nama=submission.nama,
            ig_username=submission.ig_username,
            submitted_at=submission.timestamp,
            platforms=merged.platforms,
            caption_reference=merged.caption_reference,
            required_mentions=merged.tags,
            required_hashtags=merged.hashtags,
            jadwal_post=merged.jadwal_post,
            deadline=merged.deadline,
            target_url=merged.target_url,
            expected_slides=merged.expected_slides,
            story_required=merged.is_story,
            feed_required=merged.is_feed,
            blast_notes=list(merged.blast.notes) if merged.blast else [],
        )

    def _bukti_index(self, sheet_name: str) -> dict[str, str]:
        """IG handle -> PJ description, for C7 cross-reference."""
        index: dict[str, str] = {}
        for row in self.data.load_bukti_tambahan():
            submission_name = str(row.get("submission", ""))
            if submission_name and not _sheet_mentions(submission_name, sheet_name):
                continue
            handle = normalize_handle(str(row.get("ig", "")))
            if handle:
                index[handle] = str(row.get("deskripsi", ""))
        return index

    # ── preview (no vision, no download) ───────────────────────────────────
    def preview(self, sheet_query: str) -> dict[str, Any]:
        sheet_name, _ = self._resolve_sheet(sheet_query)
        task = self.data.resolve_task(sheet_name)
        submissions = self.data.read_submissions(sheet_name)
        deduped, duplicates = dedupe_submissions(submissions)
        merged = self.engine.merge(task.task_id)
        return {
            "sheet_name": sheet_name,
            "task_id": task.task_id,
            "task_source": task.source,
            "task_type": merged.task_type,
            "rows": len(submissions),
            "unique_rows": len(deduped),
            "multi_link": sum(1 for row in deduped if row.multi_link),
            "no_link": sum(1 for row in deduped if not row.has_evidence),
            "duplicates": duplicates,
            "blast_context": bool(self.engine.blast_context.get(task.task_id)),
            "warnings": merged.warnings + task.warnings,
            "checks": self.engine.applicable_checks(task.task_id),
            "platforms": merged.platforms,
            "deadline": merged.deadline,
            "jadwal_post": merged.jadwal_post,
        }

    # ── main pipeline ──────────────────────────────────────────────────────
    def validate(
        self,
        sheet_query: str,
        force: bool = False,
        limit: int | None = None,
        progress: ProgressCallback | None = None,
        actor: str = "agent",
        skip_verified: bool = True,
        *,
        divisi: str = "",
        cancel_event: Event | None = None,
        resolved_sheet: str | None = None,
    ) -> ValidationBatch:
        batch = ValidationBatch(
            sheet_name=resolved_sheet if resolved_sheet is not None else sheet_query,
            task_id=None,
            task_type="",
            started_at=wib_now(),
            divisi=" ".join(divisi.split()),
        )

        def checkpoint() -> None:
            if cancel_event is not None and cancel_event.is_set():
                raise _ValidationCancelled()

        try:
            checkpoint()
            sheet_name = (
                resolved_sheet if resolved_sheet is not None
                else self._resolve_sheet(sheet_query)[0]
            )
            batch.sheet_name = sheet_name
            checkpoint()
            task = self.data.resolve_task(sheet_name)
            batch.task_id = task.task_id
            checkpoint()

            _emit(progress, "resolve", 0, 1, f"Sheet: {sheet_name} | Task: {task.task_id or '-'}")
            checkpoint()
            submissions = self.data.read_submissions(sheet_name)
            checkpoint()
            roster = []
            if batch.divisi or any(not row.divisi.strip() for row in submissions):
                roster = self.data.load_roster()
                checkpoint()
            # Choose the latest resubmit globally before filtering its division.
            deduped, batch.duplicates = dedupe_submissions(submissions)
            deduped, batch.divisi = _filter_division(deduped, roster, batch.divisi)
            checkpoint()

            merged = self.engine.merge(task.task_id)
            batch.task_type = merged.task_type
            batch.blast_context_used = bool(merged.blast and merged.blast.blast_message_id)
            batch.blast_message_id = merged.blast.blast_message_id if merged.blast else None
            batch.warnings.extend(merged.warnings)
            batch.warnings.extend(task.warnings)
            if task.task_id and not merged.blast:
                batch.warnings.append(
                    "Ketentuan tambahan dari #blast-tugas belum di-extract untuk "
                    f"{task.task_id}. Validasi memakai rulebase saja — jalankan "
                    "/scan-blast lalu re-validate untuk hasil lengkap."
                )

            checkpoint()
            existing = self.outputs.existing_statuses(sheet_name) if skip_verified else {}
            checkpoint()
            angel_registry = self.data.load_angel_registry()
            checkpoint()
            bukti_index = self._bukti_index(sheet_name)
            checkpoint()
            batch.warnings.extend(
                warning
                for warning in getattr(getattr(self.data, "source", None), "freshness_warnings", [])
                if warning not in batch.warnings
            )

            total = len(deduped)
            if limit:
                total = min(total, limit)

            for index, submission in enumerate(deduped[:total], start=1):
                checkpoint()
                key = (
                    sheet_name.strip().lower(),
                    submission.nama.strip().lower(),
                    submission.nim.strip(),
                )
                if existing.get(key) in (ResultStatus.VERIFIED, ResultStatus.EXEMPT) and not force:
                    batch.skipped_existing += 1
                    _emit(progress, "skip", index, total, f"{submission.nama}: sudah {existing.get(key)}")
                    continue

                _emit(progress, "angel", index, total, f"{submission.nama}: cek Angel Card")
                checkpoint()
                exempt, angel_note = self.engine.angel_exemption(
                    submission.nama, task.task_id, angel_registry, merged.blast_date
                )
                checkpoint()
                if exempt:
                    result = ValidationResult(
                        submission=submission,
                        status=ResultStatus.EXEMPT,
                        task_id=task.task_id,
                        task_type=merged.task_type,
                        angel_card_exempt=True,
                        angel_card_note=angel_note,
                        blast_date=merged.blast_date,
                        validated_at=wib_now(),
                    )
                    batch.results.append(result)
                    continue

                _emit(progress, "download", index, total, f"{submission.nama}: unduh bukti")
                checkpoint()
                with self._adapter_lock:
                    checkpoint()
                    images = (
                        self.images.fetch_many(submission.file_ids)
                        if submission.file_ids
                        else []
                    )
                checkpoint()
                failures = [image.error for image in images if image.error]
                if failures:
                    batch.warnings.append(
                        f"{submission.nama}: {len(failures)} bukti gagal diunduh "
                        f"({failures[0][:80]})"
                    )

                _emit(progress, "vision", index, total, f"{submission.nama}: baca screenshot")
                checkpoint()
                hints = self._hints(submission, merged)
                with self._adapter_lock:
                    checkpoint()
                    facts = self.vision.analyze(images, hints)
                checkpoint()

                story_note = self._story_note(submission, bukti_index, facts)
                result = self.engine.evaluate(
                    submission,
                    task.task_id,
                    facts,
                    story_note=story_note,
                    extra_warnings=task.warnings,
                )
                result.angel_card_note = angel_note
                result.validated_at = wib_now()
                _apply_anomalies(result, facts)
                batch.results.append(result)

            checkpoint()
            batch.finished_at = wib_now()
            _emit(progress, "write", total, total, "menulis CSV + audit log")
            with OUTPUT_LOCK:
                # Check again after waiting for another writer; commit as one unit.
                checkpoint()
                self.outputs.write_batch(batch, actor=actor)
                self._audit_decisions(batch, actor)
            return batch
        except _ValidationCancelled:
            completed = batch.total
            batch.cancelled = True
            batch.results.clear()
            batch.finished_at = wib_now()
            message = (
                f"Validation cancelled after {completed} completed rows; partial results "
                "discarded. No verification, violations, or blast draft written."
            )
            batch.warnings.append(message)
            self.outputs.audit(
                action="validate_cancelled",
                actor=actor,
                target=batch.sheet_name,
                result="CANCELLED",
                rationale=message,
                extra={
                    "task_id": batch.task_id,
                    "divisi": batch.divisi,
                    "completed_count": completed,
                    "skipped_existing": batch.skipped_existing,
                    "duration_seconds": batch.duration_seconds,
                },
            )
            _emit(progress, "cancelled", completed, completed, message)
            return batch

    def _story_note(
        self, submission: Submission, bukti_index: dict[str, str], facts: VisionFacts
    ) -> str | None:
        handle = normalize_handle(submission.ig_username)
        note = ""
        if handle:
            note = bukti_index.get(handle, "")
        if not note:
            # tolerate a typo in the PJ upload
            for candidate, description in bukti_index.items():
                if handle_similarity(handle, candidate) >= 0.9:
                    note = description
                    break
        if not note:
            return None

        lowered = note.lower()
        if facts.story_live is None:
            if "expired" in lowered or "hilang" in lowered:
                facts.story_live = False
            elif "live" in lowered or "masih ada" in lowered or "tayang" in lowered:
                facts.story_live = True
        return note

    def _audit_decisions(self, batch: ValidationBatch, actor: str) -> None:
        for result in batch.results:
            if result.status not in (ResultStatus.FLAGGED, ResultStatus.PENDING_MANUAL_REVIEW):
                continue
            self.outputs.audit(
                action="flag",
                actor=actor,
                target=f"{result.submission.nama} / {batch.sheet_name}",
                result=result.status,
                rationale=result.violation_summary,
                rule_ids=result.violation_ids,
                confidence=result.facts.confidence if result.facts else 0.0,
                extra={
                    "total_denda": result.total_denda,
                    "evidence": result.submission.links[:2],
                    "notes": result.notes,
                },
            )

    # ── reporting helpers used by Discord commands ─────────────────────────
    def task_overview(self) -> list[dict[str, Any]]:
        today = wib_now()
        overview: list[dict[str, Any]] = []

        for task in self.engine.all_tasks():
            task_id = task.get("id")
            merged = self.engine.merge(task_id)
            sheet = task.get("sheet")
            rows = self.outputs.rows_for_sheet(sheet) if sheet else []

            statuses = [row.get("status", "") for row in rows]
            if rows and all(status in (ResultStatus.VERIFIED, ResultStatus.EXEMPT) for status in statuses):
                state = "SELESAI"
            elif any(status == ResultStatus.FLAGGED for status in statuses):
                state = "PERLU_TINDAKAN"
            elif rows:
                state = "SEBAGIAN"
            else:
                state = "PENDING"

            deadline = merged.deadline
            overdue = bool(deadline and deadline < today and state != "SELESAI")

            overview.append(
                {
                    "task_id": task_id,
                    "task_type": merged.task_type,
                    "sheet": sheet,
                    "platforms": merged.platforms,
                    "jadwal_post": merged.jadwal_post,
                    "deadline": deadline,
                    "state": state,
                    "overdue": overdue,
                    "rows": len(rows),
                    "flagged": sum(1 for status in statuses if status == ResultStatus.FLAGGED),
                    "total_denda": sum(
                        int(float(row.get("total_denda") or 0)) for row in rows
                    ),
                    "blast_context": bool(merged.blast),
                }
            )

        overview.sort(key=lambda item: item["deadline"] or datetime.max)
        return overview

    def missing_submitters(self, sheet_query: str) -> dict[str, Any]:
        sheet_name, _ = self._resolve_sheet(sheet_query)
        submissions = self.data.read_submissions(sheet_name)
        submitted = {row.nama.strip().lower() for row in submissions}
        roster = self.data.load_roster()
        missing = [
            entry for entry in roster if entry.nama.strip().lower() not in submitted
        ]
        return {
            "sheet_name": sheet_name,
            "total_panitia": len(roster),
            "submitted": len(submissions),
            "missing": missing,
        }

    def resubmit_status(self, nama: str) -> dict[str, Any]:
        violations = self.outputs.violation_history(nama)
        return {
            "nama": nama,
            "violations": violations,
            "recurring": self.outputs.recurring_count(nama),
            "total_denda": self.outputs.denda_total(nama),
        }


# ─── module helpers ─────────────────────────────────────────────────────────
def _normalize_division(divisi: str) -> str:
    return " ".join(divisi.split()).casefold()


def _filter_division(
    submissions: Sequence[Submission], roster: Sequence[RosterEntry], divisi: str
) -> tuple[list[Submission], str]:
    available: dict[str, str] = {}
    by_nim: dict[str, list[RosterEntry]] = {}
    by_name: dict[str, list[RosterEntry]] = {}
    for entry in roster:
        if entry.divisi.strip():
            available.setdefault(_normalize_division(entry.divisi), " ".join(entry.divisi.split()))
            if entry.nim.strip():
                by_nim.setdefault(entry.nim.strip(), []).append(entry)
            by_name.setdefault(normalize_name(entry.nama), []).append(entry)

    rows: list[Submission] = []
    for submission in submissions:
        if not submission.divisi.strip():
            candidates = by_nim.get(submission.nim.strip(), [])
            if not candidates:
                candidates = [
                    entry for entry in by_name.get(normalize_name(submission.nama), [])
                    if not submission.nim.strip() or not entry.nim.strip()
                    or entry.nim.strip() == submission.nim.strip()
                ]
            divisions = {_normalize_division(entry.divisi) for entry in candidates}
            if len(divisions) == 1:
                # Do not mutate rows cached by the source or shared by other runs.
                submission = replace(submission, divisi=available[divisions.pop()])
        if submission.divisi.strip():
            available.setdefault(
                _normalize_division(submission.divisi), " ".join(submission.divisi.split())
            )
        rows.append(submission)

    target = _normalize_division(divisi)
    if not target:
        return rows, ""
    if target not in available:
        raise DivisionNotFoundError(divisi, sorted(available.values(), key=str.casefold))
    return [row for row in rows if _normalize_division(row.divisi) == target], available[target]


def _load_rulebase() -> dict[str, Any]:
    path = Path(config.RULEBASE_PATH)
    if not path.exists():
        raise FileNotFoundError(f"Rulebase not found: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _emit(
    progress: ProgressCallback | None, stage: str, current: int, total: int, message: str
) -> None:
    if progress is not None:
        try:
            progress(stage, current, total, message)
        except Exception:
            pass


def _sheet_mentions(candidate: str, sheet_name: str) -> bool:
    from .utils import sheet_similarity

    return sheet_similarity(candidate, sheet_name) >= 0.9


def _apply_anomalies(result: ValidationResult, facts: VisionFacts) -> None:
    """Turn vision anomaly flags into explicit review notes / statuses."""
    if facts.has_flag("wrong_submit"):
        result.status = ResultStatus.PENDING_MANUAL_REVIEW
        result.notes.append(
            "Bukti terlihat seperti tugas lain (wrong-submit) — perlu konfirmasi koor"
        )
    if facts.has_flag("vision_error"):
        result.status = ResultStatus.PENDING_MANUAL_REVIEW
        error = (facts.raw or {}).get("error", "")
        result.notes.append(
            "Vision gagal membaca bukti"
            + (f" ({error})" if error else "")
            + " — hasil tidak lengkap, jangan blast dulu"
        )
    if facts.has_flag("no_evidence"):
        result.notes.append("Tidak ada bukti yang bisa dibaca")
    if facts.has_flag("post_missing"):
        result.notes.append("Postingan tidak ditemukan saat validasi")
    if facts.has_flag("carousel_order_mismatch"):
        result.notes.append("Urutan carousel tidak sesuai referensi")
