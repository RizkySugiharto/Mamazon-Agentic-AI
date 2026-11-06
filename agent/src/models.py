"""
Core data types shared by every module.

Checks consume `VisionFacts` only, never raw images, so the validation logic is
pure and unit-testable without Drive, Tencent or Discord.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


# ─── Status vocabularies ────────────────────────────────────────────────────
class CheckStatus:
    PASS = "PASS"
    FAIL = "FAIL"
    FLAG = "FLAG"                      # suspicious, needs a human
    INCONCLUSIVE = "INCONCLUSIVE"      # not enough evidence to judge
    NOT_APPLICABLE = "NOT_APPLICABLE"


class ResultStatus:
    VERIFIED = "VERIFIED"
    FLAGGED = "FLAGGED"
    EXEMPT = "EXEMPT"                  # Angel Card covers this task
    PENDING_MANUAL_REVIEW = "PENDING_MANUAL_REVIEW"
    ERROR = "ERROR"


class TaskType:
    PP = "PP"
    BUZZER = "BUZZER"
    SPONSOR = "SPONSOR"


class BlastStatus:
    DRAFT = "DRAFT"
    HELD = "HELD"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    SENT = "SENT"


# ─── Submission ─────────────────────────────────────────────────────────────
@dataclass
class Submission:
    """One row of a submission sheet."""

    row_index: int
    nama: str
    nim: str = ""
    divisi: str = ""
    ig_username: str = ""
    raw_link_cell: str = ""
    links: list[str] = field(default_factory=list)
    file_ids: list[str] = field(default_factory=list)
    timestamp: datetime | None = None
    sheet_name: str = ""
    task_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def key(self) -> tuple[str, str, str]:
        """Idempotency key: (sheet, name, nim)."""
        return (self.sheet_name, self.nama.strip().lower(), self.nim.strip())

    @property
    def has_evidence(self) -> bool:
        return bool(self.links)

    @property
    def multi_link(self) -> bool:
        return len(self.links) > 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "row_index": self.row_index,
            "nama": self.nama,
            "nim": self.nim,
            "divisi": self.divisi,
            "ig_username": self.ig_username,
            "links": self.links,
            "file_ids": self.file_ids,
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "sheet_name": self.sheet_name,
            "task_id": self.task_id,
        }


# ─── Vision ─────────────────────────────────────────────────────────────────
@dataclass
class VisionFacts:
    """
    Everything the checks need to know about a set of evidence screenshots.

    Both MockVision and TencentVision must produce this shape. Unknown values
    stay None so checks can return INCONCLUSIVE instead of guessing.
    """

    image_count: int = 0
    caption_text: str = ""
    comment_text: str = ""
    hashtags: list[str] = field(default_factory=list)
    mentions: list[str] = field(default_factory=list)
    followers: int | None = None
    is_private: bool | None = None
    posted_at: datetime | None = None
    story_live: bool | None = None
    story_uploading_text: bool = False
    likes: int | None = None
    like_platforms: list[str] = field(default_factory=list)
    comment_platform: str | None = None
    rating: float | None = None
    vote_target: str | None = None
    ig_username_seen: str | None = None
    aspect_ratio: str | None = None
    carousel_slides: list[str] = field(default_factory=list)
    review_text: str = ""
    anomaly_flags: list[str] = field(default_factory=list)
    confidence: float = 0.0
    provider: str = "unknown"
    raw: dict[str, Any] = field(default_factory=dict)

    def flag(self, name: str) -> None:
        if name not in self.anomaly_flags:
            self.anomaly_flags.append(name)

    def has_flag(self, name: str) -> bool:
        return name in self.anomaly_flags

    def all_text(self) -> str:
        return "\n".join(
            part for part in (self.caption_text, self.comment_text, self.review_text) if part
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "image_count": self.image_count,
            "caption_text": self.caption_text,
            "comment_text": self.comment_text,
            "hashtags": self.hashtags,
            "mentions": self.mentions,
            "followers": self.followers,
            "is_private": self.is_private,
            "posted_at": self.posted_at.isoformat() if self.posted_at else None,
            "story_live": self.story_live,
            "likes": self.likes,
            "like_platforms": self.like_platforms,
            "rating": self.rating,
            "vote_target": self.vote_target,
            "ig_username_seen": self.ig_username_seen,
            "aspect_ratio": self.aspect_ratio,
            "carousel_slides": self.carousel_slides,
            "review_text": self.review_text,
            "anomaly_flags": self.anomaly_flags,
            "confidence": self.confidence,
            "provider": self.provider,
        }


@dataclass
class DownloadedImage:
    """Result of fetching one evidence file."""

    file_id: str
    path: str | None = None
    sha256: str = ""
    size_bytes: int = 0
    dedup_hit: bool = False
    error: str | None = None
    mime_type: str = "image/jpeg"

    @property
    def ok(self) -> bool:
        return self.error is None and bool(self.path)


# ─── Checks ─────────────────────────────────────────────────────────────────
@dataclass
class CheckResult:
    check_id: str
    name: str
    status: str
    denda: int = 0
    detail: str = ""
    evidence: list[str] = field(default_factory=list)
    confidence: float = 0.0

    @property
    def is_violation(self) -> bool:
        return self.status == CheckStatus.FAIL

    def to_dict(self) -> dict[str, Any]:
        return {
            "check_id": self.check_id,
            "name": self.name,
            "status": self.status,
            "denda": self.denda,
            "detail": self.detail,
            "evidence": self.evidence,
            "confidence": round(self.confidence, 2),
        }


@dataclass
class ValidationResult:
    submission: Submission
    status: str = ResultStatus.VERIFIED
    checks: list[CheckResult] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    angel_card_exempt: bool = False
    angel_card_note: str = ""
    blast_date: datetime | None = None
    validated_at: datetime | None = None
    facts: VisionFacts | None = None
    task_type: str = TaskType.PP
    task_id: str | None = None
    skipped: bool = False

    @property
    def violations(self) -> list[CheckResult]:
        return [c for c in self.checks if c.is_violation]

    @property
    def total_denda(self) -> int:
        return sum(c.denda for c in self.violations)

    @property
    def violation_ids(self) -> list[str]:
        return [c.check_id for c in self.violations]

    @property
    def violation_summary(self) -> str:
        return "; ".join(
            f"{c.check_id}: {c.detail}" for c in self.violations
        ) or "-"

    @property
    def needs_review(self) -> bool:
        return self.status in (
            ResultStatus.PENDING_MANUAL_REVIEW,
            ResultStatus.ERROR,
        ) or any(c.status == CheckStatus.INCONCLUSIVE for c in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {
            "sheet_name": self.submission.sheet_name,
            "task_id": self.task_id or self.submission.task_id,
            "nama": self.submission.nama,
            "nim": self.submission.nim,
            "divisi": self.submission.divisi,
            "ig_username": self.submission.ig_username,
            "status": self.status,
            "total_denda": self.total_denda,
            "violations": [c.to_dict() for c in self.violations],
            "checks": [c.to_dict() for c in self.checks],
            "notes": self.notes,
            "angel_card_exempt": self.angel_card_exempt,
            "angel_card_note": self.angel_card_note,
            "evidence_links": self.submission.links,
            "blast_date": self.blast_date.isoformat() if self.blast_date else None,
            "validated_at": self.validated_at.isoformat() if self.validated_at else None,
            "confidence": round(self.facts.confidence, 2) if self.facts else 0.0,
        }


@dataclass
class ValidationBatch:
    sheet_name: str
    task_id: str | None
    task_type: str
    results: list[ValidationResult] = field(default_factory=list)
    started_at: datetime | None = None
    finished_at: datetime | None = None
    skipped_existing: int = 0
    duplicates: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    blast_context_used: bool = False
    blast_message_id: str | None = None
    cancelled: bool = False
    divisi: str = ""

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def verified(self) -> list[ValidationResult]:
        return [r for r in self.results if r.status == ResultStatus.VERIFIED]

    @property
    def exempt(self) -> list[ValidationResult]:
        return [r for r in self.results if r.status == ResultStatus.EXEMPT]

    @property
    def flagged(self) -> list[ValidationResult]:
        return [r for r in self.results if r.status == ResultStatus.FLAGGED]

    @property
    def pending(self) -> list[ValidationResult]:
        return [r for r in self.results if r.status == ResultStatus.PENDING_MANUAL_REVIEW]

    @property
    def total_denda(self) -> int:
        """Only FLAGGED rows count — pending review has not been decided yet."""
        return sum(result.total_denda for result in self.flagged)

    @property
    def total_denda_including_pending(self) -> int:
        return sum(result.total_denda for result in self.results)

    @property
    def duration_seconds(self) -> float:
        if not self.started_at or not self.finished_at:
            return 0.0
        return round((self.finished_at - self.started_at).total_seconds(), 2)


# ─── Blast context & drafts ─────────────────────────────────────────────────
@dataclass
class BlastContext:
    """Ketentuan tambahan extracted from #blast-tugas for one task."""

    task_id: str
    blast_message_id: str = ""
    blast_date: datetime | None = None
    caption_referensi: str = ""
    tags: list[str] = field(default_factory=list)
    hashtags: list[str] = field(default_factory=list)
    deadline: datetime | None = None
    drive_folder: str = ""
    form_link: str = ""
    notes: list[str] = field(default_factory=list)
    extracted_at: datetime | None = None
    confidence: float = 0.0
    raw_message: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "blast_message_id": self.blast_message_id,
            "blast_date": self.blast_date.isoformat() if self.blast_date else None,
            "caption_referensi": self.caption_referensi,
            "tags": self.tags,
            "hashtags": self.hashtags,
            "deadline": self.deadline.isoformat() if self.deadline else None,
            "drive_folder": self.drive_folder,
            "form_link": self.form_link,
            "notes": self.notes,
            "extracted_at": self.extracted_at.isoformat() if self.extracted_at else None,
            "confidence": self.confidence,
            "raw_message": self.raw_message,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BlastContext":
        from .utils import parse_dt

        def _dt(value: str | None) -> datetime | None:
            if not value:
                return None
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                return parse_dt(value)

        return cls(
            task_id=data.get("task_id", ""),
            blast_message_id=data.get("blast_message_id", ""),
            blast_date=_dt(data.get("blast_date")),
            caption_referensi=data.get("caption_referensi", ""),
            tags=list(data.get("tags") or []),
            hashtags=list(data.get("hashtags") or []),
            deadline=_dt(data.get("deadline")),
            drive_folder=data.get("drive_folder", ""),
            form_link=data.get("form_link", ""),
            notes=list(data.get("notes") or []),
            extracted_at=_dt(data.get("extracted_at")),
            confidence=float(data.get("confidence") or 0.0),
            raw_message=data.get("raw_message", ""),
        )


@dataclass
class BlastEntry:
    """One panitia line inside a blast draft."""

    nama: str
    divisi: str
    ig_username: str
    total_denda: int
    violations: list[dict[str, Any]] = field(default_factory=list)
    evidence_links: list[str] = field(default_factory=list)
    recurring: int = 0
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "nama": self.nama,
            "divisi": self.divisi,
            "ig_username": self.ig_username,
            "total_denda": self.total_denda,
            "violations": self.violations,
            "evidence_links": self.evidence_links,
            "recurring": self.recurring,
            "note": self.note,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BlastEntry":
        return cls(
            nama=data.get("nama", ""),
            divisi=data.get("divisi", ""),
            ig_username=data.get("ig_username", ""),
            total_denda=int(data.get("total_denda") or 0),
            violations=list(data.get("violations") or []),
            evidence_links=list(data.get("evidence_links") or []),
            recurring=int(data.get("recurring") or 0),
            note=data.get("note", ""),
        )


@dataclass
class BlastRecord:
    """Persisted blast draft + approval state."""

    blast_id: str
    sheet_name: str
    task_id: str | None = None
    status: str = BlastStatus.DRAFT
    created_at: datetime | None = None
    created_by: str = ""
    approved_by: str = ""
    approved_at: datetime | None = None
    held_by: str = ""
    held_reason: str = ""
    rejected_by: str = ""
    entries: list[BlastEntry] = field(default_factory=list)
    total_denda: int = 0
    message_id: str = ""
    channel_id: str = ""
    sent_chunks: int = 0

    @property
    def count(self) -> int:
        return len(self.entries)

    def to_dict(self) -> dict[str, Any]:
        return {
            "blast_id": self.blast_id,
            "sheet_name": self.sheet_name,
            "task_id": self.task_id,
            "status": self.status,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "created_by": self.created_by,
            "approved_by": self.approved_by,
            "approved_at": self.approved_at.isoformat() if self.approved_at else None,
            "held_by": self.held_by,
            "held_reason": self.held_reason,
            "rejected_by": self.rejected_by,
            "entries": [e.to_dict() for e in self.entries],
            "total_denda": self.total_denda,
            "message_id": self.message_id,
            "channel_id": self.channel_id,
            "sent_chunks": self.sent_chunks,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "BlastRecord":
        def _dt(value: str | None) -> datetime | None:
            if not value:
                return None
            try:
                return datetime.fromisoformat(value)
            except ValueError:
                return None

        return cls(
            blast_id=data.get("blast_id", ""),
            sheet_name=data.get("sheet_name", ""),
            task_id=data.get("task_id"),
            status=data.get("status", BlastStatus.DRAFT),
            created_at=_dt(data.get("created_at")),
            created_by=data.get("created_by", ""),
            approved_by=data.get("approved_by", ""),
            approved_at=_dt(data.get("approved_at")),
            held_by=data.get("held_by", ""),
            held_reason=data.get("held_reason", ""),
            rejected_by=data.get("rejected_by", ""),
            entries=[BlastEntry.from_dict(e) for e in data.get("entries") or []],
            total_denda=int(data.get("total_denda") or 0),
            message_id=data.get("message_id", ""),
            channel_id=data.get("channel_id", ""),
            sent_chunks=int(data.get("sent_chunks") or 0),
        )
