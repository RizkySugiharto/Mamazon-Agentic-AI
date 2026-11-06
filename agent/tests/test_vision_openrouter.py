"""OpenRouter Vision adapter tests — no router required (injected fake session)."""

from __future__ import annotations

import json

import pytest

from src import config
from src.models import DownloadedImage
from src.vision_checker import (
    OpenRouterVision,
    VisionHints,
    build_vision_backend,
    facts_from_payload,
)

PAYLOAD = {
    "caption_text": "Pusing dengan project akhir/TA codingmu?",
    "hashtags": ["#PPHorizon", "#CaseOfTheUnseen"],
    "mentions": ["@horizon_umn"],
    "followers": 812,
    "is_private": False,
    "posted_at": "2026-10-28 10:00",
    "story_live": None,
    "likes": 1,
    "like_platforms": ["instagram"],
    "rating": None,
    "vote_target": "",
    "ig_username_seen": "budi_santoso",
    "aspect_ratio": "4:5",
    "carousel_slides": ["1", "2"],
    "anomaly_flags": [],
    "confidence": 0.88,
}


class FakeResponse:
    def __init__(self, status_code: int, payload: dict | str) -> None:
        self.status_code = status_code
        self._payload = payload

    @property
    def text(self) -> str:
        return self._payload if isinstance(self._payload, str) else json.dumps(self._payload)

    def json(self) -> dict:
        if isinstance(self._payload, str):
            return json.loads(self._payload)
        return self._payload


class FakeSession:
    def __init__(self, responses=None, error: Exception | None = None) -> None:
        self.responses = list(responses or [])
        self.error = error
        self.calls: list[dict] = []

    def post(self, url, headers=None, json=None, timeout=None):
        self.calls.append({"method": "POST", "url": url, "headers": headers, "json": json})
        if self.error:
            raise self.error
        return self.responses.pop(0)

    def get(self, url, headers=None, timeout=None):
        self.calls.append({"method": "GET", "url": url, "headers": headers})
        if self.error:
            raise self.error
        return self.responses.pop(0)


def completion(payload: dict | None = None, content: str | None = None) -> FakeResponse:
    body = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": content if content is not None else json.dumps(payload or PAYLOAD),
                }
            }
        ],
        "usage": {"prompt_tokens": 1200, "completion_tokens": 90},
    }
    return FakeResponse(200, body)


def make_image(tmp_path) -> DownloadedImage:
    from PIL import Image

    path = tmp_path / "evidence.png"
    Image.new("RGB", (32, 32), "white").save(path)
    return DownloadedImage(file_id="abc", path=str(path), sha256="x", size_bytes=path.stat().st_size)


def make_hints() -> VisionHints:
    return VisionHints(
        task_id="PP-03",
        nama="Budi Santoso",
        ig_username="budi_santoso",
        caption_reference="Pusing dengan project akhir/TA codingmu?",
        required_hashtags=["#PPHorizon"],
        required_mentions=["@horizon_umn"],
        feed_required=True,
    )


class TestAnalyze:
    def test_parses_facts(self, tmp_path):
        backend = OpenRouterVision(model="google/gemini-2.0-flash-001", session=FakeSession([completion()]))
        facts = backend.analyze([make_image(tmp_path)], make_hints())

        assert facts.provider == "openrouter"
        assert facts.caption_text.startswith("Pusing")
        assert facts.followers == 812
        assert facts.aspect_ratio == "4:5"
        assert facts.confidence == 0.88
        assert facts.image_count == 1
        assert facts.raw["usage"]["prompt_tokens"] == 1200

    def test_request_shape_is_openai_compatible(self, tmp_path):
        session = FakeSession([completion()])
        backend = OpenRouterVision(model="google/gemini-2.0-flash-001", session=session)
        backend.analyze([make_image(tmp_path)], make_hints())

        call = session.calls[0]
        assert call["url"].endswith("/v1/chat/completions")
        assert call["json"]["model"] == "google/gemini-2.0-flash-001"
        assert call["json"]["stream"] is False
        content = call["json"]["messages"][0]["content"]
        assert content[0]["type"] == "text"
        assert content[1]["type"] == "image_url"
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")

    def test_bearer_header_when_key_set(self, tmp_path):
        session = FakeSession([completion()])
        backend = OpenRouterVision(model="m", api_key="secret-key", session=session)
        backend.analyze([make_image(tmp_path)], make_hints())
        assert session.calls[0]["headers"]["Authorization"] == "Bearer secret-key"

    def test_no_auth_header_without_key(self, tmp_path):
        session = FakeSession([completion()])
        backend = OpenRouterVision(model="m", api_key="", session=session)
        backend.analyze([make_image(tmp_path)], make_hints())
        assert "Authorization" not in session.calls[0]["headers"]

    def test_fenced_json_is_parsed(self, tmp_path):
        fenced = "```json\n" + json.dumps(PAYLOAD) + "\n```"
        backend = OpenRouterVision(
            model="m", session=FakeSession([completion(content=fenced)])
        )
        facts = backend.analyze([make_image(tmp_path)], make_hints())
        assert facts.followers == 812

    def test_json_mode_rejected_retries_without_it(self, tmp_path):
        rejected = FakeResponse(400, {"error": "response_format is not supported"})
        session = FakeSession([rejected, completion()])
        backend = OpenRouterVision(model="m", session=session)
        facts = backend.analyze([make_image(tmp_path)], make_hints())

        assert len(session.calls) == 2
        assert "response_format" in session.calls[0]["json"]
        assert "response_format" not in session.calls[1]["json"]
        assert facts.followers == 812

    def test_no_images_returns_no_evidence(self):
        backend = OpenRouterVision(model="m", session=FakeSession([]))
        facts = backend.analyze([], make_hints())
        assert facts.image_count == 0
        assert facts.has_flag("no_evidence")
        assert not facts.has_flag("vision_error")

    def test_missing_model_degrades(self, tmp_path):
        backend = OpenRouterVision(model="", session=FakeSession([]))
        facts = backend.analyze([make_image(tmp_path)], make_hints())
        assert facts.has_flag("vision_error")
        assert "OPENROUTER_MODEL" in backend.last_error


class TestFailureModes:
    def test_connection_refused(self, tmp_path):
        backend = OpenRouterVision(
            model="m", session=FakeSession(error=ConnectionError("refused"))
        )
        facts = backend.analyze([make_image(tmp_path)], make_hints())
        assert facts.has_flag("vision_error")
        assert facts.confidence == 0
        assert "Tidak bisa konek" in backend.last_error
        assert "openrouter" in backend.last_error.lower()

    def test_401_points_at_api_key(self, tmp_path):
        backend = OpenRouterVision(
            model="m", session=FakeSession([FakeResponse(401, {"error": "unauthorized"})])
        )
        backend.analyze([make_image(tmp_path)], make_hints())
        assert "OPENROUTER_API_KEY" in backend.last_error

    def test_404_points_at_models_endpoint(self, tmp_path):
        backend = OpenRouterVision(
            model="nope/model", session=FakeSession([FakeResponse(404, {"error": "no model"})])
        )
        backend.analyze([make_image(tmp_path)], make_hints())
        assert "vision-status --models" in backend.last_error

    def test_502_explains_upstream(self, tmp_path):
        backend = OpenRouterVision(
            model="m", session=FakeSession([FakeResponse(502, "upstream connect failed")])
        )
        backend.analyze([make_image(tmp_path)], make_hints())
        assert "provider" in backend.last_error.lower()

    def test_non_vision_model_400(self, tmp_path):
        backend = OpenRouterVision(
            model="text-only",
            session=FakeSession([FakeResponse(400, {"error": "image input not supported"})]),
        )
        backend.analyze([make_image(tmp_path)], make_hints())
        assert "tidak mendukung image" in backend.last_error

    def test_garbage_content_degrades(self, tmp_path):
        backend = OpenRouterVision(
            model="m", session=FakeSession([completion(content="maaf saya tidak bisa")])
        )
        facts = backend.analyze([make_image(tmp_path)], make_hints())
        assert facts.has_flag("vision_error")


def model_entry(model_id: str, modalities=("text", "image"), price="0.0000001") -> dict:
    return {
        "id": model_id,
        "name": model_id,
        "context_length": 128000,
        "architecture": {"input_modalities": list(modalities)},
        "pricing": {"prompt": price},
    }


class TestHealth:
    def test_ok_when_model_listed(self):
        models = {"data": [model_entry("google/gemini-2.0-flash-001"), model_entry("openai/gpt-4o-mini")]}
        backend = OpenRouterVision(
            model="google/gemini-2.0-flash-001",
            api_key="sk-or-test",
            session=FakeSession([FakeResponse(200, models)]),
        )
        ok, message = backend.health()
        assert ok
        assert "model vision" in message

    def test_text_only_model_is_rejected(self):
        """A text-only model would 400 on image input — catch it before spending."""
        models = {"data": [model_entry("meta-llama/llama-3.1-8b-instruct", modalities=["text"])]}
        backend = OpenRouterVision(
            model="meta-llama/llama-3.1-8b-instruct",
            api_key="sk-or-test",
            session=FakeSession([FakeResponse(200, models)]),
        )
        ok, message = backend.health()
        assert not ok
        assert "bukan model vision" in message

    def test_unknown_model_lists_alternatives(self):
        models = {"data": [model_entry("google/gemini-2.0-flash-001")]}
        backend = OpenRouterVision(
            model="nope/model",
            api_key="sk-or-test",
            session=FakeSession([FakeResponse(200, models)]),
        )
        ok, message = backend.health()
        assert not ok
        assert "google/gemini-2.0-flash-001" in message

    def test_missing_api_key_is_reported(self):
        models = {"data": [model_entry("google/gemini-2.0-flash-001")]}
        backend = OpenRouterVision(
            model="google/gemini-2.0-flash-001",
            api_key="",
            session=FakeSession([FakeResponse(200, models)]),
        )
        ok, message = backend.health()
        assert not ok
        assert "OPENROUTER_API_KEY" in message

    def test_health_without_model(self):
        ok, message = OpenRouterVision(model="", session=FakeSession([])).health()
        assert not ok
        assert "OPENROUTER_MODEL" in message

    def test_list_models_parses_ids(self):
        models = {"data": [{"id": "a/1"}, {"id": "b/2"}, {"nope": True}]}
        backend = OpenRouterVision(model="a/1", session=FakeSession([FakeResponse(200, models)]))
        assert backend.list_models() == ["a/1", "b/2"]

    def test_list_vision_models_filters_and_sorts(self):
        models = {
            "data": [
                model_entry("openai/gpt-4o-mini", price="0.00000015"),
                model_entry("meta-llama/llama-3.1-8b", modalities=["text"]),
                model_entry("google/gemini-2.0-flash-001"),
            ]
        }
        backend = OpenRouterVision(model="x", session=FakeSession([FakeResponse(200, models)]))
        vision = backend.list_vision_models()
        assert [model["id"] for model in vision] == [
            "google/gemini-2.0-flash-001",
            "openai/gpt-4o-mini",
        ]
        assert vision[1]["prompt_price"] == "0.00000015"


class TestSmokeTest:
    def test_reads_text(self, tmp_path):
        image = make_image(tmp_path)
        response = completion(content='{"text": "MAMAZON TEST 4711", "confidence": 0.9}')
        backend = OpenRouterVision(model="m", session=FakeSession([response]))
        ok, detail, payload = backend.smoke_test(image.path)
        assert ok
        assert "MAMAZON" in detail
        assert payload["confidence"] == 0.9

    def test_empty_answer_is_failure(self, tmp_path):
        image = make_image(tmp_path)
        response = completion(content='{"text": "", "confidence": 0.1}')
        backend = OpenRouterVision(model="m", session=FakeSession([response]))
        ok, detail, _ = backend.smoke_test(image.path)
        assert not ok
        assert "vision-capable" in detail


class TestFactoryAndConfig:
    def test_factory_returns_openrouter(self):
        assert isinstance(build_vision_backend("openrouter"), OpenRouterVision)

    @pytest.mark.parametrize("alias", ["or", "openai", "openrouter.ai"])
    def test_factory_aliases(self, alias):
        assert isinstance(build_vision_backend(alias), OpenRouterVision)

    def test_default_base_url(self):
        backend = OpenRouterVision(model="m")
        assert backend.base_url == "https://openrouter.ai/api/v1"

    def test_base_url_trailing_slash_stripped(self):
        backend = OpenRouterVision(base_url="https://openrouter.ai/api/v1/", model="m")
        assert backend.base_url == "https://openrouter.ai/api/v1"

    def test_attribution_headers(self):
        backend = OpenRouterVision(model="m", api_key="k")
        headers = backend._headers()
        assert headers["HTTP-Referer"]
        assert headers["X-OpenRouter-Title"]

    def test_config_reports_missing_model(self, monkeypatch):
        monkeypatch.setattr(config, "VISION_PROVIDER", "openrouter")
        monkeypatch.setattr(config, "OPENROUTER_MODEL", "")
        issues = config.validate_config()
        assert any("OPENROUTER_MODEL" in issue for issue in issues)

    def test_config_reports_missing_key(self, monkeypatch):
        monkeypatch.setattr(config, "VISION_PROVIDER", "openrouter")
        monkeypatch.setattr(config, "OPENROUTER_MODEL", "google/gemini-2.0-flash-001")
        monkeypatch.setattr(config, "OPENROUTER_API_KEY", "")
        issues = config.validate_config()
        assert any("OPENROUTER_API_KEY" in issue for issue in issues)


class TestFactsMapper:
    def test_none_stays_none(self):
        facts = facts_from_payload({"caption_text": "x"}, 1, "openrouter")
        assert facts.followers is None
        assert facts.story_live is None
        assert facts.confidence == 0.0

    def test_flags_preserved(self):
        facts = facts_from_payload({"anomaly_flags": ["post_missing"]}, 1, "openrouter")
        assert facts.has_flag("post_missing")


class TestPrompt:
    def test_prompt_renders_despite_json_braces(self):
        """Regression: .format() used to blow up on the literal JSON example."""
        from src.vision_checker import build_prompt

        prompt = build_prompt(make_hints())
        assert "Task context:" in prompt
        assert '"caption_text"' in prompt
        assert "PP-03" in prompt
        assert "<<CONTEXT>>" not in prompt

    def test_prompt_includes_expected_values(self):
        from src.vision_checker import build_prompt

        prompt = build_prompt(make_hints())
        assert "#PPHorizon" in prompt
        assert "@horizon_umn" in prompt
