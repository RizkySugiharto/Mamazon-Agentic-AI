"""9Router Vision adapter tests — no gateway required (injected fake session)."""

from __future__ import annotations

import json

import pytest

from src import config
from src.models import DownloadedImage
from src.vision_checker import (
    HTTPStatusError,
    MockVision,
    NineRouterVision,
    UnavailableVision,
    VisionHints,
    _Response,
    build_vision_backend,
    facts_from_payload,
)

BASE_URL = "http://gw.test/v1"

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
    def __init__(self, status_code: int, payload: dict | str, headers: dict | None = None) -> None:
        self.status_code = status_code
        self._payload = payload
        self.headers = headers or {}

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


def completion(payload: dict | None = None, content: str | None = None,
               finish_reason: str = "stop", reasoning: str = "") -> FakeResponse:
    message: dict = {
        "role": "assistant",
        "content": content if content is not None else json.dumps(payload or PAYLOAD),
    }
    if reasoning:
        message["reasoning_content"] = reasoning
    body = {
        "choices": [
            {"message": message, "finish_reason": finish_reason}
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


def backend(model="m", session=None, api_key="") -> NineRouterVision:
    return NineRouterVision(base_url=BASE_URL, model=model, api_key=api_key, session=session)


class TestAnalyze:
    def test_parses_facts(self, tmp_path):
        vision = backend(model="ag/gemini-3.8-flash", session=FakeSession([completion()]))
        facts = vision.analyze([make_image(tmp_path)], make_hints())

        assert facts.provider == "9router"
        assert facts.caption_text.startswith("Pusing")
        assert facts.followers == 812
        assert facts.aspect_ratio == "4:5"
        assert facts.confidence == 0.88
        assert facts.image_count == 1
        assert facts.raw["usage"]["prompt_tokens"] == 1200

    def test_request_shape_is_openai_compatible(self, tmp_path):
        session = FakeSession([completion()])
        vision = backend(model="ag/gemini-3.8-flash", session=session)
        vision.analyze([make_image(tmp_path)], make_hints())

        call = session.calls[0]
        assert call["url"].endswith("/v1/chat/completions")
        assert call["json"]["model"] == "ag/gemini-3.8-flash"
        assert call["json"]["stream"] is False
        content = call["json"]["messages"][0]["content"]
        assert content[0]["type"] == "text"
        assert content[1]["type"] == "image_url"
        assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")

    def test_bearer_header_when_key_set(self, tmp_path):
        session = FakeSession([completion()])
        vision = backend(model="m", api_key="secret-key", session=session)
        vision.analyze([make_image(tmp_path)], make_hints())
        assert session.calls[0]["headers"]["Authorization"] == "Bearer secret-key"

    def test_no_auth_header_without_key(self, tmp_path):
        session = FakeSession([completion()])
        vision = backend(model="m", api_key="", session=session)
        vision.analyze([make_image(tmp_path)], make_hints())
        assert "Authorization" not in session.calls[0]["headers"]

    def test_fenced_json_is_parsed(self, tmp_path):
        fenced = "```json\n" + json.dumps(PAYLOAD) + "\n```"
        vision = backend(model="m", session=FakeSession([completion(content=fenced)]))
        facts = vision.analyze([make_image(tmp_path)], make_hints())
        assert facts.followers == 812

    def test_json_mode_rejected_retries_without_it(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "NINEROUTER_JSON_MODE", True)
        rejected = FakeResponse(400, {"error": "response_format is not supported"})
        session = FakeSession([rejected, completion()])
        vision = backend(model="m", session=session)
        facts = vision.analyze([make_image(tmp_path)], make_hints())

        assert len(session.calls) == 2
        assert "response_format" in session.calls[0]["json"]
        assert "response_format" not in session.calls[1]["json"]
        assert facts.followers == 812

    def test_no_images_returns_no_evidence(self):
        vision = backend(model="m", session=FakeSession([]))
        facts = vision.analyze([], make_hints())
        assert facts.image_count == 0
        assert facts.has_flag("no_evidence")
        assert not facts.has_flag("vision_error")

    def test_missing_model_degrades(self, tmp_path):
        vision = backend(model="", session=FakeSession([]))
        facts = vision.analyze([make_image(tmp_path)], make_hints())
        assert facts.has_flag("vision_error")
        assert "NINEROUTER_MODEL" in vision.last_error


class TestRetriesAndEscalation:
    def test_429_honors_retry_after_once_then_succeeds(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.vision_checker.time.sleep", lambda *a: None)
        limited = FakeResponse(429, "rate limited", headers={"Retry-After": "1.5"})
        session = FakeSession([limited, completion()])
        vision = backend(model="m", session=session)
        facts = vision.analyze([make_image(tmp_path)], make_hints())

        assert len(session.calls) == 2
        assert facts.followers == 812

    def test_429_retry_after_is_honored_and_bounded(self, tmp_path, monkeypatch):
        delays = []
        monkeypatch.setattr("src.vision_checker.time.sleep", lambda delay: delays.append(delay))
        limited = FakeResponse(429, "rate limited", headers={"Retry-After": "60"})
        session = FakeSession([limited, completion()])
        vision = backend(model="m", session=session)
        vision.analyze([make_image(tmp_path)], make_hints())

        assert delays == [8.0]

    def test_persistent_429_explains_rate_limit(self, tmp_path, monkeypatch):
        monkeypatch.setattr("src.vision_checker.time.sleep", lambda *a: None)
        session = FakeSession([FakeResponse(429, "rate limited"), FakeResponse(429, "rate limited")])
        vision = backend(model="m", session=session)
        facts = vision.analyze([make_image(tmp_path)], make_hints())
        assert facts.has_flag("vision_error")
        assert "429" in vision.last_error
        assert "rate limit" in vision.last_error.lower()

    def test_empty_content_with_length_finish_escalates_budget(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "NINEROUTER_JSON_MODE", False)
        monkeypatch.setattr(config, "NINEROUTER_MAX_TOKENS", 3000)
        truncated = completion(content="", finish_reason="length")
        session = FakeSession([truncated, completion()])
        vision = backend(model="m", session=session)
        facts = vision.analyze([make_image(tmp_path)], make_hints())

        assert len(session.calls) == 2
        assert session.calls[1]["json"]["max_tokens"] == 8000
        assert facts.followers == 812

    def test_empty_content_with_reasoning_explains_the_cause(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "NINEROUTER_JSON_MODE", False)
        session = FakeSession([
            completion(content="", finish_reason="stop", reasoning="model is thinking")
        ])
        vision = backend(model="m", session=session)
        facts = vision.analyze([make_image(tmp_path)], make_hints())

        assert facts.has_flag("vision_error")
        assert "content kosong" in vision.last_error
        assert "reasoning" in vision.last_error.lower()
        assert "model is thinking"[:20] in vision.last_error

    def test_truncated_json_retries_with_bigger_budget(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "NINEROUTER_JSON_MODE", False)
        monkeypatch.setattr(config, "NINEROUTER_MAX_TOKENS", 1000)
        cut = completion(content='{"caption_text": "Pus', finish_reason="length")
        session = FakeSession([cut, completion()])
        vision = backend(model="m", session=session)
        facts = vision.analyze([make_image(tmp_path)], make_hints())

        assert len(session.calls) == 2
        assert session.calls[1]["json"]["max_tokens"] == 2000
        assert facts.followers == 812


class TestFailureModes:
    def test_connection_refused(self, tmp_path):
        vision = backend(model="m", session=FakeSession(error=ConnectionError("refused")))
        facts = vision.analyze([make_image(tmp_path)], make_hints())
        assert facts.has_flag("vision_error")
        assert facts.confidence == 0
        assert "Tidak bisa konek" in vision.last_error
        assert vision.base_url in vision.last_error

    def test_401_points_at_api_key(self, tmp_path):
        vision = backend(model="m", session=FakeSession([FakeResponse(401, {"error": "unauthorized"})]))
        vision.analyze([make_image(tmp_path)], make_hints())
        assert "NINEROUTER_API_KEY" in vision.last_error

    def test_402_points_at_quota(self, tmp_path):
        vision = backend(model="m", session=FakeSession([FakeResponse(402, {"error": "quota exceeded"})]))
        vision.analyze([make_image(tmp_path)], make_hints())
        assert "402" in vision.last_error
        assert "kuota" in vision.last_error.lower()

    def test_404_points_at_models_endpoint(self, tmp_path):
        vision = backend(model="nope/model", session=FakeSession([FakeResponse(404, {"error": "no model"})]))
        vision.analyze([make_image(tmp_path)], make_hints())
        assert "vision-status --models" in vision.last_error

    def test_502_explains_upstream(self, tmp_path):
        vision = backend(model="m", session=FakeSession([FakeResponse(502, "upstream connect failed")]))
        vision.analyze([make_image(tmp_path)], make_hints())
        assert "provider" in vision.last_error.lower()

    def test_non_vision_model_400(self, tmp_path):
        vision = backend(
            model="text-only",
            session=FakeSession([FakeResponse(400, {"error": "image input not supported"})]),
        )
        vision.analyze([make_image(tmp_path)], make_hints())
        assert "tidak mendukung image" in vision.last_error

    def test_garbage_content_degrades(self, tmp_path):
        vision = backend(model="m", session=FakeSession([completion(content="maaf saya tidak bisa")]))
        facts = vision.analyze([make_image(tmp_path)], make_hints())
        assert facts.has_flag("vision_error")


def model_entry(model_id: str, owned_by: str = "ag", vision: bool = True,
                tools: bool = False, context_length: int = 128000) -> dict:
    return {
        "id": model_id,
        "owned_by": owned_by,
        "capabilities": {"vision": vision, "tools": tools},
        "context_length": context_length,
    }


def models_response(entries: list[dict]) -> FakeResponse:
    return FakeResponse(200, {"data": entries})


MODELS = {
    "data": [
        {"id": "mamazon", "owned_by": "combo", "capabilities": {"vision": True, "tools": True}, "context_length": 1000000},
        {"id": "ag/gemini-3.8-flash", "owned_by": "ag", "capabilities": {"vision": True}, "context_length": 1000000},
        {"id": "text-only-model", "owned_by": "ag", "capabilities": {"vision": False}, "context_length": 32000},
    ]
}


class TestHealth:
    def test_ok_when_model_listed(self):
        vision = backend(model="mamazon", api_key="sk-test", session=FakeSession([models_response(MODELS["data"])]))
        ok, message = vision.health()
        assert ok
        assert "model vision" in message

    def test_text_only_model_is_rejected(self):
        """A text-only model would 400 on image input — catch it before spending."""
        entries = [model_entry("text-only-model", vision=False)]
        vision = backend(model="text-only-model", api_key="sk-test", session=FakeSession([models_response(entries)]))
        ok, message = vision.health()
        assert not ok
        assert "bukan model vision" in message

    def test_unknown_model_lists_alternatives(self):
        entries = [model_entry("ag/gemini-3.8-flash")]
        vision = backend(model="nope/model", api_key="sk-test", session=FakeSession([models_response(entries)]))
        ok, message = vision.health()
        assert not ok
        assert "ag/gemini-3.8-flash" in message

    def test_missing_api_key_is_reported(self):
        vision = backend(model="mamazon", api_key="", session=FakeSession([models_response(MODELS["data"])]))
        ok, message = vision.health()
        assert not ok
        assert "NINEROUTER_API_KEY" in message

    def test_health_without_model(self):
        ok, message = backend(model="", session=FakeSession([])).health()
        assert not ok
        assert "NINEROUTER_MODEL" in message

    def test_list_models_parses_ids(self):
        entries = [{"id": "a/1"}, {"id": "b/2"}, {"nope": True}]
        vision = backend(model="a/1", session=FakeSession([models_response(entries)]))
        assert vision.list_models() == ["a/1", "b/2"]

    def test_list_model_entries_parses_9router_fields(self):
        vision = backend(model="mamazon", session=FakeSession([models_response(MODELS["data"])]))
        entries = vision.list_model_entries()
        by_id = {entry["id"]: entry for entry in entries}

        assert by_id["mamazon"]["owned_by"] == "combo"
        assert by_id["mamazon"]["combo"] is True
        assert by_id["mamazon"]["vision"] is True
        assert by_id["mamazon"]["tools"] is True
        assert by_id["mamazon"]["context_length"] == 1000000

        assert by_id["ag/gemini-3.8-flash"]["combo"] is False
        assert by_id["ag/gemini-3.8-flash"]["vision"] is True
        assert by_id["ag/gemini-3.8-flash"]["tools"] is False

        assert by_id["text-only-model"]["vision"] is False

        # Entries are sorted by id for stable operator output.
        assert [entry["id"] for entry in entries] == sorted(by_id)

    def test_list_vision_models_filters_and_sorts(self):
        vision = backend(model="mamazon", session=FakeSession([models_response(MODELS["data"])]))
        vision_models = vision.list_vision_models()
        assert [model["id"] for model in vision_models] == [
            "ag/gemini-3.8-flash",
            "mamazon",
        ]


class TestSmokeTest:
    def test_reads_text(self, tmp_path):
        image = make_image(tmp_path)
        response = completion(content='{"text": "MAMAZON TEST 4711", "confidence": 0.9}')
        vision = backend(model="m", session=FakeSession([response]))
        ok, detail, payload = vision.smoke_test(image.path)
        assert ok
        assert "MAMAZON" in detail
        assert payload["confidence"] == 0.9

    def test_empty_answer_is_failure(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "NINEROUTER_JSON_MODE", False)
        image = make_image(tmp_path)
        response = completion(content='{"text": "", "confidence": 0.1}')
        vision = backend(model="m", session=FakeSession([response]))
        ok, detail, _ = vision.smoke_test(image.path)
        assert not ok
        assert "vision-capable" in detail


class TestFactoryAndConfig:
    def test_factory_returns_9router(self):
        assert isinstance(build_vision_backend("9router"), NineRouterVision)

    @pytest.mark.parametrize("alias", ["ninerouter", "9r", None])
    def test_factory_aliases(self, alias):
        assert isinstance(build_vision_backend(alias), NineRouterVision)

    def test_factory_selects_mock_and_rejects_removed(self):
        assert isinstance(build_vision_backend("mock"), MockVision)
        assert isinstance(build_vision_backend("offline"), MockVision)
        for removed in ("opencode", "opencode-api", "tencent", "agy"):
            backend = build_vision_backend(removed)
            assert isinstance(backend, UnavailableVision)
            assert "9Router" in backend.reason

    def test_default_base_url(self, monkeypatch):
        monkeypatch.setattr(config, "NINEROUTER_BASE_URL", "http://localhost:20128/v1")
        vision = NineRouterVision(model="m")
        assert vision.base_url == "http://localhost:20128/v1"

    def test_base_url_trailing_slash_stripped(self):
        vision = NineRouterVision(base_url="http://localhost:20128/v1/", model="m")
        assert vision.base_url == "http://localhost:20128/v1"

    def test_attribution_headers(self):
        vision = backend(model="m", api_key="k")
        headers = vision._headers()
        assert headers["HTTP-Referer"]
        assert headers["X-9Router-Title"]

    def test_config_reports_missing_model(self, monkeypatch):
        monkeypatch.setattr(config, "USE_MOCK_VISION", False)
        monkeypatch.setattr(config, "NINEROUTER_MODEL", "")
        issues = config.validate_config()
        assert any("NINEROUTER_MODEL" in issue for issue in issues)

    def test_config_reports_missing_key(self, monkeypatch):
        monkeypatch.setattr(config, "USE_MOCK_VISION", False)
        monkeypatch.setattr(config, "NINEROUTER_MODEL", "mamazon")
        monkeypatch.setattr(config, "NINEROUTER_API_KEY", "")
        issues = config.validate_config()
        assert any("NINEROUTER_API_KEY" in issue for issue in issues)


class TestFactsMapper:
    def test_none_stays_none(self):
        facts = facts_from_payload({"caption_text": "x"}, 1, "9router")
        assert facts.followers is None
        assert facts.story_live is None
        assert facts.confidence == 0.0

    def test_flags_preserved(self):
        facts = facts_from_payload({"anomaly_flags": ["post_missing"]}, 1, "9router")
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


class TestContentToText:
    def test_string_content_passes_through(self):
        from src.vision_checker import _content_to_text

        assert _content_to_text("halo") == "halo"

    def test_part_list_content_is_joined(self):
        from src.vision_checker import _content_to_text

        parts = [
            "baris satu",
            {"type": "text", "text": "baris dua"},
            {"type": "image_url"},
            None,
        ]
        assert _content_to_text(parts) == "baris satu\nbaris dua"

    def test_none_content_is_empty(self):
        from src.vision_checker import _content_to_text

        assert _content_to_text(None) == ""


class TestHTTPStatusError:
    def test_keeps_status_and_bounded_body(self):
        error = HTTPStatusError(429, "x" * 1000)
        assert error.status_code == 429
        assert str(error).startswith("HTTP 429: ")
        assert len(str(error)) < 400
