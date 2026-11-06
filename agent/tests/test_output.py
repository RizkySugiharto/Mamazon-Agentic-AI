"""Output artefacts: CSV upserts, violations log, blast draft, state machine."""

from __future__ import annotations

import csv
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from threading import Event

import pytest

from src.models import (
    BlastEntry,
    BlastStatus,
    CheckResult,
    CheckStatus,
    ResultStatus,
    Submission,
    ValidationBatch,
    ValidationResult,
    VisionFacts,
)
from src.output_generator import OUTPUT_LOCK, BlastStore, OutputGenerator, read_csv


def make_result(
    nama: str = "Budi Santoso",
    status: str = ResultStatus.FLAGGED,
    denda: int = 5000,
    check_id: str = "C4",
    sheet: str = "PP Feed 26 Oktober",
) -> ValidationResult:
    submission = Submission(
        row_index=2,
        nama=nama,
        nim="000001234567",
        divisi="Conan (Acara)",
        ig_username="budi_santoso",
        links=["https://drive.google.com/open?id=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"],
        timestamp=datetime(2026, 10, 28, 10, 0),
        sheet_name=sheet,
        task_id="PP-03",
    )
    result = ValidationResult(
        submission=submission,
        status=status,
        task_id="PP-03",
        validated_at=datetime(2026, 11, 5, 8, 0),
        blast_date=datetime(2026, 10, 28),
        facts=VisionFacts(image_count=2, confidence=0.9),
    )
    if denda:
        result.checks.append(
            CheckResult(
                check_id=check_id,
                name="Caption sesuai ketentuan",
                status=CheckStatus.FAIL,
                denda=denda,
                detail="Caption tidak sesuai referensi",
            )
        )
    else:
        result.checks.append(
            CheckResult(check_id=check_id, name="Caption sesuai ketentuan", status=CheckStatus.PASS)
        )
    return result


def make_batch(results) -> ValidationBatch:
    return ValidationBatch(
        sheet_name="PP Feed 26 Oktober",
        task_id="PP-03",
        task_type="PP",
        results=list(results),
        started_at=datetime(2026, 11, 5, 8, 0),
        finished_at=datetime(2026, 11, 5, 8, 1),
    )


class TestVerificationReport:
    def test_written_once(self, outputs: OutputGenerator):
        outputs.write_verification([make_result()])
        rows = read_csv(outputs.verification_path)
        assert len(rows) == 1
        assert rows[0]["status"] == ResultStatus.FLAGGED
        assert rows[0]["violations"] == "C4"

    def test_revalidation_replaces_not_appends(self, outputs: OutputGenerator):
        outputs.write_verification([make_result(status=ResultStatus.FLAGGED)])
        outputs.write_verification([make_result(status=ResultStatus.VERIFIED, denda=0)])
        rows = read_csv(outputs.verification_path)
        assert len(rows) == 1
        assert rows[0]["status"] == ResultStatus.VERIFIED
        assert rows[0]["total_denda"] == "0"

    def test_other_sheets_preserved(self, outputs: OutputGenerator):
        outputs.write_verification([make_result(sheet="PP 29 Oktober")])
        outputs.write_verification([make_result(sheet="PP Feed 26 Oktober")])
        assert len(read_csv(outputs.verification_path)) == 2

    def test_existing_statuses(self, outputs: OutputGenerator):
        outputs.write_verification([make_result(status=ResultStatus.VERIFIED, denda=0)])
        statuses = outputs.existing_statuses("PP Feed 26 Oktober")
        assert statuses[("pp feed 26 oktober", "budi santoso", "000001234567")] == "VERIFIED"

    def test_csv_bytes(self, outputs: OutputGenerator):
        payload = outputs.results_csv_bytes([make_result()])
        assert payload.startswith(b"\xef\xbb\xbf")  # utf-8-sig for Excel
        assert b"Budi Santoso" in payload


class TestViolationsLog:
    def test_written(self, outputs: OutputGenerator):
        outputs.write_violations([make_result()])
        rows = read_csv(outputs.violations_path)
        assert len(rows) == 1
        assert rows[0]["rule_id"] == "C4"
        assert rows[0]["status"] == ResultStatus.FLAGGED

    def test_not_duplicated_on_revalidation(self, outputs: OutputGenerator):
        outputs.write_violations([make_result()])
        outputs.write_violations([make_result()])
        assert len(read_csv(outputs.violations_path)) == 1

    def test_marked_resolved_when_passing(self, outputs: OutputGenerator):
        outputs.write_violations([make_result(status=ResultStatus.FLAGGED)])
        outputs.write_violations([make_result(status=ResultStatus.VERIFIED, denda=0)])
        rows = read_csv(outputs.violations_path)
        assert len(rows) == 1
        assert rows[0]["status"] == "RESOLVED"

    def test_recurring_count_and_total(self, outputs: OutputGenerator):
        outputs.write_violations(
            [make_result(), make_result(check_id="C6", denda=5000)]
        )
        assert outputs.recurring_count("Budi Santoso") == 2
        assert outputs.denda_total("Budi Santoso") == 10000

    def test_blast_marks_status(self, outputs: OutputGenerator):
        outputs.write_violations([make_result()])
        outputs.mark_blasted("Budi Santoso", "PP-03")
        rows = read_csv(outputs.violations_path)
        assert rows[0]["status"] == "BLASTED"

    def test_resolved_excluded_by_default(self, outputs: OutputGenerator):
        outputs.write_violations([make_result()])
        outputs.write_violations([make_result(status=ResultStatus.VERIFIED, denda=0)])
        assert outputs.denda_rows("Budi Santoso") == []
        assert len(outputs.denda_rows("Budi Santoso", include_resolved=True)) == 1

    @pytest.mark.parametrize("status", [ResultStatus.PENDING_MANUAL_REVIEW, ResultStatus.ERROR, ResultStatus.EXEMPT])
    def test_undecided_or_exempt_results_never_insert_fines(self, outputs, status):
        result = make_result(status=status)
        assert result.total_denda == 5000  # Failed checks may survive anomaly handling.
        assert outputs.write_violations([result]) == 0
        assert read_csv(outputs.violations_path) == []
        assert outputs.denda_total(result.submission.nama) == 0

    @pytest.mark.parametrize("status", [ResultStatus.PENDING_MANUAL_REVIEW, ResultStatus.ERROR, ResultStatus.EXEMPT])
    def test_stale_unblasted_charges_withdrawn_even_without_checks(self, outputs, status):
        outputs.write_violations([make_result(), make_result(check_id="C6"), make_result(nama="Sari Dewi")])
        result = make_result(status=status)
        result.checks.clear()
        outputs.write_violations([result])
        assert {row["status"] for row in outputs.violation_history("Budi Santoso")} == {"WITHDRAWN"}
        assert outputs.denda_total("Budi Santoso") == 0
        assert outputs.recurring_count("Budi Santoso") == 0
        assert outputs.denda_total("Sari Dewi") == 5000

    @pytest.mark.parametrize("status", [ResultStatus.PENDING_MANUAL_REVIEW, ResultStatus.ERROR, ResultStatus.EXEMPT])
    def test_blasted_charges_never_withdrawn_automatically(self, outputs, status):
        outputs.write_violations([make_result()])
        outputs.mark_blasted("Budi Santoso", "PP-03")
        before = outputs.violation_history("Budi Santoso")
        outputs.write_violations([make_result(status=status)])
        assert outputs.violation_history("Budi Santoso") == before
        assert outputs.denda_total("Budi Santoso") == 5000

    def test_withdrawal_is_scoped_to_person_task_and_sheet(self, outputs):
        other_task = make_result()
        other_task.task_id = "PP-04"
        outputs.write_violations([make_result(), other_task, make_result(sheet="Other sheet")])
        outputs.write_violations([make_result(status=ResultStatus.PENDING_MANUAL_REVIEW)])
        rows = outputs.violation_history("Budi Santoso")
        assert sum(row["status"] == "WITHDRAWN" for row in rows) == 1
        assert outputs.denda_total("Budi Santoso") == 10000

    def test_withdrawn_charge_can_be_reflagged_by_confirmed_result(self, outputs):
        outputs.write_violations([make_result()])
        outputs.write_violations([make_result(status=ResultStatus.PENDING_MANUAL_REVIEW)])
        outputs.write_violations([make_result()])
        assert len(outputs.violation_history("Budi Santoso")) == 1
        assert outputs.denda_total("Budi Santoso") == 5000

    def test_blast_does_not_reactivate_withdrawn_charges(self, outputs):
        outputs.write_violations([make_result()])
        outputs.write_violations([make_result(status=ResultStatus.EXEMPT)])
        outputs.mark_blasted("Budi Santoso", "PP-03")
        assert outputs.violation_history("Budi Santoso")[0]["status"] == "WITHDRAWN"
        assert outputs.denda_total("Budi Santoso") == 0


class TestBlastDraft:
    def test_contains_entry_and_totals(self, outputs: OutputGenerator):
        batch = make_batch([make_result(), make_result(nama="Sari Dewi")])
        path, entries = outputs.write_blast_draft(batch)
        text = path.read_text(encoding="utf-8")
        assert "BLAST DENDA" in text
        assert "Budi Santoso" in text
        assert "Rp 5.000" in text
        assert len(entries) == 2

    def test_verified_rows_not_in_draft(self, outputs: OutputGenerator):
        batch = make_batch(
            [make_result(status=ResultStatus.VERIFIED, denda=0), make_result()]
        )
        _, entries = outputs.write_blast_draft(batch)
        assert len(entries) == 1

    def test_batch_total_excludes_pending_review(self):
        flagged = make_result()
        pending = make_result(nama="Nabila", status=ResultStatus.PENDING_MANUAL_REVIEW)
        batch = make_batch([flagged, pending])
        assert batch.total_denda == 5000
        assert batch.total_denda_including_pending == 10000


class TestAuditLog:
    def test_append_and_read(self, outputs: OutputGenerator):
        outputs.audit(action="validate", actor="tester", target="PP-03", result="ok")
        outputs.audit(action="blast", actor="koor", target="PP-03", result="held")
        entries = outputs.read_audit(limit=10)
        assert len(entries) == 2
        assert entries[-1]["action"] == "blast"

    def test_filter_by_actor(self, outputs: OutputGenerator):
        outputs.audit(action="validate", actor="tester")
        outputs.audit(action="blast", actor="koor")
        assert len(outputs.read_audit(user="koor")) == 1


class TestBatchPersistence:
    def test_pending_fines_excluded_from_log_and_draft(self, outputs):
        batch = make_batch([
            make_result(), make_result(nama="Pending", status=ResultStatus.PENDING_MANUAL_REVIEW),
            make_result(nama="Error", status=ResultStatus.ERROR),
        ])
        report = outputs.write_batch(batch)
        assert report["violation_rows"] == 1
        assert [entry.nama for entry in report["blast_entries"]] == ["Budi Santoso"]
        assert [row["nama_komitee"] for row in read_csv(outputs.violations_path)] == ["Budi Santoso"]
        assert outputs.denda_total("Pending") == outputs.denda_total("Error") == 0

    def test_cancelled_batch_cannot_be_persisted(self, outputs):
        batch = make_batch([make_result()])
        batch.cancelled = True
        report = outputs.write_batch(batch)
        assert report["verification_rows"] == report["violation_rows"] == 0
        assert report["blast_entries"] == []
        assert list(outputs.output_dir.iterdir()) == []

    def test_division_metadata_is_audited(self, outputs):
        batch = make_batch([make_result()])
        batch.divisi = "Conan (Acara)"
        outputs.write_batch(batch)
        assert outputs.read_audit()[0]["extra"]["divisi"] == batch.divisi


class TestFineLifecycleSafety:
    def test_not_applicable_withdraws_previous_active_check(self, outputs):
        outputs.write_violations([make_result(check_id="C3")])
        result = make_result(status=ResultStatus.VERIFIED, denda=0, check_id="C3")
        result.checks[0].status = CheckStatus.NOT_APPLICABLE
        outputs.write_violations([result])
        assert outputs.denda_total(result.submission.nama) == 0

    def test_mark_blasted_is_scoped_to_sheet_and_rules(self, outputs):
        outputs.write_violations([make_result(sheet="Sheet A"), make_result(sheet="Sheet B"), make_result(sheet="Sheet A", check_id="C6")])
        outputs.mark_blasted("Budi Santoso", "PP-03", "Sheet A", ["C4"])
        rows = outputs.denda_rows()
        assert sum(row["status"] == "BLASTED" for row in rows) == 1
        blasted = next(row for row in rows if row["status"] == "BLASTED")
        assert blasted["sheet_name"] == "Sheet A" and blasted["rule_id"] == "C4"


class TestSharedOutputLock:
    @pytest.mark.parametrize("method", ["write_verification", "write_violations", "mark_blasted", "audit", "write_batch"])
    def test_direct_writers_use_shared_module_lock(self, outputs, method):
        second = OutputGenerator(
            output_dir=outputs.output_dir,
            verification_path=outputs.verification_path,
            violations_path=outputs.violations_path,
            blast_draft_path=outputs.blast_draft_path,
            audit_path=outputs.audit_path,
        )
        ready = Event()
        args = {
            "write_verification": ([make_result()],),
            "write_violations": ([make_result()],),
            "mark_blasted": ("Budi Santoso", "PP-03"),
            "audit": ("test",),
            "write_batch": (make_batch([make_result()]),),
        }[method]

        def mutate():
            ready.set()
            return getattr(second, method)(*args)

        with ThreadPoolExecutor(max_workers=1) as executor:
            with OUTPUT_LOCK:
                future = executor.submit(mutate)
                assert ready.wait(5)
                assert not future.done()
                assert list(outputs.output_dir.iterdir()) == []
            future.result(timeout=5)

    def test_entire_batch_blocks_other_instance_mutations(self, outputs, monkeypatch):
        second = OutputGenerator(
            output_dir=outputs.output_dir,
            verification_path=outputs.verification_path,
            violations_path=outputs.violations_path,
            blast_draft_path=outputs.blast_draft_path,
            audit_path=outputs.audit_path,
        )
        in_batch = Event()
        release = Event()
        mutation_started = Event()
        write_draft = outputs.write_blast_draft

        def paused_draft(batch):
            in_batch.set()
            assert release.wait(5)
            return write_draft(batch)

        monkeypatch.setattr(outputs, "write_blast_draft", paused_draft)

        def audit():
            mutation_started.set()
            second.audit(action="other_task")

        with ThreadPoolExecutor(max_workers=2) as executor:
            batch_future = executor.submit(outputs.write_batch, make_batch([make_result()]))
            try:
                assert in_batch.wait(5)
                audit_future = executor.submit(audit)
                assert mutation_started.wait(5)
                assert not audit_future.done()
            finally:
                release.set()
            batch_future.result(timeout=5)
            audit_future.result(timeout=5)
        assert [entry["action"] for entry in outputs.read_audit()] == ["validate", "other_task"]

    def test_concurrent_batches_preserve_all_csv_rows_and_audits(self, outputs):
        generators = [
            OutputGenerator(
                output_dir=outputs.output_dir,
                verification_path=outputs.verification_path,
                violations_path=outputs.violations_path,
                blast_draft_path=outputs.blast_draft_path,
                audit_path=outputs.audit_path,
            ) for _ in range(12)
        ]
        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [
                executor.submit(generator.write_batch, make_batch([make_result(nama=f"Person {index}")]))
                for index, generator in enumerate(generators)
            ]
            for future in futures:
                future.result(timeout=10)
        assert len(read_csv(outputs.verification_path)) == 12
        assert len(read_csv(outputs.violations_path)) == 12
        assert len(outputs.read_audit(limit=20)) == 12
        assert outputs.denda_total("Person 0") == 5000


class TestBlastStore:
    def test_create_and_reload(self, blast_store: BlastStore):
        record = blast_store.create(
            sheet_name="PP Feed 26 Oktober",
            entries=[BlastEntry(nama="Budi", divisi="Conan (Acara)", ig_username="budi", total_denda=5000)],
            task_id="PP-03",
            created_by="koor",
        )
        assert record.status == BlastStatus.DRAFT
        reloaded = BlastStore(directory=blast_store.directory).get(record.blast_id)
        assert reloaded is not None
        assert reloaded.count == 1
        assert reloaded.total_denda == 5000

    def test_state_machine(self, blast_store: BlastStore):
        record = blast_store.create("PP 29 Oktober", [], created_by="koor")
        assert blast_store.hold(record.blast_id, "koor", "tunggu BPH").status == BlastStatus.HELD
        assert blast_store.release(record.blast_id, "koor").status == BlastStatus.DRAFT
        assert blast_store.approve(record.blast_id, "koor").status == BlastStatus.APPROVED
        assert blast_store.mark_sent(record.blast_id).status == BlastStatus.SENT

    def test_reject(self, blast_store: BlastStore):
        record = blast_store.create("PP 29 Oktober", [], created_by="koor")
        assert blast_store.reject(record.blast_id, "koor").status == BlastStatus.REJECTED

    def test_missing_id(self, blast_store: BlastStore):
        assert blast_store.get("blast_nope") is None
        assert blast_store.approve("blast_nope", "koor") is None

    def test_latest_for_sheet(self, blast_store: BlastStore):
        first = blast_store.create("PP 29 Oktober", [], created_by="a")
        second = blast_store.create("PP 29 Oktober", [], created_by="b")
        latest = blast_store.latest_for_sheet("PP 29 Oktober")
        assert latest.blast_id in {first.blast_id, second.blast_id}
        assert blast_store.summary().get(BlastStatus.DRAFT) == 2
