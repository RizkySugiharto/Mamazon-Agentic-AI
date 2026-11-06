"""
Rulebase engine: loads rulebase.json, merges per-blast extra requirements from
blast_context.json, and applies checks C1-C10 (PP) / B1-B7 (Buzzer).

Design rules:
  • Checks are pure functions of (submission, merged context, VisionFacts).
    They never touch the network, so they are fully unit-testable.
  • When the reference material is missing, the check returns INCONCLUSIVE
    instead of FAIL. We never fine a panitia for something we could not verify.
  • Date inconsistencies inside rulebase.json are surfaced as FLAG + a warning,
    never as a mass fine. See `lint_rulebase()`.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import config
from .data_layer import AngelEntry
from .models import (
    BlastContext,
    CheckResult,
    CheckStatus,
    ResultStatus,
    Submission,
    TaskType,
    ValidationResult,
    VisionFacts,
)
from .utils import (
    caption_similarity,
    day_name_matches,
    extract_mentions,
    format_rupiah,
    handle_similarity,
    is_emoji_only,
    name_similarity,
    normalize_caption,
    normalize_handle,
    parse_dt,
    truncate,
    unique,
    word_count,
)


# ─── Merged context ─────────────────────────────────────────────────────────
@dataclass
class MergedContext:
    """rulebase task + blast_context, flattened for the checks."""

    task_id: str
    task_type: str
    raw_task: dict[str, Any] = field(default_factory=dict)
    platforms: list[str] = field(default_factory=list)
    jadwal_post: datetime | None = None
    deadline: datetime | None = None
    blast_date: datetime | None = None
    caption_reference: str = ""
    tags: list[str] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)
    extra_hashtags: list[str] = field(default_factory=list)
    keep_hours: int = config.STORY_KEEP_HOURS
    do_not_delete_until: datetime | None = None
    blast: BlastContext | None = None
    sources: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    form_link: str = ""
    drive_link: str = ""

    @property
    def timing_consistent(self) -> bool:
        """False when rulebase data has deadline before the scheduled post."""
        if not self.jadwal_post or not self.deadline:
            return True
        return self.deadline >= self.jadwal_post

    @property
    def is_story(self) -> bool:
        return any(p.lower() == "story" for p in self.platforms)

    @property
    def is_feed(self) -> bool:
        return any(p.lower() == "feed" for p in self.platforms)

    @property
    def expected_slides(self) -> int:
        return _expected_slide_count(self.raw_task) or 0

    @property
    def target_url(self) -> str:
        return ((self.raw_task.get("links") or {}).get("target") or "")


def _handle_key(handle: str | None) -> str:
    """Treat @horizon.umn and @horizon_umn as the same account."""
    return normalize_handle(handle).replace(".", "_")


def _dedupe_handles(handles: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for handle in handles:
        key = _handle_key(handle)
        if key and key not in seen:
            seen.add(key)
            output.append(handle)
    return output


def _dedupe_hashtags(tags: Iterable[str]) -> list[str]:
    seen: set[str] = set()
    output: list[str] = []
    for tag in tags:
        key = str(tag).strip().lower()
        if key and key not in seen:
            seen.add(key)
            output.append(str(tag).strip())
    return output


# ─── Lint ───────────────────────────────────────────────────────────────────
@dataclass
class LintIssue:
    severity: str      # ERROR | WARN | INFO
    code: str
    message: str
    task_id: str | None = None

    def __str__(self) -> str:
        where = f"[{self.task_id}] " if self.task_id else ""
        return f"{self.severity:5} {self.code:22} {where}{self.message}"


def lint_rulebase(rulebase: dict[str, Any]) -> list[LintIssue]:
    """
    Report data problems in rulebase.json without mutating it.

    Known issues in v1.1: PP-01 has deadline before jadwal_post, and the sheet
    mapping conflicts with config.SHEET_TO_TASK for PP-01 / BZ-01 / BZ-07.
    """
    issues: list[LintIssue] = []
    sheet_owner: dict[str, str] = {}

    for key, task_type in (("tugas_pp", TaskType.PP), ("tugas_buzzer", TaskType.BUZZER)):
        for task in rulebase.get(key, []) or []:
            task_id = task.get("id", "?")
            jadwal_raw = task.get("jadwal_post")
            deadline_raw = task.get("deadline")
            jadwal = parse_dt(jadwal_raw)
            deadline = parse_dt(deadline_raw)

            if not jadwal_raw:
                issues.append(LintIssue("ERROR", "MISSING_JADWAL", "jadwal_post kosong", task_id))
            elif jadwal is None:
                issues.append(
                    LintIssue("ERROR", "UNPARSED_JADWAL", f"tidak bisa parse '{jadwal_raw}'", task_id)
                )

            if not deadline_raw:
                issues.append(LintIssue("ERROR", "MISSING_DEADLINE", "deadline kosong", task_id))
            elif deadline is None:
                issues.append(
                    LintIssue("ERROR", "UNPARSED_DEADLINE", f"tidak bisa parse '{deadline_raw}'", task_id)
                )

            if jadwal and deadline and deadline < jadwal:
                issues.append(
                    LintIssue(
                        "ERROR",
                        "DEADLINE_BEFORE_JADWAL",
                        f"deadline {deadline:%d %b %Y %H:%M} mendahului jadwal "
                        f"{jadwal:%d %b %Y %H:%M} — validasi timing akan di-flag, "
                        "bukan didenda",
                        task_id,
                    )
                )

            if jadwal and jadwal_raw:
                matches = day_name_matches(jadwal, jadwal_raw)
                if matches is False:
                    issues.append(
                        LintIssue(
                            "WARN",
                            "DAY_NAME_MISMATCH",
                            f"nama hari di '{jadwal_raw}' tidak cocok dengan tanggal "
                            f"({jadwal:%A})",
                            task_id,
                        )
                    )

            sheet = task.get("sheet")
            if sheet:
                normalized = sheet.strip().lower()
                if normalized in sheet_owner:
                    issues.append(
                        LintIssue(
                            "WARN",
                            "DUPLICATE_SHEET",
                            f"sheet '{sheet}' juga dipakai {sheet_owner[normalized]}",
                            task_id,
                        )
                    )
                sheet_owner[normalized] = task_id

                mapped = config.SHEET_TO_TASK.get(sheet)
                if mapped and mapped != task_id:
                    issues.append(
                        LintIssue(
                            "WARN",
                            "SHEET_MAP_CONFLICT",
                            f"rulebase sheet '{sheet}' -> {task_id}, tapi "
                            f"config.SHEET_TO_TASK -> {mapped}",
                            task_id,
                        )
                    )

            if task_type == TaskType.PP:
                custom = task.get("custom_requirements") or {}
                if custom.get("caption_verbatim") and not task.get("caption_verbatim"):
                    issues.append(
                        LintIssue(
                            "WARN",
                            "MISSING_VERBATIM_CAPTION",
                            "caption_verbatim=true tapi teks caption tidak ada di "
                            "rulebase — C4 akan INCONCLUSIVE sampai blast_context diisi",
                            task_id,
                        )
                    )

            blob = json.dumps(task, ensure_ascii=False)
            if any("\u4e00" <= ch <= "\u9fff" for ch in blob):
                issues.append(
                    LintIssue(
                        "INFO",
                        "GARBLED_TEXT",
                        "mengandung karakter CJK (kemungkinan salah encoding)",
                        task_id,
                    )
                )

    for sheet, mapped in config.SHEET_TO_TASK.items():
        if mapped and sheet.strip().lower() not in sheet_owner:
            issues.append(
                LintIssue(
                    "WARN",
                    "CONFIG_ONLY_SHEET",
                    f"config maps '{sheet}' -> {mapped} tapi tidak ada task yang "
                    "mendeklarasikan sheet tersebut",
                    mapped,
                )
            )

    return issues


# ─── Engine ─────────────────────────────────────────────────────────────────
class RulebaseEngine:
    def __init__(
        self,
        rulebase: dict[str, Any] | None = None,
        blast_context: dict[str, BlastContext] | None = None,
        path: Path | None = None,
    ) -> None:
        if rulebase is None:
            rulebase = json.loads(
                Path(path or config.RULEBASE_PATH).read_text(encoding="utf-8")
            )
        self.rulebase = rulebase
        self.blast_context = blast_context or {}
        self._tasks: dict[str, dict[str, Any]] = {}
        self._task_types: dict[str, str] = {}
        self._checks: dict[str, dict[str, Any]] = {}

        for key, task_type in (("tugas_pp", TaskType.PP), ("tugas_buzzer", TaskType.BUZZER)):
            for task in rulebase.get(key, []) or []:
                task_id = task.get("id")
                if task_id:
                    self._tasks[task_id] = task
                    self._task_types[task_id] = task_type

        for key in ("checks_pp", "checks_buzzer"):
            for check in rulebase.get(key, []) or []:
                self._checks[check["id"]] = check

        self.denda_table = rulebase.get("denanda_denda", {}) or {}

    # ── lookups ────────────────────────────────────────────────────────────
    def task(self, task_id: str | None) -> dict[str, Any] | None:
        return self._tasks.get(task_id) if task_id else None

    def task_type(self, task_id: str | None) -> str:
        if not task_id:
            return TaskType.PP
        return self._task_types.get(task_id, TaskType.PP)

    def check_meta(self, check_id: str) -> dict[str, Any]:
        return self._checks.get(check_id, {})

    def check_name(self, check_id: str) -> str:
        return self.check_meta(check_id).get("nama", check_id)

    def check_denda(self, check_id: str) -> int:
        return int(self.check_meta(check_id).get("denda") or 0)

    def all_tasks(self, task_type: str | None = None) -> list[dict[str, Any]]:
        if task_type is None:
            return list(self._tasks.values())
        return [
            task for task_id, task in self._tasks.items()
            if self._task_types[task_id] == task_type
        ]

    def task_ids(self, task_type: str | None = None) -> list[str]:
        return [task["id"] for task in self.all_tasks(task_type)]

    # ── merge ──────────────────────────────────────────────────────────────
    def merge(self, task_id: str | None) -> MergedContext:
        task = self.task(task_id) or {}
        task_type = self.task_type(task_id)
        blast = self.blast_context.get(task_id) if task_id else None

        platforms: list[str] = []
        tags: list[str] = []
        hashtags: list[str] = []
        caption_reference = ""
        keep_hours = config.STORY_KEEP_HOURS
        do_not_delete_until = None

        if task:
            platforms = list(task.get("platform") or [])
            if not platforms and task_type == TaskType.BUZZER:
                platforms = ["buzzer"]

            for platform_key in ("story", "feed"):
                block = task.get(platform_key) or {}
                tags.extend(block.get("tags") or [])
                hashtags.extend(block.get("hashtags") or [])
                if platform_key == "story":
                    keep_hours = int(block.get("keep_hours") or keep_hours)
                if platform_key == "feed":
                    do_not_delete_until = parse_dt(block.get("do_not_delete_until"))

            caption_reference = normalize_caption(
                task.get("caption_verbatim") or ""
            )

            custom = task.get("custom_requirements") or {}
            hashtags.extend(custom.get("extra_hashtags_in_caption") or [])

        sources: list[str] = ["rulebase.json"] if task else []
        warnings: list[str] = []

        if blast:
            sources.append(f"blast_context:{blast.blast_message_id or blast.task_id}")
            tags.extend(blast.tags)
            hashtags.extend(blast.hashtags)
            if blast.caption_referensi and not caption_reference:
                caption_reference = normalize_caption(blast.caption_referensi)

        # A schedule written without a clock time means "any time that day", so it
        # must parse to 00:00 — not 23:59, which would flag every morning post as
        # "too early". Deadlines keep end-of-day as the default.
        jadwal = parse_dt(task.get("jadwal_post"), default_time=(0, 0)) if task else None
        deadline = parse_dt(task.get("deadline"), default_time=(23, 59)) if task else None
        if blast and blast.deadline:
            deadline = blast.deadline

        blast_date = None
        if blast and blast.blast_date:
            blast_date = blast.blast_date
        elif jadwal:
            blast_date = jadwal
        elif deadline:
            blast_date = deadline

        merged = MergedContext(
            task_id=task_id or "UNKNOWN",
            task_type=task_type,
            raw_task=task,
            platforms=platforms,
            jadwal_post=jadwal,
            deadline=deadline,
            blast_date=blast_date,
            caption_reference=caption_reference,
            tags=_dedupe_handles(tags),
            hashtags=_dedupe_hashtags(hashtags),
            extra_hashtags=_dedupe_hashtags(
                (task.get("custom_requirements") or {}).get("extra_hashtags_in_caption") or []
            ),
            keep_hours=keep_hours,
            do_not_delete_until=do_not_delete_until,
            blast=blast,
            sources=sources,
            warnings=warnings,
            form_link=task.get("form_link", ""),
            drive_link=(blast.drive_folder if blast and blast.drive_folder else task.get("drive_link", "")) or "",
        )

        if not merged.timing_consistent:
            merged.warnings.append(
                "Data rulebase tidak konsisten: deadline lebih awal dari jadwal post. "
                "C2/C10 di-flag untuk review manual, bukan didenda otomatis."
            )
        if not task:
            merged.warnings.append(
                "Task tidak ditemukan di rulebase — validasi memakai generic checks."
            )
        if task and not merged.caption_reference and merged.is_feed:
            merged.warnings.append(
                "Caption referensi tidak tersedia (rulebase maupun blast_context). "
                "C4 akan INCONCLUSIVE."
            )

        return merged

    # ── applicability ──────────────────────────────────────────────────────
    def applicable_checks(self, task_id: str | None) -> list[str]:
        if self.task_type(task_id) == TaskType.BUZZER:
            task = self.task(task_id) or {}
            types = set(task.get("tipe_validasi") or [])
            checks: list[str] = []
            if types & {"like", "like_dual_platform"}:
                checks.append("B1")
            if "komentar_natural" in types:
                checks.append("B2")
            if types & {"review_teks", "template_match"}:
                checks.append("B3")
            if "rating_5_star" in types:
                checks.append("B4")
            if "vote" in types:
                checks.append("B5")
            if task_id == "BZ-07" or "jawaban_jujur" in types:
                checks.append("B6")
            checks.append("B7")
            return checks

        checks = ["C1", "C2"]
        merged = self.merge(task_id)
        if merged.is_feed:
            checks.extend(["C3", "C4", "C5", "C6", "C8", "C9"])
        else:
            checks.extend(["C4", "C5", "C6"])
        if merged.is_story:
            checks.append("C7")
        checks.append("C10")
        return checks

    # ── Angel Card ─────────────────────────────────────────────────────────
    def angel_exemption(
        self,
        nama: str,
        task_id: str | None,
        registry: Sequence[AngelEntry],
        blast_date: datetime | None = None,
    ) -> tuple[bool, str]:
        """
        Angel Card exempts PP and Buzzer only when it was bought on or before
        the blast date. Sponsor tasks are never exempt.
        """
        task_type = self.task_type(task_id)
        if task_type == TaskType.SPONSOR:
            return False, "Angel Card tidak berlaku untuk Sponsor"

        if not registry:
            return False, ""

        best: AngelEntry | None = None
        best_score = 0.0
        for entry in registry:
            score = name_similarity(nama, entry.nama)
            if score > best_score:
                best, best_score = entry, score
        if best is None or best_score < 0.86:
            return False, ""
        if not best.active:
            return False, f"Angel Card {best.nama} status {best.status}"
        if best.tanggal_beli is None:
            return False, f"Angel Card {best.nama} tanpa tanggal pembelian"

        reference = blast_date or self.merge(task_id).blast_date
        if reference is None:
            return False, f"Tanggal blast tidak diketahui untuk {task_id}"

        if best.tanggal_beli.date() <= reference.date():
            return True, (
                f"Angel Card aktif (beli {best.tanggal_beli:%d %b %Y} ≤ blast "
                f"{reference:%d %b %Y}) — dibebaskan dari {task_id}"
            )
        return False, (
            f"Angel Card dibeli {best.tanggal_beli:%d %b %Y}, setelah blast "
            f"{reference:%d %b %Y} — tetap wajib mengerjakan {task_id}"
        )

    # ── evaluation ─────────────────────────────────────────────────────────
    def evaluate(
        self,
        submission: Submission,
        task_id: str | None,
        facts: VisionFacts,
        story_note: str | None = None,
        extra_warnings: Sequence[str] | None = None,
    ) -> ValidationResult:
        merged = self.merge(task_id)
        result = ValidationResult(
            submission=submission,
            task_type=merged.task_type,
            task_id=task_id,
            blast_date=merged.blast_date,
            facts=facts,
        )
        # Context warnings (rulebase inconsistency, missing caption reference) are
        # batch-level concerns; the individual checks already say INCONCLUSIVE or
        # FLAG. Keeping them out of result.notes avoids repeating the same line in
        # every CSV row.
        if extra_warnings:
            for warning in extra_warnings:
                if warning not in merged.warnings:
                    result.notes.append(warning)

        for check_id in self.applicable_checks(task_id):
            check = self._run_check(check_id, submission, merged, facts, story_note)
            result.checks.append(check)

        # Status resolution
        if any(check.is_violation for check in result.checks):
            result.status = ResultStatus.FLAGGED
        elif facts.image_count == 0 and submission.has_evidence:
            result.status = ResultStatus.PENDING_MANUAL_REVIEW
            result.notes.append("Bukti tidak bisa diunduh / dibaca — perlu review manual")
        else:
            result.status = ResultStatus.VERIFIED

        return result

    def _run_check(
        self,
        check_id: str,
        submission: Submission,
        merged: MergedContext,
        facts: VisionFacts,
        story_note: str | None,
    ) -> CheckResult:
        handlers = {
            "C1": self._check_c1,
            "C2": self._check_c2,
            "C3": self._check_c3,
            "C4": self._check_c4,
            "C5": self._check_c5,
            "C6": self._check_c6,
            "C7": self._check_c7,
            "C8": self._check_c8,
            "C9": self._check_c9,
            "C10": self._check_c10,
            "B1": self._check_b1,
            "B2": self._check_b2,
            "B3": self._check_b3,
            "B4": self._check_b4,
            "B5": self._check_b5,
            "B6": self._check_b6,
            "B7": self._check_b7,
        }
        handler = handlers.get(check_id)
        if handler is None:
            return self._result(
                check_id, CheckStatus.NOT_APPLICABLE, "check tidak dikenal", 0
            )
        return handler(submission, merged, facts, story_note)

    def _result(
        self,
        check_id: str,
        status: str,
        detail: str,
        denda: int | None = None,
        evidence: Sequence[str] | None = None,
        confidence: float = 0.0,
    ) -> CheckResult:
        return CheckResult(
            check_id=check_id,
            name=self.check_name(check_id),
            status=status,
            denda=denda if denda is not None else 0,
            detail=detail,
            evidence=list(evidence or []),
            confidence=confidence,
        )

    def _fail(self, check_id: str, detail: str, submission: Submission, confidence: float) -> CheckResult:
        return self._result(
            check_id,
            CheckStatus.FAIL,
            detail,
            denda=self.check_denda(check_id),
            evidence=submission.links[:3],
            confidence=confidence,
        )

    # ── PP checks ──────────────────────────────────────────────────────────
    def _check_c1(self, sub, merged, facts, story_note) -> CheckResult:
        if facts.followers is None and facts.is_private is None:
            return self._result(
                "C1", CheckStatus.INCONCLUSIVE,
                "Jumlah followers tidak terbaca dari bukti", confidence=facts.confidence,
            )

        if facts.is_private:
            return self._fail("C1", "Akun Instagram di-set private", sub, facts.confidence)

        if facts.followers is not None and facts.followers < config.MIN_IG_FOLLOWERS:
            return self._fail(
                "C1",
                f"Followers {facts.followers} < {config.MIN_IG_FOLLOWERS}",
                sub,
                facts.confidence,
            )

        if facts.ig_username_seen and sub.ig_username:
            similarity = handle_similarity(facts.ig_username_seen, sub.ig_username)
            if similarity < 0.8:
                return self._result(
                    "C1",
                    CheckStatus.FLAG,
                    f"Username di bukti (@{normalize_handle(facts.ig_username_seen)}) "
                    f"berbeda dengan spreadsheet (@{normalize_handle(sub.ig_username)}) "
                    "— kemungkinan ganti akun, butuh konfirmasi koor",
                    confidence=facts.confidence,
                )

        return self._result(
            "C1", CheckStatus.PASS,
            f"Followers {facts.followers if facts.followers is not None else '?'} "
            f"≥ {config.MIN_IG_FOLLOWERS}, akun publik",
            confidence=facts.confidence,
        )

    def _check_c2(self, sub, merged, facts, story_note) -> CheckResult:
        posted = facts.posted_at or sub.timestamp
        if not merged.timing_consistent:
            return self._result(
                "C2", CheckStatus.FLAG,
                "Rulebase: deadline mendahului jadwal post — timing tidak bisa "
                "dinilai otomatis",
                confidence=facts.confidence,
            )
        if posted is None or merged.jadwal_post is None:
            return self._result(
                "C2", CheckStatus.INCONCLUSIVE,
                "Timestamp post / jadwal tidak tersedia", confidence=facts.confidence,
            )

        early_limit = merged.jadwal_post - _minutes(config.POST_EARLY_TOLERANCE_MINUTES)
        if posted < early_limit:
            delta = int((merged.jadwal_post - posted).total_seconds() // 60)
            return self._fail(
                "C2",
                f"Post {delta} menit lebih awal dari jadwal "
                f"{merged.jadwal_post:%d %b %Y %H:%M}",
                sub,
                facts.confidence,
            )
        if merged.deadline and posted > merged.deadline:
            delta = int((posted - merged.deadline).total_seconds() // 3600)
            return self._fail(
                "C2",
                f"Post terlambat {delta} jam dari deadline "
                f"{merged.deadline:%d %b %Y %H:%M}",
                sub,
                facts.confidence,
            )
        return self._result(
            "C2", CheckStatus.PASS,
            f"Post {posted:%d %b %Y %H:%M} sesuai jadwal",
            confidence=facts.confidence,
        )

    def _check_c3(self, sub, merged, facts, story_note) -> CheckResult:
        if facts.has_flag("carousel_order_mismatch"):
            return self._fail(
                "C3", "Urutan slide/carousel tidak sesuai referensi", sub, facts.confidence
            )
        if not facts.carousel_slides:
            return self._result(
                "C3", CheckStatus.NOT_APPLICABLE,
                "Tidak ada carousel terdeteksi di bukti", confidence=facts.confidence,
            )
        expected = _expected_slide_count(merged.raw_task)
        if expected and len(facts.carousel_slides) < expected:
            return self._result(
                "C3", CheckStatus.FLAG,
                f"Carousel {len(facts.carousel_slides)} slide, referensi {expected} slide "
                "— butuh cek manual",
                confidence=facts.confidence,
            )
        return self._result(
            "C3", CheckStatus.PASS,
            f"Carousel {len(facts.carousel_slides)} slide, urutan sesuai",
            confidence=facts.confidence,
        )

    def _check_c4(self, sub, merged, facts, story_note) -> CheckResult:
        reference = merged.caption_reference
        if not reference:
            return self._result(
                "C4", CheckStatus.INCONCLUSIVE,
                "Caption referensi tidak tersedia (rulebase/blast_context kosong)",
                confidence=facts.confidence,
            )
        actual = facts.caption_text
        if not actual:
            return self._result(
                "C4", CheckStatus.INCONCLUSIVE,
                "Caption tidak terbaca dari screenshot", confidence=facts.confidence,
            )

        similarity = caption_similarity(actual, reference)
        if similarity >= 0.97:
            return self._result(
                "C4", CheckStatus.PASS,
                f"Caption sesuai referensi (kemiripan {similarity:.0%})",
                confidence=facts.confidence,
            )
        return self._fail(
            "C4",
            f"Caption tidak sesuai referensi (kemiripan {similarity:.0%}, "
            f"wajib verbatim {len(reference)} karakter)",
            sub,
            facts.confidence,
        )

    def _check_c5(self, sub, merged, facts, story_note) -> CheckResult:
        required = merged.tags
        if not required:
            return self._result(
                "C5", CheckStatus.NOT_APPLICABLE,
                "Task tidak mendefinisikan tag wajib", confidence=facts.confidence,
            )
        if not facts.mentions and not facts.caption_text:
            return self._result(
                "C5", CheckStatus.INCONCLUSIVE,
                "Tidak ada teks caption yang bisa dibaca", confidence=facts.confidence,
            )

        present = {_handle_key(mention) for mention in facts.mentions}
        present |= {_handle_key(handle) for handle in extract_mentions(facts.caption_text)}
        missing = [tag for tag in required if _handle_key(tag) not in present]

        if missing:
            return self._fail(
                "C5",
                "Tag wajib tidak ditemukan: " + ", ".join(missing),
                sub,
                facts.confidence,
            )
        return self._result(
            "C5", CheckStatus.PASS,
            f"Semua tag wajib ada ({len(required)})", confidence=facts.confidence,
        )

    def _check_c6(self, sub, merged, facts, story_note) -> CheckResult:
        required = merged.hashtags
        if not required:
            return self._result(
                "C6", CheckStatus.NOT_APPLICABLE,
                "Task tidak mendefinisikan hashtag wajib", confidence=facts.confidence,
            )
        if not facts.hashtags and not facts.all_text():
            return self._result(
                "C6", CheckStatus.INCONCLUSIVE,
                "Hashtag tidak terbaca dari bukti", confidence=facts.confidence,
            )

        present = {tag.lower() for tag in facts.hashtags}
        missing = [tag for tag in required if tag.lower() not in present]
        if missing:
            return self._fail(
                "C6",
                f"Hashtag kurang: {', '.join(missing)} ({len(present)}/{len(required)} ada)",
                sub,
                facts.confidence,
            )
        return self._result(
            "C6", CheckStatus.PASS,
            f"Semua {len(required)} hashtag wajib ada", confidence=facts.confidence,
        )

    def _check_c7(self, sub, merged, facts, story_note) -> CheckResult:
        if facts.story_uploading_text:
            return self._fail(
                "C7", "Screenshot menunjukkan story belum selesai di-upload", sub, facts.confidence
            )
        if facts.story_live is True:
            detail = f"Story live, keep {merged.keep_hours} jam"
            if story_note:
                detail += f" (bukti tambahan: {story_note})"
            return self._result("C7", CheckStatus.PASS, detail, confidence=facts.confidence)
        if facts.story_live is False:
            return self._fail(
                "C7",
                "Story tidak tersedia / sudah expired" + (f" — {story_note}" if story_note else ""),
                sub,
                facts.confidence,
            )
        return self._result(
            "C7", CheckStatus.INCONCLUSIVE,
            "Status story tidak bisa ditentukan dari bukti" + (f" — {story_note}" if story_note else ""),
            confidence=facts.confidence,
        )

    def _check_c8(self, sub, merged, facts, story_note) -> CheckResult:
        if facts.has_flag("post_missing"):
            return self._fail(
                "C8", "Feed sudah tidak ada / dihapus saat validasi", sub, facts.confidence
            )
        return self._result(
            "C8", CheckStatus.PASS,
            "Feed masih live" + (
                f" (wajib sampai {merged.do_not_delete_until:%d %b %Y})"
                if merged.do_not_delete_until else ""
            ),
            confidence=facts.confidence,
        )

    def _check_c9(self, sub, merged, facts, story_note) -> CheckResult:
        if facts.has_flag("aspect_ratio_mismatch"):
            return self._fail(
                "C9", f"Aspect ratio {facts.aspect_ratio or '?'} tidak sesuai referensi",
                sub, facts.confidence,
            )
        if not facts.aspect_ratio:
            return self._result(
                "C9", CheckStatus.NOT_APPLICABLE,
                "Dimensi gambar tidak terbaca", confidence=facts.confidence,
            )
        return self._result(
            "C9", CheckStatus.PASS,
            f"Aspect ratio {facts.aspect_ratio} sesuai", confidence=facts.confidence,
        )

    def _check_c10(self, sub, merged, facts, story_note) -> CheckResult:
        return self._check_bukti_timely("C10", sub, merged, facts)

    def _check_b7(self, sub, merged, facts, story_note) -> CheckResult:
        return self._check_bukti_timely("B7", sub, merged, facts)

    def _check_bukti_timely(
        self, check_id: str, sub, merged, facts
    ) -> CheckResult:
        """Shared logic for C10 (PP) and B7 (Buzzer): proof sent on time."""
        if not sub.has_evidence:
            return self._fail(check_id, "Tidak ada bukti yang dikirim", sub, 1.0)
        if not merged.timing_consistent:
            return self._result(
                check_id, CheckStatus.FLAG,
                "Rulebase: deadline mendahului jadwal — ketepatan bukti tidak bisa "
                "dinilai otomatis",
                confidence=facts.confidence,
            )
        if sub.timestamp is None:
            return self._result(
                check_id, CheckStatus.FLAG,
                "Timestamp submission tidak terbaca", confidence=facts.confidence,
            )
        if merged.deadline and sub.timestamp > merged.deadline:
            delta = int((sub.timestamp - merged.deadline).total_seconds() // 3600)
            return self._fail(
                check_id,
                f"Bukti dikirim {delta} jam setelah deadline "
                f"{merged.deadline:%d %b %Y %H:%M}",
                sub,
                facts.confidence,
            )
        return self._result(
            check_id, CheckStatus.PASS,
            f"Bukti dikirim {sub.timestamp:%d %b %Y %H:%M}",
            confidence=facts.confidence,
        )

    # ── Buzzer checks ──────────────────────────────────────────────────────
    def _check_b1(self, sub, merged, facts, story_note) -> CheckResult:
        expected = _expected_platforms(merged.raw_task)
        detected = {platform.lower() for platform in facts.like_platforms}
        if not detected and facts.likes is None:
            return self._result(
                "B1", CheckStatus.INCONCLUSIVE,
                "Like tidak terbaca dari bukti", confidence=facts.confidence,
            )
        missing = [platform for platform in expected if platform not in detected]
        if missing:
            return self._fail(
                "B1",
                "Like belum ditemukan di: " + ", ".join(missing),
                sub,
                facts.confidence,
            )
        return self._result(
            "B1", CheckStatus.PASS,
            f"Like terdeteksi di {', '.join(sorted(detected))}", confidence=facts.confidence,
        )

    def _check_b2(self, sub, merged, facts, story_note) -> CheckResult:
        custom = merged.raw_task.get("custom_requirements") or {}
        min_words = int(custom.get("komentar_natural_min_length") or 5)
        comment = facts.comment_text
        if not comment:
            return self._result(
                "B2", CheckStatus.INCONCLUSIVE,
                "Komentar tidak terbaca dari screenshot", confidence=facts.confidence,
            )
        if is_emoji_only(comment):
            return self._fail("B2", "Komentar hanya emoji", sub, facts.confidence)
        count = word_count(comment)
        if count < min_words:
            return self._fail(
                "B2", f"Komentar terlalu pendek ({count} kata, minimal {min_words})",
                sub, facts.confidence,
            )
        return self._result(
            "B2", CheckStatus.PASS,
            f"Komentar natural ({count} kata)", confidence=facts.confidence,
        )

    def _check_b3(self, sub, merged, facts, story_note) -> CheckResult:
        reference = merged.caption_reference
        if not reference:
            return self._result(
                "B3", CheckStatus.INCONCLUSIVE,
                "Template ulasan tidak tersedia di rulebase/blast_context",
                confidence=facts.confidence,
            )
        actual = facts.review_text
        if not actual:
            return self._result(
                "B3", CheckStatus.INCONCLUSIVE,
                "Teks ulasan tidak terbaca", confidence=facts.confidence,
            )
        similarity = caption_similarity(actual, reference)
        if similarity >= 0.55:
            return self._result(
                "B3", CheckStatus.PASS,
                f"Ulasan sesuai template (kemiripan {similarity:.0%})",
                confidence=facts.confidence,
            )
        return self._fail(
            "B3", f"Ulasan tidak sesuai template (kemiripan {similarity:.0%})",
            sub, facts.confidence,
        )

    def _check_b4(self, sub, merged, facts, story_note) -> CheckResult:
        if facts.rating is None:
            return self._result(
                "B4", CheckStatus.INCONCLUSIVE,
                "Rating tidak terbaca dari screenshot", confidence=facts.confidence,
            )
        if facts.rating < 5:
            return self._fail(
                "B4", f"Rating {facts.rating:g} bintang, wajib 5", sub, facts.confidence
            )
        return self._result(
            "B4", CheckStatus.PASS, "Rating 5 bintang", confidence=facts.confidence
        )

    def _check_b5(self, sub, merged, facts, story_note) -> CheckResult:
        expected = ((merged.raw_task.get("links") or {}).get("target") or "")
        if not expected:
            return self._result(
                "B5", CheckStatus.NOT_APPLICABLE,
                "Task tidak mendefinisikan target vote", confidence=facts.confidence,
            )
        if not facts.vote_target:
            return self._result(
                "B5", CheckStatus.INCONCLUSIVE,
                "Target vote tidak terbaca", confidence=facts.confidence,
            )
        if _same_domain_or_text(expected, facts.vote_target):
            return self._result(
                "B5", CheckStatus.PASS,
                f"Vote pada campaign yang benar ({truncate(facts.vote_target, 60)})",
                confidence=facts.confidence,
            )
        return self._fail(
            "B5", f"Vote pada campaign berbeda: {truncate(facts.vote_target, 60)}",
            sub, facts.confidence,
        )

    def _check_b6(self, sub, merged, facts, story_note) -> CheckResult:
        # Honest answers are only detectable in aggregate (see batch summary).
        return self._result(
            "B6", CheckStatus.PASS,
            "Dicek agregat: distribusi jawaban dianalisis per batch, bukan per orang",
            confidence=0.5,
        )

    # ── summaries ──────────────────────────────────────────────────────────
    def denda_breakdown(self, result: ValidationResult) -> list[tuple[str, int, str]]:
        return [
            (check.check_id, check.denda, check.detail)
            for check in result.violations
        ]

    def summary_line(self, result: ValidationResult) -> str:
        if result.status == ResultStatus.EXEMPT:
            return f"{result.submission.nama}: EXEMPT ({result.angel_card_note})"
        if not result.violations:
            return f"{result.submission.nama}: AMAN"
        parts = ", ".join(
            f"{check.check_id} -{format_rupiah(check.denda)}" for check in result.violations
        )
        return f"{result.submission.nama}: {format_rupiah(result.total_denda)} ({parts})"


def _minutes(value: int):
    from datetime import timedelta

    return timedelta(minutes=value)


def _expected_slide_count(task: dict[str, Any]) -> int | None:
    """Parse "slide 1 - 4 pada G drive" -> 4."""
    import re

    block = task.get("feed") or task.get("story") or {}
    media = str(block.get("media") or "")
    numbers = [int(n) for n in re.findall(r"\b(\d{1,2})\b", media)]
    if len(numbers) >= 2:
        return max(numbers) - min(numbers) + 1
    return None


def _expected_platforms(task: dict[str, Any]) -> list[str]:
    links = task.get("links") or {}
    platforms: list[str] = []
    if "instagram" in links:
        platforms.append("instagram")
    if "tiktok" in links:
        platforms.append("tiktok")
    return platforms or ["instagram"]


def _same_domain_or_text(expected: str, actual: str) -> bool:
    from urllib.parse import urlparse

    def domain(url: str) -> str:
        try:
            return urlparse(url if "://" in url else f"https://{url}").netloc.lower()
        except ValueError:
            return ""

    expected_domain = domain(expected)
    actual_domain = domain(actual)
    if expected_domain and actual_domain and expected_domain in actual_domain:
        return True
    return expected.lower().strip() in actual.lower()
