"""Agent-based submission evaluation and rule checking.

Replaces the legacy RulebaseEngine by evaluating submissions against rules
defined in Markdown files and indexed in the vector store.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from typing import Any, Iterable, Sequence

from .. import config
from ..data_layer import AngelEntry
from ..models import (
    BlastContext,
    CheckResult,
    CheckStatus,
    LintIssue,
    MergedContext,
    ResultStatus,
    Submission,
    TaskCatalogEntry,
    TaskType,
    ValidationResult,
    VisionFacts,
)
from ..utils import (
    caption_similarity,
    extract_mentions,
    format_rupiah,
    handle_similarity,
    name_similarity,
    normalize_caption,
    normalize_handle,
    parse_dt,
    truncate,
    unique,
    word_count,
)
from . import knowledge

logger = logging.getLogger(__name__)


def _handle_key(handle: str | None) -> str:
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


def merge_task_context(
    task_id: str | None,
    blast_context: dict[str, BlastContext] | None = None,
) -> MergedContext:
    """Merge catalog task metadata with live blast_context."""
    task_id = (task_id or "").upper().strip()
    task = knowledge.get_task(task_id) if task_id else None
    blast = blast_context.get(task_id) if (blast_context and task_id) else None

    task_type = task.task_type if task else ("BZ" if task_id.startswith("BZ") else "PP")
    platforms = list(task.platforms) if task else (["Story", "Feed"] if task_type == "PP" else [])
    tags = list(task.tags) if task else []
    hashtags = list(task.hashtags) if task else []
    caption_raw = task.caption if task else ""
    if task and task.raw.get("Caption Verbatim"):
        caption_raw = task.raw["Caption Verbatim"]
    if caption_raw.lower().startswith("sesuai") or "lihat caption_verbatim" in caption_raw.lower():
        caption_raw = ""
    caption_reference = normalize_caption(caption_raw) if caption_raw else ""
    keep_hours = (task.story.get("keep_hours") if task else 24) or 24
    sources = ["markdown_rules"]
    warnings: list[str] = []

    if blast:
        sources.append(f"blast:{blast.blast_message_id}")
        tags.extend(blast.tags)
        hashtags.extend(blast.hashtags)
        if blast.caption_referensi:
            caption_reference = normalize_caption(blast.caption_referensi)

    jadwal = parse_dt(task.jadwal_post, default_time=(0, 0)) if (task and task.jadwal_post) else None
    deadline = parse_dt(task.deadline, default_time=(23, 59)) if (task and task.deadline) else None
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
        raw_task=task.raw if task else {},
        platforms=platforms,
        jadwal_post=jadwal,
        deadline=deadline,
        blast_date=blast_date,
        caption_reference=caption_reference,
        tags=_dedupe_handles(tags),
        hashtags=_dedupe_hashtags(hashtags),
        keep_hours=keep_hours,
        blast=blast,
        sources=sources,
        warnings=warnings,
        form_link=task.form_link if task else "",
        drive_link=(blast.drive_folder if blast and blast.drive_folder else (task.drive_link if task else "")) or "",
    )

    if not merged.timing_consistent:
        merged.warnings.append(
            "Data aturan tidak konsisten: deadline lebih awal dari jadwal post. "
            "C2/C10 di-flag untuk review manual, bukan didenda otomatis."
        )
    if not task:
        merged.warnings.append("Task tidak ditemukan di katalog aturan — validasi memakai generic checks.")
    if task and not merged.caption_reference and merged.is_feed:
        merged.warnings.append("Caption referensi tidak tersedia. C4 akan INCONCLUSIVE.")

    return merged


def check_meta(check_id: str) -> dict[str, Any]:
    return knowledge.get_check_meta(check_id)


def check_denda(check_id: str) -> int:
    return int(check_meta(check_id).get("denda") or 0)


def applicable_checks(task_id: str | None, task_type: str | None = None) -> list[str]:
    """Return applicable check IDs for a task."""
    resolved_type = task_type or ("BUZZER" if (task_id and task_id.upper().startswith("BZ")) else "PP")
    if resolved_type.upper() in ("BUZZER", "BZ"):
        task = knowledge.get_task(task_id or "")
        types = set()
        if task:
            types.update(task.raw.get("tipe_validasi") or [])
            if isinstance(task.raw.get("Tipe Validasi"), str):
                types.update(s.strip() for s in task.raw["Tipe Validasi"].split(","))
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
        if (task_id and task_id.upper() == "BZ-07") or "jawaban_jujur" in types:
            checks.append("B6")
        checks.append("B7")
        return checks

    merged = merge_task_context(task_id)
    checks = ["C1", "C2"]
    if merged.is_feed:
        checks.extend(["C3", "C4", "C5", "C6", "C8", "C9"])
    else:
        checks.extend(["C4", "C5", "C6"])
    if merged.is_story:
        checks.append("C7")
    checks.append("C10")
    return checks


def check_angel_exemption(
    nama: str,
    task_id: str | None,
    registry: Sequence[AngelEntry],
    blast_date: datetime | None = None,
) -> tuple[bool, str]:
    """Angel Card exempts PP and Buzzer tasks bought on or before blast date."""
    task_id = (task_id or "").upper().strip()
    if task_id.startswith("SPONSOR"):
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

    reference = blast_date or merge_task_context(task_id).blast_date
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


def _minutes(value: int):
    from datetime import timedelta
    return timedelta(minutes=value)


def _expected_slide_count(task: dict[str, Any]) -> int | None:
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


# ── Grounded Check Implementations (Pure Functions) ────────────────────────
def _result(check_id: str, status: str, detail: str, denda: int | None = None, evidence: Sequence[str] | None = None, confidence: float = 0.0) -> CheckResult:
    meta = check_meta(check_id)
    return CheckResult(
        check_id=check_id,
        name=meta.get("nama", check_id),
        status=status,
        denda=denda if denda is not None else 0,
        detail=detail,
        evidence=list(evidence or []),
        confidence=confidence,
    )


def _fail(check_id: str, detail: str, submission: Submission, confidence: float) -> CheckResult:
    return _result(
        check_id,
        CheckStatus.FAIL,
        detail,
        denda=check_denda(check_id),
        evidence=submission.links[:3],
        confidence=confidence,
    )


def _eval_c1(sub: Submission, merged: MergedContext, facts: VisionFacts, story_note: str | None = None) -> CheckResult:
    if facts.followers is None and facts.is_private is None:
        return _result(
            "C1", CheckStatus.INCONCLUSIVE,
            "Jumlah followers tidak terbaca dari bukti", confidence=facts.confidence,
        )
    if facts.is_private:
        return _fail("C1", "Akun Instagram di-set private", sub, facts.confidence)
    if facts.followers is not None and facts.followers < config.MIN_IG_FOLLOWERS:
        return _fail(
            "C1",
            f"Followers {facts.followers} < {config.MIN_IG_FOLLOWERS}",
            sub,
            facts.confidence,
        )
    if facts.ig_username_seen and sub.ig_username:
        similarity = handle_similarity(facts.ig_username_seen, sub.ig_username)
        if similarity < 0.8:
            return _result(
                "C1",
                CheckStatus.FLAG,
                f"Username di bukti (@{normalize_handle(facts.ig_username_seen)}) "
                f"berbeda dengan spreadsheet (@{normalize_handle(sub.ig_username)}) "
                "— kemungkinan ganti akun, butuh konfirmasi koor",
                confidence=facts.confidence,
            )
    return _result(
        "C1", CheckStatus.PASS,
        f"Followers {facts.followers if facts.followers is not None else '?'} "
        f"≥ {config.MIN_IG_FOLLOWERS}, akun publik",
        confidence=facts.confidence,
    )


def _eval_c2(sub: Submission, merged: MergedContext, facts: VisionFacts, story_note: str | None = None) -> CheckResult:
    posted = facts.posted_at or sub.timestamp
    if not merged.timing_consistent:
        return _result(
            "C2", CheckStatus.FLAG,
            "Rulebase: deadline mendahului jadwal post — timing tidak bisa dinilai otomatis",
            confidence=facts.confidence,
        )
    if posted is None or merged.jadwal_post is None:
        return _result(
            "C2", CheckStatus.INCONCLUSIVE,
            "Timestamp post / jadwal tidak tersedia", confidence=facts.confidence,
        )
    early_limit = merged.jadwal_post - _minutes(config.POST_EARLY_TOLERANCE_MINUTES)
    if posted < early_limit:
        delta = int((merged.jadwal_post - posted).total_seconds() // 60)
        return _fail(
            "C2",
            f"Post {delta} menit lebih awal dari jadwal {merged.jadwal_post:%d %b %Y %H:%M}",
            sub,
            facts.confidence,
        )
    if merged.deadline and posted > merged.deadline:
        delta = int((posted - merged.deadline).total_seconds() // 3600)
        return _fail(
            "C2",
            f"Post terlambat {delta} jam dari deadline {merged.deadline:%d %b %Y %H:%M}",
            sub,
            facts.confidence,
        )
    return _result(
        "C2", CheckStatus.PASS,
        f"Post {posted:%d %b %Y %H:%M} sesuai jadwal",
        confidence=facts.confidence,
    )


def _eval_c3(sub: Submission, merged: MergedContext, facts: VisionFacts, story_note: str | None = None) -> CheckResult:
    if facts.has_flag("carousel_order_mismatch"):
        return _fail("C3", "Urutan slide/carousel tidak sesuai referensi", sub, facts.confidence)
    if not facts.carousel_slides:
        return _result(
            "C3", CheckStatus.NOT_APPLICABLE,
            "Tidak ada carousel terdeteksi di bukti", confidence=facts.confidence,
        )
    expected = _expected_slide_count(merged.raw_task)
    if expected and len(facts.carousel_slides) < expected:
        return _result(
            "C3", CheckStatus.FLAG,
            f"Carousel {len(facts.carousel_slides)} slide, referensi {expected} slide — butuh cek manual",
            confidence=facts.confidence,
        )
    return _result(
        "C3", CheckStatus.PASS,
        f"Carousel {len(facts.carousel_slides)} slide, urutan sesuai",
        confidence=facts.confidence,
    )


def _eval_c4(sub: Submission, merged: MergedContext, facts: VisionFacts, story_note: str | None = None) -> CheckResult:
    reference = merged.caption_reference
    if not reference or reference.lower().startswith("sesuai") or "lihat caption_verbatim" in reference.lower():
        return _result(
            "C4", CheckStatus.INCONCLUSIVE,
            "Caption referensi tidak tersedia (rulebase/blast_context kosong)",
            confidence=facts.confidence,
        )
    actual = facts.caption_text
    if not actual:
        return _result(
            "C4", CheckStatus.INCONCLUSIVE,
            "Caption tidak terbaca dari screenshot", confidence=facts.confidence,
        )
    similarity = caption_similarity(actual, reference)
    if similarity >= 0.97:
        return _result(
            "C4", CheckStatus.PASS,
            f"Caption sesuai referensi (kemiripan {similarity:.0%})",
            confidence=facts.confidence,
        )
    return _fail(
        "C4",
        f"Caption tidak sesuai referensi (kemiripan {similarity:.0%}, wajib verbatim {len(reference)} karakter)",
        sub,
        facts.confidence,
    )


def _eval_c5(sub: Submission, merged: MergedContext, facts: VisionFacts, story_note: str | None = None) -> CheckResult:
    required = merged.tags
    if not required:
        return _result("C5", CheckStatus.NOT_APPLICABLE, "Task tidak mendefinisikan tag wajib", confidence=facts.confidence)
    if not facts.mentions and not facts.caption_text:
        return _result("C5", CheckStatus.INCONCLUSIVE, "Tidak ada teks caption yang bisa dibaca", confidence=facts.confidence)
    present = {_handle_key(mention) for mention in facts.mentions}
    present |= {_handle_key(handle) for handle in extract_mentions(facts.caption_text)}
    missing = [tag for tag in required if _handle_key(tag) not in present]
    if missing:
        return _fail("C5", "Tag wajib tidak ditemukan: " + ", ".join(missing), sub, facts.confidence)
    return _result("C5", CheckStatus.PASS, f"Semua tag wajib ada ({len(required)})", confidence=facts.confidence)


def _eval_c6(sub: Submission, merged: MergedContext, facts: VisionFacts, story_note: str | None = None) -> CheckResult:
    required = merged.hashtags
    if not required:
        return _result("C6", CheckStatus.NOT_APPLICABLE, "Task tidak mendefinisikan hashtag wajib", confidence=facts.confidence)
    if not facts.hashtags and not facts.all_text():
        return _result("C6", CheckStatus.INCONCLUSIVE, "Hashtag tidak terbaca dari bukti", confidence=facts.confidence)
    present = {tag.lower() for tag in facts.hashtags}
    missing = [tag for tag in required if tag.lower() not in present]
    if missing:
        return _fail("C6", f"Hashtag kurang: {', '.join(missing)} ({len(present)}/{len(required)} ada)", sub, facts.confidence)
    return _result("C6", CheckStatus.PASS, f"Semua {len(required)} hashtag wajib ada", confidence=facts.confidence)


def _eval_c7(sub: Submission, merged: MergedContext, facts: VisionFacts, story_note: str | None = None) -> CheckResult:
    if facts.story_uploading_text:
        return _fail("C7", "Screenshot menunjukkan story belum selesai di-upload", sub, facts.confidence)
    if facts.story_live is True:
        detail = f"Story live, keep {merged.keep_hours} jam"
        if story_note:
            detail += f" (bukti tambahan: {story_note})"
        return _result("C7", CheckStatus.PASS, detail, confidence=facts.confidence)
    if facts.story_live is False:
        return _fail(
            "C7",
            "Story tidak tersedia / sudah expired" + (f" — {story_note}" if story_note else ""),
            sub,
            facts.confidence,
        )
    return _result(
        "C7", CheckStatus.INCONCLUSIVE,
        "Status story tidak bisa ditentukan dari bukti" + (f" — {story_note}" if story_note else ""),
        confidence=facts.confidence,
    )


def _eval_c8(sub: Submission, merged: MergedContext, facts: VisionFacts, story_note: str | None = None) -> CheckResult:
    if facts.has_flag("post_missing"):
        return _fail("C8", "Feed sudah tidak ada / dihapus saat validasi", sub, facts.confidence)
    return _result(
        "C8", CheckStatus.PASS,
        "Feed masih live" + (
            f" (wajib sampai {merged.do_not_delete_until:%d %b %Y})"
            if merged.do_not_delete_until else ""
        ),
        confidence=facts.confidence,
    )


def _eval_c9(sub: Submission, merged: MergedContext, facts: VisionFacts, story_note: str | None = None) -> CheckResult:
    if facts.has_flag("aspect_ratio_mismatch"):
        return _fail("C9", f"Aspect ratio {facts.aspect_ratio or '?'} tidak sesuai referensi", sub, facts.confidence)
    if not facts.aspect_ratio:
        return _result("C9", CheckStatus.NOT_APPLICABLE, "Dimensi gambar tidak terbaca", confidence=facts.confidence)
    return _result("C9", CheckStatus.PASS, f"Aspect ratio {facts.aspect_ratio} sesuai", confidence=facts.confidence)


def _eval_c10(sub: Submission, merged: MergedContext, facts: VisionFacts, story_note: str | None = None) -> CheckResult:
    return _eval_bukti_timely("C10", sub, merged, facts)


def _eval_b1(sub: Submission, merged: MergedContext, facts: VisionFacts, story_note: str | None = None) -> CheckResult:
    expected = _expected_platforms(merged.raw_task)
    detected = {platform.lower() for platform in facts.like_platforms}
    if not detected and facts.likes is None:
        return _result("B1", CheckStatus.INCONCLUSIVE, "Like tidak terbaca dari bukti", confidence=facts.confidence)
    missing = [platform for platform in expected if platform not in detected]
    if missing:
        return _fail("B1", "Like belum ditemukan di: " + ", ".join(missing), sub, facts.confidence)
    return _result("B1", CheckStatus.PASS, f"Like terdeteksi di {', '.join(sorted(detected))}", confidence=facts.confidence)


def _eval_b2(sub: Submission, merged: MergedContext, facts: VisionFacts, story_note: str | None = None) -> CheckResult:
    from ..utils import is_emoji_only
    custom = merged.raw_task.get("custom_requirements") or {}
    min_words = int(custom.get("komentar_natural_min_length") or 5)
    comment = facts.comment_text
    if not comment:
        return _result("B2", CheckStatus.INCONCLUSIVE, "Komentar tidak terbaca dari screenshot", confidence=facts.confidence)
    if is_emoji_only(comment):
        return _fail("B2", "Komentar hanya emoji", sub, facts.confidence)
    count = word_count(comment)
    if count < min_words:
        return _fail("B2", f"Komentar terlalu pendek ({count} kata, minimal {min_words})", sub, facts.confidence)
    return _result("B2", CheckStatus.PASS, f"Komentar natural ({count} kata)", confidence=facts.confidence)


def _eval_b3(sub: Submission, merged: MergedContext, facts: VisionFacts, story_note: str | None = None) -> CheckResult:
    reference = merged.caption_reference
    if not reference:
        return _result("B3", CheckStatus.INCONCLUSIVE, "Template ulasan tidak tersedia di rulebase/blast_context", confidence=facts.confidence)
    actual = facts.review_text
    if not actual:
        return _result("B3", CheckStatus.INCONCLUSIVE, "Teks ulasan tidak terbaca", confidence=facts.confidence)
    similarity = caption_similarity(actual, reference)
    if similarity >= 0.55:
        return _result("B3", CheckStatus.PASS, f"Ulasan sesuai template (kemiripan {similarity:.0%})", confidence=facts.confidence)
    return _fail("B3", f"Ulasan tidak sesuai template (kemiripan {similarity:.0%})", sub, facts.confidence)


def _eval_b4(sub: Submission, merged: MergedContext, facts: VisionFacts, story_note: str | None = None) -> CheckResult:
    if facts.rating is None:
        return _result("B4", CheckStatus.INCONCLUSIVE, "Rating tidak terbaca dari screenshot", confidence=facts.confidence)
    if facts.rating < 5:
        return _fail("B4", f"Rating {facts.rating:g} bintang, wajib 5", sub, facts.confidence)
    return _result("B4", CheckStatus.PASS, "Rating 5 bintang", confidence=facts.confidence)


def _eval_b5(sub: Submission, merged: MergedContext, facts: VisionFacts, story_note: str | None = None) -> CheckResult:
    expected = ((merged.raw_task.get("links") or {}).get("target") or "")
    if not expected:
        return _result("B5", CheckStatus.NOT_APPLICABLE, "Task tidak mendefinisikan target vote", confidence=facts.confidence)
    if not facts.vote_target:
        return _result("B5", CheckStatus.INCONCLUSIVE, "Target vote tidak terbaca", confidence=facts.confidence)
    if _same_domain_or_text(expected, facts.vote_target):
        return _result("B5", CheckStatus.PASS, f"Vote pada campaign yang benar ({truncate(facts.vote_target, 60)})", confidence=facts.confidence)
    return _fail("B5", f"Vote pada campaign berbeda: {truncate(facts.vote_target, 60)}", sub, facts.confidence)


def _eval_b6(sub: Submission, merged: MergedContext, facts: VisionFacts, story_note: str | None = None) -> CheckResult:
    return _result("B6", CheckStatus.PASS, "Dicek agregat: distribusi jawaban dianalisis per batch, bukan per orang", confidence=0.5)


def _eval_b7(sub: Submission, merged: MergedContext, facts: VisionFacts, story_note: str | None = None) -> CheckResult:
    return _eval_bukti_timely("B7", sub, merged, facts)


def _eval_bukti_timely(check_id: str, sub: Submission, merged: MergedContext, facts: VisionFacts) -> CheckResult:
    if not sub.has_evidence:
        return _fail(check_id, "Tidak ada bukti yang dikirim", sub, 1.0)
    if not merged.timing_consistent:
        return _result(
            check_id, CheckStatus.FLAG,
            "Rulebase: deadline mendahului jadwal — ketepatan bukti tidak bisa dinilai otomatis",
            confidence=facts.confidence,
        )
    if sub.timestamp is None:
        return _result(check_id, CheckStatus.FLAG, "Timestamp submission tidak terbaca", confidence=facts.confidence)
    if merged.deadline and sub.timestamp > merged.deadline:
        delta = int((sub.timestamp - merged.deadline).total_seconds() // 3600)
        return _fail(
            check_id,
            f"Bukti dikirim {delta} jam setelah deadline {merged.deadline:%d %b %Y %H:%M}",
            sub,
            facts.confidence,
        )
    return _result(check_id, CheckStatus.PASS, f"Bukti dikirim {sub.timestamp:%d %b %Y %H:%M}", confidence=facts.confidence)


def evaluate_with_agent(
    submission: Submission,
    task_id: str | None,
    facts: VisionFacts,
    story_note: str | None = None,
    extra_warnings: Sequence[str] | None = None,
    blast_context: dict[str, BlastContext] | None = None,
) -> ValidationResult:
    """Evaluate submission evidence and facts against task rules."""
    merged = merge_task_context(task_id, blast_context)
    result = ValidationResult(
        submission=submission,
        status=ResultStatus.VERIFIED,
        task_id=task_id or merged.task_id,
        task_type=merged.task_type,
        facts=facts,
        blast_date=merged.blast_date,
    )
    if extra_warnings:
        for warning in extra_warnings:
            if warning not in merged.warnings:
                result.notes.append(warning)
    if story_note:
        result.notes.append(story_note)

    handlers = {
        "C1": _eval_c1,
        "C2": _eval_c2,
        "C3": _eval_c3,
        "C4": _eval_c4,
        "C5": _eval_c5,
        "C6": _eval_c6,
        "C7": _eval_c7,
        "C8": _eval_c8,
        "C9": _eval_c9,
        "C10": _eval_c10,
        "B1": _eval_b1,
        "B2": _eval_b2,
        "B3": _eval_b3,
        "B4": _eval_b4,
        "B5": _eval_b5,
        "B6": _eval_b6,
        "B7": _eval_b7,
    }

    checks: list[CheckResult] = []
    applicable = applicable_checks(task_id, merged.task_type)
    for cid in applicable:
        h = handlers.get(cid)
        if h:
            checks.append(h(submission, merged, facts, story_note))
        else:
            meta = check_meta(cid)
            checks.append(CheckResult(cid, meta.get("nama", cid), CheckStatus.PASS, 0, "Sesuai ketentuan", confidence=0.8))

    result.checks = checks
    if any(check.is_violation for check in result.checks):
        result.status = ResultStatus.FLAGGED
    elif facts.image_count == 0 and submission.has_evidence:
        result.status = ResultStatus.PENDING_MANUAL_REVIEW
        result.notes.append("Bukti tidak bisa diunduh / dibaca — perlu review manual")
    else:
        result.status = ResultStatus.VERIFIED
    return result


def denda_breakdown(result: ValidationResult) -> list[tuple[str, int, str]]:
    return [
        (check.check_id, check.denda, check.detail)
        for check in result.violations
    ]


def summary_line(result: ValidationResult) -> str:
    if result.status == ResultStatus.EXEMPT:
        return f"{result.submission.nama}: EXEMPT ({result.angel_card_note})"
    if not result.violations:
        return f"{result.submission.nama}: AMAN"
    parts = ", ".join(
        f"{check.check_id} -{format_rupiah(check.denda)}" for check in result.violations
    )
    return f"{result.submission.nama}: {format_rupiah(result.total_denda)} ({parts})"


def lint_rules(rulebase: Any = None) -> list[LintIssue]:
    """Verify consistency of rules catalog."""
    issues: list[LintIssue] = []
    tasks = knowledge.all_tasks()
    for task in tasks:
        merged = merge_task_context(task.id)
        if not merged.timing_consistent:
            issues.append(
                LintIssue("ERROR", "DEADLINE_BEFORE_JADWAL", f"Deadline {merged.deadline} mendahului jadwal post {merged.jadwal_post}", task.id)
            )
        if not task.platforms and task.task_type == "PP":
            issues.append(
                LintIssue("WARN", "NO_PLATFORM", "Tugas PP tanpa platform", task.id)
            )
        if task.sheet and task.sheet in config.SHEET_TO_TASK:
            mapped = config.SHEET_TO_TASK[task.sheet]
            if mapped and mapped != task.id:
                issues.append(
                    LintIssue("WARN", "SHEET_MAP_CONFLICT", f"Sheet '{task.sheet}' mapped to {task.id} in rules but {mapped} in SHEET_TO_TASK", task.id)
                )

    sheet_map: dict[str, list[str]] = {}
    for task in tasks:
        if task.sheet:
            norm = task.sheet.strip().lower()
            sheet_map.setdefault(norm, []).append(task.id)
    for sheet_norm, ids in sheet_map.items():
        if len(ids) > 1:
            issues.append(
                LintIssue("WARN", "DUPLICATE_SHEET", f"Sheet '{sheet_norm}' dipetakan ke multiple tasks: {', '.join(ids)}", ids[0])
            )
    return issues


lint_rulebase = lint_rules
