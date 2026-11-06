"""Adapter availability: a misconfigured backend must fail loudly, never fake data."""

from __future__ import annotations

import pytest

from src import config
from src.drive_client import DriveError, UnavailableDrive, build_drive_backend
from src.models import DownloadedImage, ResultStatus, Submission, ValidationResult, VisionFacts
from src.validator import _apply_anomalies
from src.vision_checker import UnavailableVision, build_vision_backend


class TestVisionAvailability:
    def test_unavailable_flags_every_call(self):
        backend = UnavailableVision("SDK missing")
        facts = backend.analyze(
            [DownloadedImage(file_id="a", path="x.png")], hints=None  # type: ignore[arg-type]
        )
        assert backend.name == "unavailable"
        assert facts.has_flag("vision_error")
        assert facts.raw["error"] == "SDK missing"
        assert facts.confidence == 0

    @pytest.mark.skipif(
        config.module_available("tencentcloud"), reason="SDK installed, client may init"
    )
    def test_tencent_without_sdk_is_unavailable_not_mock(self, monkeypatch):
        """Regression: it used to silently return MockVision (fake evidence)."""
        monkeypatch.setattr(config, "TENCENT_SECRET_ID", "id")
        monkeypatch.setattr(config, "TENCENT_SECRET_KEY", "key")
        backend = build_vision_backend("tencent")
        assert isinstance(backend, UnavailableVision)
        assert "tencentcloud-sdk-python" in backend.reason

    def test_explicit_mock_still_works(self):
        assert build_vision_backend("mock").name == "mock"


class TestDriveAvailability:
    def test_unavailable_drive_raises_with_reason(self):
        backend = UnavailableDrive("libs missing")
        with pytest.raises(DriveError) as excinfo:
            backend.fetch("abc")
        assert "libs missing" in str(excinfo.value)

    @pytest.mark.skipif(
        config.module_available("googleapiclient"), reason="libs installed"
    )
    def test_google_without_libs_is_unavailable_not_mock(self, monkeypatch):
        monkeypatch.setattr(config, "SERVICE_ACCOUNT_PATH", config.CONFIG_DIR / "nope.json")
        backend = build_drive_backend("google")
        assert isinstance(backend, UnavailableDrive)
        assert "google-api-python-client" in backend.reason


class TestConfigChecks:
    def test_flags_missing_tencent_sdk(self, monkeypatch):
        monkeypatch.setattr(config, "VISION_PROVIDER", "tencent")
        monkeypatch.setattr(config, "TENCENT_SECRET_ID", "id")
        monkeypatch.setattr(config, "TENCENT_SECRET_KEY", "key")
        monkeypatch.setattr(config, "module_available", lambda name: name != "tencentcloud")
        issues = config.validate_config()
        assert any("SDK is not installed" in issue for issue in issues)

    def test_gsheets_requires_spreadsheet_id(self, monkeypatch):
        monkeypatch.setattr(config, "SHEET_SOURCE", "gsheets")
        monkeypatch.setattr(config, "GOOGLE_SPREADSHEET_ID", "")
        issues = config.validate_config()
        assert any("GOOGLE_SPREADSHEET_ID" in issue for issue in issues)

    def test_unknown_sheet_source_is_flagged(self, monkeypatch):
        monkeypatch.setattr(config, "SHEET_SOURCE", "csv")
        issues = config.validate_config()
        assert any("tidak dikenal" in issue for issue in issues)

    def test_flags_missing_drive_libs(self, monkeypatch):
        monkeypatch.setattr(config, "DRIVE_PROVIDER", "google")
        monkeypatch.setattr(config, "module_available", lambda name: name != "googleapiclient")
        issues = config.validate_config()
        assert any("google-api-python-client" in issue for issue in issues)


class TestVisionErrorStatus:
    @staticmethod
    def _result() -> ValidationResult:
        submission = Submission(
            row_index=2,
            nama="Budi",
            links=["https://drive.google.com/open?id=A"],
            sheet_name="PP 29 Oktober",
        )
        return ValidationResult(submission=submission, task_id="PP-01")

    def test_vision_error_forces_manual_review(self):
        facts = VisionFacts(image_count=2, confidence=0.0)
        facts.flag("vision_error")
        facts.raw["error"] = "502 upstream"
        result = self._result()
        _apply_anomalies(result, facts)
        assert result.status == ResultStatus.PENDING_MANUAL_REVIEW
        assert "502 upstream" in " ".join(result.notes)

    def test_clean_facts_do_not_change_status(self):
        result = self._result()
        _apply_anomalies(result, VisionFacts(image_count=2, confidence=0.9))
        assert result.status == ResultStatus.VERIFIED
