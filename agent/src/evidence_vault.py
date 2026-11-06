"""
Evidence Vault: structure the violations of a batch for #evidence and flag
panitia whose history reaches the recurring threshold.

The vault itself is the Drive folder + violations_log.csv; this module only
decides *what* to post and *who* needs escalation, so the Discord layer stays
thin and the logic stays testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Sequence

from . import config
from .models import ResultStatus, ValidationBatch, ValidationResult
from .output_generator import OutputGenerator


@dataclass
class EvidenceEntry:
    nama: str
    divisi: str
    ig_username: str
    task_id: str
    sheet_name: str
    violations: list[dict[str, Any]] = field(default_factory=list)
    total_denda: int = 0
    evidence_links: list[str] = field(default_factory=list)
    history_count: int = 0
    recurring: bool = False
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "nama": self.nama,
            "divisi": self.divisi,
            "ig_username": self.ig_username,
            "task_id": self.task_id,
            "sheet_name": self.sheet_name,
            "violations": self.violations,
            "total_denda": self.total_denda,
            "evidence_links": self.evidence_links,
            "history_count": self.history_count,
            "recurring": self.recurring,
            "notes": self.notes,
        }


class EvidenceVault:
    def __init__(self, outputs: OutputGenerator | None = None) -> None:
        self.outputs = outputs or OutputGenerator()

    def build(self, batch: ValidationBatch) -> list[EvidenceEntry]:
        entries: list[EvidenceEntry] = []
        for result in batch.results:
            if result.status != ResultStatus.FLAGGED or not result.violations:
                continue
            history = self.outputs.recurring_count(result.submission.nama)
            entries.append(
                EvidenceEntry(
                    nama=result.submission.nama,
                    divisi=result.submission.divisi,
                    ig_username=result.submission.ig_username,
                    task_id=result.task_id or result.submission.task_id or "",
                    sheet_name=batch.sheet_name,
                    violations=[
                        {
                            "rule_id": check.check_id,
                            "detail": check.detail,
                            "denda": check.denda,
                        }
                        for check in result.violations
                    ],
                    total_denda=result.total_denda,
                    evidence_links=result.submission.links[:3],
                    history_count=history,
                    recurring=history >= config.RECURRING_THRESHOLD,
                    notes=result.notes,
                )
            )
        entries.sort(key=lambda entry: (-entry.history_count, -entry.total_denda))
        return entries

    def escalations(self, entries: Sequence[EvidenceEntry]) -> list[EvidenceEntry]:
        return [entry for entry in entries if entry.recurring]

    def summary(self, entries: Sequence[EvidenceEntry]) -> str:
        if not entries:
            return "Tidak ada pelanggaran baru."
        recurring = self.escalations(entries)
        total = sum(entry.total_denda for entry in entries)
        text = (
            f"{len(entries)} pelanggaran, total Rp {total:,}".replace(",", ".")
        )
        if recurring:
            names = ", ".join(entry.nama for entry in recurring)
            text += f" | RECURRING (≥{config.RECURRING_THRESHOLD}x): {names}"
        return text

    def resolution_updates(self, batch: ValidationBatch) -> list[dict[str, Any]]:
        """Previously flagged rows that now pass — posted as RESOLVED updates."""
        updates: list[dict[str, Any]] = []
        for result in batch.results:
            if result.status != ResultStatus.VERIFIED:
                continue
            history = self.outputs.violation_history(result.submission.nama)
            resolved = [
                row
                for row in history
                if row.get("status") == "RESOLVED"
                and str(row.get("sheet_name", "")).strip().lower()
                == batch.sheet_name.strip().lower()
            ]
            if not resolved:
                continue
            updates.append(
                {
                    "nama": result.submission.nama,
                    "sheet_name": batch.sheet_name,
                    "resolved": [row.get("rule_id") for row in resolved],
                    "detail": "; ".join(row.get("catatan", "") for row in resolved),
                }
            )
        return updates
