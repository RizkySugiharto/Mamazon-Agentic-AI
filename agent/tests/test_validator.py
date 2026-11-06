"""End-to-end validator tests against the dummy dataset (mock Vision + Drive)."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Event
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from src.data_layer import DataLayer, RosterEntry, SheetResolution, TaskResolution
from src.sheets_client import XlsxSource
from src import config
from src.image_manager import ImageManager
from src.models import CheckResult, CheckStatus, ResultStatus, Submission, ValidationResult, VisionFacts
from src.output_generator import OUTPUT_LOCK, OutputGenerator
from src.rulebase_engine import RulebaseEngine
from src.validator import DivisionNotFoundError, SheetAmbiguousError, SheetNotFoundError, Validator
from src.vision_checker import MockVision


@pytest.fixture()
def validator(dataset_available, rulebase, outputs, images, tmp_path) -> Validator:
    data = DataLayer(rulebase=rulebase, source=XlsxSource(config.DUMMY_DATA_PATH))
    instance = Validator(
        data_layer=data,
        vision=MockVision(),
        images=images,
        outputs=outputs,
        blast_context={},
    )
    yield instance
    data.close()


class TestResolution:
    def test_unknown_sheet_raises_with_options(self, validator):
        with pytest.raises(SheetNotFoundError) as excinfo:
            validator.validate("PP 27 Oktober", limit=1)
        assert excinfo.value.options

    def test_ambiguous_sheet_raises(self, validator):
        with pytest.raises(SheetAmbiguousError) as excinfo:
            validator.validate("PP 26 Oktober", limit=1)
        assert "PP Feed 26 Oktober" in excinfo.value.options
        assert "PP Bundle 26 Oktober" in excinfo.value.options

    def test_preview_does_not_touch_vision(self, validator):
        preview = validator.preview("PP 29 Oktober")
        assert preview["rows"] == 42
        assert preview["multi_link"] == 34
        assert preview["task_id"] == "PP-01"
        assert preview["no_link"] == 0


class TestRun:
    def test_batch_counts_add_up(self, validator):
        batch = validator.validate("PP 29 Oktober", limit=10, actor="test")
        assert batch.total == 10
        assert len(batch.verified) + len(batch.flagged) + len(batch.exempt) + len(batch.pending) == 10

    def test_outputs_written(self, validator, outputs):
        validator.validate("PP 29 Oktober", limit=5, actor="test")
        assert outputs.verification_path.exists()
        assert outputs.violations_path.exists()
        assert outputs.blast_draft_path.exists()
        assert outputs.audit_path.exists()
        assert len(outputs.read_audit()) >= 1

    def test_second_run_skips_verified(self, validator):
        first = validator.validate("PP 29 Oktober", limit=12, actor="test")
        second = validator.validate("PP 29 Oktober", limit=12, actor="test")
        assert first.total == 12
        # Verified and exempt rows are skipped; flagged ones are re-checked.
        assert second.skipped_existing >= len(first.verified) + len(first.exempt) - 1

    def test_force_revalidates_everything(self, validator):
        validator.validate("PP 29 Oktober", limit=6, actor="test")
        forced = validator.validate("PP 29 Oktober", limit=6, force=True, actor="test")
        assert forced.skipped_existing == 0
        assert forced.total == 6

    def test_warns_when_blast_context_missing(self, validator):
        batch = validator.validate("PP Feed 26 Oktober", limit=3, actor="test")
        assert any("blast_context" in warning or "blast-tugas" in warning for warning in batch.warnings)

    def test_inconsistent_rulebase_does_not_mass_fine(self, validator):
        """PP-01 has deadline before jadwal — timing must be FLAG, not 20k."""
        batch = validator.validate("PP 29 Oktober", limit=20, actor="test", force=True)
        for result in batch.results:
            for check in result.checks:
                if check.check_id in ("C2", "C10"):
                    assert check.status != "FAIL", check.detail
                    assert check.denda == 0

    def test_angel_card_exemption_applied(self, validator):
        batch = validator.validate("PP Feed 26 Oktober", limit=30, actor="test", force=True)
        exempt = batch.exempt
        assert exempt, "expected at least one Angel Card holder in the sample"
        assert all(result.total_denda == 0 for result in exempt)
        assert all(result.angel_card_exempt for result in exempt)

    def test_buzzer_sheet_uses_buzzer_checks(self, validator):
        batch = validator.validate("Buzzer IG 09 Oktober", limit=8, actor="test")
        assert batch.task_type == "BUZZER"
        check_ids = {check.check_id for result in batch.results for check in result.checks}
        assert check_ids <= {"B1", "B2", "B7"}
        assert "B7" in check_ids

    def test_progress_callback_invoked(self, validator):
        events: list[tuple[str, int, int]] = []
        validator.validate(
            "PP 29 Oktober",
            limit=3,
            progress=lambda stage, current, total, message: events.append((stage, current, total)),
            actor="test",
        )
        assert events
        assert events[-1][1] == events[-1][2]  # ends at 3/3

    def test_reports_use_evidence_links(self, validator, outputs):
        batch = validator.validate("PP 29 Oktober", limit=5, actor="test")
        rows = outputs.rows_for_sheet("PP 29 Oktober")
        assert rows
        assert all(row["evidence_links"].startswith("https://") for row in rows if row["evidence_count"] != "0")


class TestReporting:
    def test_task_overview_covers_all_tasks(self, validator):
        overview = validator.task_overview()
        assert len(overview) == 12
        assert {item["task_id"] for item in overview} >= {"PP-01", "BZ-06"}

    def test_missing_submitters(self, validator):
        report = validator.missing_submitters("PP 29 Oktober")
        assert report["submitted"] == 42
        assert len(report["missing"]) == 106 - 42

    def test_resubmit_status(self, validator, outputs):
        outputs.write_violations([])
        report = validator.resubmit_status("Budi Santoso")
        assert report["nama"] == "Budi Santoso"
        assert report["total_denda"] >= 0


@pytest.fixture()
def isolated_validator(monkeypatch, outputs):
    monkeypatch.setattr("src.validator._load_rulebase", lambda: {})
    data = Mock(spec=DataLayer)
    data.source = SimpleNamespace(freshness_warnings=[])
    data.resolve_sheet.return_value = SheetResolution("Submission", [("Submission", 1.0)])
    data.resolve_task.return_value = TaskResolution("Submission", None)
    data.read_submissions.return_value = [
        Submission(2, "Alice", nim="1", divisi="Alpha", file_ids=["evidence"], sheet_name="Submission")
    ]
    data.load_roster.return_value = []
    data.load_angel_registry.return_value = []
    data.load_bukti_tambahan.return_value = []
    engine = Mock(spec=RulebaseEngine)
    engine.merge.return_value = RulebaseEngine({}).merge(None)
    engine.angel_exemption.return_value = (False, "")

    def evaluate(submission, task_id, facts, **kwargs):
        return ValidationResult(
            submission=submission,
            status=ResultStatus.FLAGGED,
            task_id=task_id,
            facts=facts,
            checks=[CheckResult("C4", "Caption", CheckStatus.FAIL, denda=5000)],
        )

    engine.evaluate.side_effect = evaluate
    vision = Mock()
    vision.analyze.return_value = VisionFacts(image_count=1, confidence=0.9)
    images = Mock(spec=ImageManager)
    images.fetch_many.return_value = []
    return Validator(data_layer=data, engine=engine, vision=vision, images=images, outputs=outputs, blast_context={})


def assert_cancelled(batch, outputs, completed=0):
    assert batch.cancelled is True
    assert batch.results == []
    assert batch.total == batch.total_denda == 0
    assert batch.finished_at is not None
    assert not outputs.verification_path.exists()
    assert not outputs.violations_path.exists()
    assert not outputs.blast_draft_path.exists()
    entries = outputs.read_audit()
    assert len(entries) == 1
    assert entries[0]["action"] == "validate_cancelled"
    assert entries[0]["result"] == "CANCELLED"
    assert entries[0]["rule_ids"] == []
    assert entries[0]["extra"]["completed_count"] == completed
    assert f"{completed} completed rows" in entries[0]["rationale"]


class TestDivisionFilter:
    def test_latest_resubmit_then_exact_filter_before_limit(self, isolated_validator):
        validator = isolated_validator
        validator.data.read_submissions.return_value = [
            Submission(2, "Bob", nim="2", divisi="Alpha Extended", sheet_name="Submission"),
            Submission(3, "Alice", nim="1", divisi=" Alpha ", sheet_name="Submission", timestamp=datetime(2026, 1, 1)),
            Submission(4, "Alice", nim="1", divisi="Beta", sheet_name="Submission", timestamp=datetime(2026, 1, 2)),
            Submission(5, "Charlie", nim="3", divisi="Alpha", sheet_name="Submission"),
        ]
        batch = validator.validate("Submission", divisi="  aLPHa  ", limit=1)
        assert not batch.cancelled
        assert batch.divisi == "Alpha"
        assert batch.total == 1
        # Alice moved to Beta in her latest resubmit; never revalidate old Alpha evidence.
        assert batch.results[0].submission.row_index == 5
        assert batch.results[0].submission.nama == "Charlie"
        assert len(batch.duplicates) == 1 and "latest row 4" in batch.duplicates[0]
        validator.vision.analyze.assert_called_once()

    def test_blank_division_uses_roster_without_mutating_source(self, isolated_validator):
        validator = isolated_validator
        row = Submission(2, "Alice", nim="1", divisi=" \t", sheet_name="Submission")
        validator.data.read_submissions.return_value = [row]
        validator.data.load_roster.return_value = [RosterEntry("Alice", nim="1", divisi="Alpha Team")]
        batch = validator.validate("Submission", divisi=" alpha   TEAM ")
        assert batch.divisi == "Alpha Team"
        assert batch.results[0].submission.divisi == "Alpha Team"
        assert row.divisi == " \t"

    def test_blank_division_falls_back_by_normalized_name(self, isolated_validator):
        validator = isolated_validator
        validator.data.read_submissions.return_value[0].nim = ""
        validator.data.read_submissions.return_value[0].divisi = ""
        validator.data.load_roster.return_value = [RosterEntry(" ALICE ", divisi="Alpha")]
        batch = validator.validate("Submission", divisi="alpha")
        assert batch.total == 1
        assert batch.results[0].submission.divisi == "Alpha"

    def test_ambiguous_roster_name_does_not_guess_division(self, isolated_validator):
        validator = isolated_validator
        row = validator.data.read_submissions.return_value[0]
        row.nim = row.divisi = ""
        validator.data.load_roster.return_value = [
            RosterEntry("Alice", nim="1", divisi="Alpha"),
            RosterEntry("Alice", nim="2", divisi="Beta"),
        ]
        assert validator.validate("Submission", divisi="Alpha").total == 0
        assert row.divisi == ""

    def test_unknown_division_lists_available_options(self, isolated_validator, outputs):
        validator = isolated_validator
        validator.data.load_roster.return_value = [RosterEntry("Bob", divisi="Beta")]
        with pytest.raises(DivisionNotFoundError, match="Unknown division.*Available divisions") as excinfo:
            validator.validate("Submission", divisi="Alph")
        assert excinfo.value.options == ["Alpha", "Beta"]
        assert "Alpha" in str(excinfo.value) and "Beta" in str(excinfo.value)
        validator.images.fetch_many.assert_not_called()
        validator.vision.analyze.assert_not_called()
        assert not outputs.audit_path.exists()
        assert not outputs.verification_path.exists()

    def test_filtered_batch_preserves_other_divisions(self, isolated_validator, outputs):
        validator = isolated_validator
        validator.data.read_submissions.return_value.append(
            Submission(3, "Bob", nim="2", divisi="Beta", sheet_name="Submission")
        )
        validator.validate("Submission")
        verification_before = next(row for row in outputs.rows_for_sheet("Submission") if row["nama"] == "Bob")
        violation_before = outputs.violation_history("Bob")
        validator.engine.angel_exemption.return_value = (True, "Exempt")
        batch = validator.validate("Submission", divisi="alpha", force=True)
        assert [result.submission.nama for result in batch.results] == ["Alice"]
        assert next(row for row in outputs.rows_for_sheet("Submission") if row["nama"] == "Bob") == verification_before
        assert outputs.violation_history("Bob") == violation_before
        assert outputs.denda_total("Alice") == 0
        assert outputs.denda_total("Bob") == 5000

    def test_resolved_sheet_bypasses_resolution(self, isolated_validator):
        batch = isolated_validator.validate("ambiguous query", resolved_sheet="Submission")
        assert batch.sheet_name == "Submission"
        isolated_validator.data.resolve_sheet.assert_not_called()
        isolated_validator.data.read_submissions.assert_called_once_with("Submission")

    def test_collects_source_freshness_warnings_after_reads(self, isolated_validator):
        def read_bukti():
            isolated_validator.data.source.freshness_warnings.append("Using quota fallback snapshot")
            return []

        isolated_validator.data.load_bukti_tambahan.side_effect = read_bukti
        batch = isolated_validator.validate("Submission")
        assert "Using quota fallback snapshot" in batch.warnings


class TestCancellation:
    def test_cancelled_before_start_does_not_read_sources(self, isolated_validator, outputs):
        event = Event()
        event.set()
        batch = isolated_validator.validate("unresolved", divisi="Alpha", cancel_event=event)
        assert_cancelled(batch, outputs)
        assert batch.divisi == "Alpha"
        assert isolated_validator.data.mock_calls == []
        isolated_validator.images.fetch_many.assert_not_called()
        isolated_validator.vision.analyze.assert_not_called()

    @pytest.mark.parametrize("source_method", [
        "resolve_sheet", "resolve_task", "read_submissions", "load_roster", "load_angel_registry", "load_bukti_tambahan"
    ])
    def test_cancellation_after_source_returns(self, isolated_validator, outputs, source_method):
        event = Event()
        method = getattr(isolated_validator.data, source_method)
        value = method.return_value

        def read():
            event.set()
            return value

        method.side_effect = lambda *args: read()
        batch = isolated_validator.validate("Submission", divisi="Alpha", cancel_event=event)
        assert_cancelled(batch, outputs)
        isolated_validator.images.fetch_many.assert_not_called()
        isolated_validator.vision.analyze.assert_not_called()

    @pytest.mark.parametrize("stage", ["download", "vision", "write"])
    def test_cancellation_from_progress_before_work(self, isolated_validator, outputs, stage):
        event = Event()

        def progress(current_stage, *args):
            if current_stage == stage:
                event.set()

        batch = isolated_validator.validate("Submission", progress=progress, cancel_event=event)
        assert_cancelled(batch, outputs, completed=1 if stage == "write" else 0)
        if stage == "download":
            isolated_validator.images.fetch_many.assert_not_called()
        if stage != "write":
            isolated_validator.vision.analyze.assert_not_called()

    def test_cancellation_after_download_returns(self, isolated_validator, outputs):
        event = Event()

        def download(file_ids):
            event.set()
            return []

        isolated_validator.images.fetch_many.side_effect = download
        batch = isolated_validator.validate("Submission", cancel_event=event)
        assert_cancelled(batch, outputs)
        isolated_validator.vision.analyze.assert_not_called()

    def test_cancellation_after_analyze_returns_discards_partial_batch(self, isolated_validator, outputs):
        event = Event()
        validator = isolated_validator
        validator.data.read_submissions.return_value.append(
            Submission(3, "Bob", nim="2", divisi="Alpha", sheet_name="Submission")
        )

        def analyze(images, hints):
            if hints.nama == "Bob":
                event.set()
            return VisionFacts(image_count=1, confidence=0.9)

        validator.vision.analyze.side_effect = analyze
        batch = validator.validate("Submission", cancel_event=event)
        assert_cancelled(batch, outputs, completed=1)
        assert validator.vision.analyze.call_count == 2
        validator.engine.evaluate.assert_called_once()

    def test_cancellation_between_rows(self, isolated_validator, outputs):
        event = Event()
        validator = isolated_validator
        validator.data.read_submissions.return_value.append(
            Submission(3, "Bob", nim="2", divisi="Alpha", sheet_name="Submission")
        )
        evaluate = validator.engine.evaluate.side_effect

        def cancelling_evaluate(*args, **kwargs):
            result = evaluate(*args, **kwargs)
            event.set()
            return result

        validator.engine.evaluate.side_effect = cancelling_evaluate
        batch = validator.validate("Submission", cancel_event=event)
        assert_cancelled(batch, outputs, completed=1)
        validator.vision.analyze.assert_called_once()

    def test_cancellation_preserves_existing_files(self, isolated_validator, outputs):
        isolated_validator.validate("Submission")
        paths = [outputs.verification_path, outputs.violations_path, *outputs.output_dir.glob("*.md")]
        before = {path: path.read_bytes() for path in paths}
        audit_count = len(outputs.read_audit())
        event = Event()

        def progress(stage, *args):
            if stage == "write":
                event.set()

        batch = isolated_validator.validate("Submission", force=True, cancel_event=event, progress=progress)
        assert batch.cancelled and batch.results == []
        assert {path: path.read_bytes() for path in paths} == before
        audit = outputs.read_audit()
        assert len(audit) == audit_count + 1
        assert audit[-1]["action"] == "validate_cancelled"

    def test_cancellation_while_waiting_for_output_lock(self, isolated_validator, outputs):
        event = Event()
        ready = Event()

        def progress(stage, *args):
            if stage == "write":
                ready.set()

        with ThreadPoolExecutor(max_workers=1) as executor:
            with OUTPUT_LOCK:
                future = executor.submit(
                    isolated_validator.validate, "Submission", skip_verified=False,
                    cancel_event=event, progress=progress
                )
                assert ready.wait(5), "Validation did not reach persistence"
                event.set()
            batch = future.result(timeout=5)
        assert_cancelled(batch, outputs, completed=1)
