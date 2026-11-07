"""Adapter availability: a misconfigured backend must fail loudly, never fake data."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from src import config
from src.drive_client import DriveError, UnavailableDrive, build_drive_backend
from src.models import DownloadedImage, ResultStatus, Submission, ValidationResult, VisionFacts
from src.validator import _apply_anomalies
from src.vision_checker import (
    MockVision,
    NineRouterVision,
    UnavailableVision,
    VisionHints,
    build_vision_backend,
)


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


class TestUnifiedAIProviderConfig:
    def test_single_9router_provider_is_the_constant(self):
        assert config.AI_PROVIDER == "9router"
        assert config.AI_PROVIDERS == {"9router", "mock"}

    @pytest.mark.parametrize("removed", ["openrouter", "opencode", "opencode-api", "tencent"])
    def test_removed_providers_are_not_selectable(self, removed):
        """Old provider names must be rejected loudly, never silently selected."""
        assert removed in config.REMOVED_AI_PROVIDERS
        assert removed not in config.AI_PROVIDERS

    def test_active_ai_model_is_always_the_9router_model(self, monkeypatch):
        monkeypatch.setattr(config, "NINEROUTER_MODEL", "mamazon")
        assert config.active_ai_model() == "mamazon"
        assert config.active_chat_model() == "mamazon"

        monkeypatch.setattr(config, "NINEROUTER_MODEL", "")
        assert config.active_ai_model() == ""

    def test_ninerouter_requires_key_model_and_base_url(self, monkeypatch):
        monkeypatch.setattr(config, "USE_MOCK_VISION", False)
        monkeypatch.setattr(config, "NINEROUTER_API_KEY", "")
        monkeypatch.setattr(config, "NINEROUTER_MODEL", "")
        monkeypatch.setattr(config, "NINEROUTER_BASE_URL", "not-a-url")
        issues = config.validate_ai_config()
        assert any("NINEROUTER_API_KEY" in issue for issue in issues)
        assert any("NINEROUTER_MODEL" in issue for issue in issues)
        assert any("NINEROUTER_BASE_URL" in issue for issue in issues)

    def test_ninerouter_valid_config_has_no_issues(self, monkeypatch):
        monkeypatch.setattr(config, "USE_MOCK_VISION", False)
        monkeypatch.setattr(config, "NINEROUTER_API_KEY", "test-key")
        monkeypatch.setattr(config, "NINEROUTER_MODEL", "mamazon")
        monkeypatch.setattr(config, "NINEROUTER_BASE_URL", "http://localhost:20128/v1")
        assert config.validate_ai_config() == []
        status = config.ai_provider_status()
        assert status["provider"] == "9router"
        assert status["model"] == "mamazon"
        assert status["api_base_url"] == "http://localhost:20128/v1"
        assert status["api_key_set"] is True

    def test_offline_mode_has_no_issues(self, monkeypatch):
        """AI_OFFLINE is opt-in: no gateway required and nothing is faked silently."""
        monkeypatch.setattr(config, "USE_MOCK_VISION", True)
        monkeypatch.setattr(config, "NINEROUTER_API_KEY", "")
        monkeypatch.setattr(config, "NINEROUTER_MODEL", "")
        assert config.validate_ai_config() == []


class TestVisionFactory:
    def test_factory_selects_9router_and_aliases(self):
        for alias in ("9router", "ninerouter", "9r", None):
            assert isinstance(build_vision_backend(alias), NineRouterVision)

    def test_factory_selects_mock_for_offline(self):
        for alias in ("mock", "offline"):
            backend = build_vision_backend(alias)
            assert isinstance(backend, MockVision)
            assert backend.name == "mock"

    def test_factory_rejects_removed_providers_with_9router_hint(self):
        for removed in ("opencode", "opencode-api", "tencent", "agy"):
            backend = build_vision_backend(removed)
            assert isinstance(backend, UnavailableVision)
            assert "9Router" in backend.reason


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


class TestUnifiedAIProviderHelpers:
    """Effective base URL, warnings and safe status flags for the 9Router gateway."""

    def test_effective_base_url_strips_chat_completions_but_keeps_normal_base(self, monkeypatch):
        monkeypatch.setattr(config, "NINEROUTER_BASE_URL", "http://localhost:20128/v1/chat/completions")
        assert config.effective_ninerouter_base_url() == "http://localhost:20128/v1"

        monkeypatch.setattr(config, "NINEROUTER_BASE_URL", "http://localhost:20128/v1/")
        assert config.effective_ninerouter_base_url() == "http://localhost:20128/v1"

        monkeypatch.setattr(config, "NINEROUTER_BASE_URL", "http://localhost:20128/v1")
        assert config.effective_ninerouter_base_url() == "http://localhost:20128/v1"

        # A bare base is never appended twice.
        assert not config.effective_ninerouter_base_url().endswith("/chat/completions")

    def test_ai_provider_warnings_key_base_and_model(self, monkeypatch):
        monkeypatch.setattr(config, "NINEROUTER_API_KEY", "")
        monkeypatch.setattr(config, "NINEROUTER_BASE_URL", "not-a-url")
        monkeypatch.setattr(config, "NINEROUTER_MODEL", "")

        warnings = config.ai_provider_warnings()
        assert any("NINEROUTER_API_KEY" in warning for warning in warnings)
        assert any("NINEROUTER_BASE_URL" in warning for warning in warnings)
        assert any("NINEROUTER_MODEL" in warning for warning in warnings)

        # A fully configured gateway emits no warnings.
        monkeypatch.setattr(config, "NINEROUTER_API_KEY", "test-key")
        monkeypatch.setattr(config, "NINEROUTER_BASE_URL", "http://localhost:20128/v1")
        monkeypatch.setattr(config, "NINEROUTER_MODEL", "mamazon")
        assert config.ai_provider_warnings() == []

    def test_ai_provider_status_includes_warnings_tools_memory_and_web_flags(self, monkeypatch):
        monkeypatch.setattr(config, "NINEROUTER_API_KEY", "test-key")
        monkeypatch.setattr(config, "NINEROUTER_MODEL", "mamazon")
        monkeypatch.setattr(config, "NINEROUTER_BASE_URL", "http://localhost:20128/v1/chat/completions")
        monkeypatch.setattr(config, "CHAT_TOOLS_ENABLED", True)
        monkeypatch.setattr(config, "CHAT_MEMORY_ENABLED", True)
        monkeypatch.setattr(config, "WEB_SEARCH_URL", "https://search.example/api")
        monkeypatch.setattr(config, "NINEROUTER_USER_AGENT", "MamazonTest/1.0")

        status = config.ai_provider_status()
        assert status["provider"] == "9router"
        assert status["configured_model"] == "mamazon"
        assert status["model"] == "mamazon"
        assert status["api_base_url"] == "http://localhost:20128/v1"
        assert status["api_user_agent"] == "MamazonTest/1.0"
        assert status["tools_enabled"] is True
        assert status["memory_enabled"] is True
        assert status["web_search_configured"] is True
        assert status["warnings"] == config.ai_provider_warnings()
        # Status is a safe, secret-free summary.
        assert "test-key" not in str(status)

        monkeypatch.setattr(config, "CHAT_TOOLS_ENABLED", False)
        monkeypatch.setattr(config, "CHAT_MEMORY_ENABLED", False)
        monkeypatch.setattr(config, "WEB_SEARCH_URL", "")
        off = config.ai_provider_status()
        assert off["tools_enabled"] is False
        assert off["memory_enabled"] is False
        assert off["web_search_configured"] is False
