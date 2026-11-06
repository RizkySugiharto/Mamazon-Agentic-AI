"""
Vision backends.

Both backends must return the same `VisionFacts` shape:
  • MockVision     — deterministic, no API key, no network. Used for development
                     and for the offline demo. Seeded by (panitia, task) so the
                     same row always produces the same verdict.
  • TencentVision  — Tencent Cloud Hunyuan vision model, JSON-only prompt.
                     Any failure degrades to empty facts (checks then return
                     INCONCLUSIVE) instead of guessing.

Switch with VISION_PROVIDER=auto|mock|tencent in agent/.env.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Sequence

from . import config
from .models import DownloadedImage, TaskType, VisionFacts
from .utils import normalize_handle, parse_dt


# ─── Hints ──────────────────────────────────────────────────────────────────
@dataclass
class VisionHints:
    """
    Everything the backend knows *before* looking at pixels.

    Passing hints keeps the mock realistic (it can echo the expected caption and
    hashtags) and lets the real backend build a targeted prompt.
    """

    task_id: str = ""
    task_type: str = TaskType.PP
    nama: str = ""
    ig_username: str = ""
    submitted_at: datetime | None = None
    platforms: list[str] = field(default_factory=list)
    caption_reference: str = ""
    required_mentions: list[str] = field(default_factory=list)
    required_hashtags: list[str] = field(default_factory=list)
    jadwal_post: datetime | None = None
    deadline: datetime | None = None
    target_url: str = ""
    expected_slides: int = 0
    story_required: bool = False
    feed_required: bool = False
    blast_notes: list[str] = field(default_factory=list)

    @property
    def seed(self) -> int:
        payload = f"{self.nama.strip().lower()}|{self.task_id}".encode("utf-8")
        return int(hashlib.sha256(payload).hexdigest()[:8], 16)


class VisionBackend:
    name = "base"

    def analyze(
        self, images: Sequence[DownloadedImage], hints: VisionHints
    ) -> VisionFacts:  # pragma: no cover - interface
        raise NotImplementedError


# ─── Mock ───────────────────────────────────────────────────────────────────
_NATURAL_COMMENTS = [
    "keren banget postingannya, bermanfaat buat mahasiswa",
    "wah insightnya dapet, lanjutkan konten seperti ini",
    "suka sama konsepnya, semoga makin banyak yang lihat",
    "menarik, jadi pengen coba juga nih",
    "bagus sih ini, relate sama kehidupan kampus",
]

_REVIEWS = [
    "tempatnya nyaman, pelayanannya ramah dan cepat, recommended",
    "pengalaman beli di sini menyenangkan, produk sesuai deskripsi",
    "prosesnya gampang, respon admin cepat, puas belanja di sini",
]


class MockVision(VisionBackend):
    """
    Deterministic mock. Violation profiles are chosen by hashing
    (nama, task_id) so a sheet always produces the same mix of verdicts.
    """

    name = "mock"

    # profile name -> upper bound of the hash bucket
    _PP_PROFILES = [
        ("compliant", 86),
        ("missing_hashtag", 89),
        ("missing_tag", 92),
        ("caption_mismatch", 94),
        ("low_followers", 96),
        ("carousel_order", 97),
        ("story_expired", 98),
        ("post_missing", 99),
        ("wrong_submit", 100),
    ]

    _BUZZER_PROFILES = [
        ("compliant", 86),
        ("missing_platform", 90),
        ("emoji_comment", 93),
        ("short_comment", 95),
        ("rating_low", 97),
        ("wrong_vote", 99),
        ("post_missing", 100),
    ]

    def analyze(self, images: Sequence[DownloadedImage], hints: VisionHints) -> VisionFacts:
        usable = [image for image in images if image.ok]
        facts = VisionFacts(
            image_count=len(usable),
            provider=self.name,
            confidence=0.0,
        )

        if not usable:
            facts.confidence = 0.0
            facts.flag("no_evidence")
            return facts

        profile = self._pick_profile(hints)
        facts.raw["profile"] = profile
        facts.confidence = 0.78 + (hints.seed % 17) / 100.0  # 0.78 - 0.94

        if hints.task_type == TaskType.BUZZER:
            self._fill_buzzer(facts, hints, profile)
        else:
            self._fill_pp(facts, hints, profile)

        facts.image_count = len(usable)
        return facts

    def _pick_profile(self, hints: VisionHints) -> str:
        bucket = hints.seed % 100
        table = (
            self._BUZZER_PROFILES
            if hints.task_type == TaskType.BUZZER
            else self._PP_PROFILES
        )
        for name, upper in table:
            if bucket < upper:
                return name
        return "compliant"

    # ── PP ─────────────────────────────────────────────────────────────────
    def _fill_pp(self, facts: VisionFacts, hints: VisionHints, profile: str) -> None:
        caption = hints.caption_reference or (
            f"Caption resmi {hints.task_id} sesuai brief klien"
        )
        hashtags = list(hints.required_hashtags)
        mentions = list(hints.required_mentions)
        followers = 320 + (hints.seed % 2400)
        posted_at = hints.submitted_at
        story_live: bool | None = True if hints.story_required else None
        slides = [f"slide-{index + 1}" for index in range(hints.expected_slides or 1)]

        if profile == "missing_hashtag" and hashtags:
            drop = 1 + (hints.seed % min(2, len(hashtags)))
            hashtags = hashtags[:-drop]
        elif profile == "missing_tag" and mentions:
            mentions = mentions[1:]
        elif profile == "caption_mismatch" and caption:
            keep = max(20, len(caption) // 3)
            caption = caption[:keep] + " ... (caption diubah)"
        elif profile == "low_followers":
            followers = 120 + (hints.seed % 170)
        elif profile == "carousel_order":
            slides = list(reversed(slides))
            facts.flag("carousel_order_mismatch")
        elif profile == "story_expired":
            if hints.story_required:
                story_live = False
            else:
                hashtags = hashtags[:-1] if hashtags else hashtags
        elif profile == "post_missing":
            facts.flag("post_missing")
        elif profile == "wrong_submit":
            facts.flag("wrong_submit")
            caption = "Buzzer: like dan komentar postingan instagram"
            hashtags = []
            mentions = []
            facts.confidence = min(facts.confidence, 0.55)

        facts.caption_text = caption
        facts.hashtags = hashtags
        facts.mentions = mentions
        facts.followers = followers
        facts.is_private = False
        facts.posted_at = posted_at
        facts.story_live = story_live
        facts.story_uploading_text = profile == "story_expired" and hints.story_required
        facts.aspect_ratio = "4:5"
        facts.carousel_slides = slides if hints.feed_required else []
        facts.ig_username_seen = hints.ig_username
        facts.likes = 1 if hints.feed_required else None
        facts.like_platforms = ["instagram"] if hints.feed_required else []

    # ── Buzzer ─────────────────────────────────────────────────────────────
    def _fill_buzzer(self, facts: VisionFacts, hints: VisionHints, profile: str) -> None:
        platforms = _expected_platforms(hints)
        comment = _NATURAL_COMMENTS[hints.seed % len(_NATURAL_COMMENTS)]
        review = hints.caption_reference or _REVIEWS[hints.seed % len(_REVIEWS)]
        rating = 5.0
        vote_target = hints.target_url
        like_platforms = list(platforms)

        if profile == "missing_platform" and len(like_platforms) > 1:
            like_platforms = like_platforms[:1]
        elif profile == "missing_platform":
            like_platforms = []
        elif profile == "emoji_comment":
            comment = "🔥🔥🔥"
        elif profile == "short_comment":
            comment = "mantap"
        elif profile == "rating_low":
            rating = 4.0
        elif profile == "wrong_vote":
            vote_target = "https://example.com/campaign-lain"
        elif profile == "post_missing":
            facts.flag("post_missing")

        facts.comment_text = comment
        facts.review_text = review
        facts.rating = rating
        facts.vote_target = vote_target
        facts.like_platforms = like_platforms
        facts.likes = 1 if like_platforms else None
        facts.ig_username_seen = hints.ig_username
        facts.hashtags = list(hints.required_hashtags)
        facts.mentions = list(hints.required_mentions)
        facts.posted_at = hints.submitted_at
        facts.confidence = max(facts.confidence, 0.7)


def _expected_platforms(hints: VisionHints) -> list[str]:
    if hints.target_url and "tiktok" in hints.target_url:
        return ["instagram", "tiktok"]
    # BZ-01 is the only dual-platform task in the rulebase.
    if hints.task_id == "BZ-01":
        return ["instagram", "tiktok"]
    return ["instagram"]


# ─── Tencent ────────────────────────────────────────────────────────────────
_PROMPT = """You are an evidence checker for Instagram/TikTok task submissions.

Read the attached screenshot(s) and return ONLY a JSON object, no prose:
{
  "caption_text": "full caption visible in the screenshot, verbatim",
  "comment_text": "comment text if visible, else empty string",
  "review_text": "google maps review text if visible, else empty string",
  "hashtags": ["#example"],
  "mentions": ["@example"],
  "followers": 1234,
  "is_private": false,
  "posted_at": "YYYY-MM-DD HH:MM or empty",
  "story_live": true,
  "story_uploading_text": false,
  "likes": 1,
  "like_platforms": ["instagram"],
  "rating": 5,
  "vote_target": "url or empty",
  "ig_username_seen": "handle without @",
  "aspect_ratio": "4:5",
  "carousel_slides": ["1", "2"],
  "anomaly_flags": ["edited_image", "text_inconsistent"],
  "confidence": 0.0
}

Rules:
- Transcribe text EXACTLY, including typos and emoji. Never correct or translate.
- Use null when a value is not visible. Do not guess.
- anomaly_flags may include: post_missing, carousel_order_mismatch,
  aspect_ratio_mismatch, edited_image, text_inconsistent, wrong_submit.
- confidence is 0-1 for the overall reading.

Task context: <<CONTEXT>>
"""


def build_prompt(hints: "VisionHints") -> str:
    """
    Render the shared prompt.

    Uses a token replace instead of str.format: the prompt body contains literal
    JSON braces, which .format() would treat as fields.
    """
    return _PROMPT.replace("<<CONTEXT>>", _context_text(hints))


class TencentVision(VisionBackend):
    """
    Tencent Cloud Hunyuan vision backend.

    Requires TENCENT_SECRET_ID / TENCENT_SECRET_KEY. On any error the backend
    returns empty facts so the engine degrades to INCONCLUSIVE — never a fine.
    """

    name = "tencent"

    def __init__(self) -> None:
        self._client = None
        self._init_error: str | None = None
        try:  # imported lazily so the mock path needs no SDK
            from tencentcloud.common import credential
            from tencentcloud.common.profile.client_profile import ClientProfile
            from tencentcloud.common.profile.http_profile import HttpProfile
            from tencentcloud.hunyuan.v20230901 import hunyuan_client

            cred = credential.Credential(
                config.TENCENT_SECRET_ID, config.TENCENT_SECRET_KEY
            )
            http_profile = HttpProfile()
            http_profile.endpoint = config.TENCENT_VISION_ENDPOINT
            http_profile.reqTimeout = 60
            client_profile = ClientProfile()
            client_profile.httpProfile = http_profile
            self._client = hunyuan_client.HunyuanClient(
                cred, config.TENCENT_REGION, client_profile
            )
        except Exception as exc:  # missing SDK or bad credentials
            self._init_error = f"{type(exc).__name__}: {exc}"

    def analyze(self, images: Sequence[DownloadedImage], hints: VisionHints) -> VisionFacts:
        usable = [image for image in images if image.ok and image.path]
        if not usable or self._client is None:
            facts = VisionFacts(image_count=len(usable), provider=self.name, confidence=0.0)
            facts.raw["error"] = self._init_error or "no usable images"
            return facts

        try:
            payload = self._call_api(usable, hints)
            return self._to_facts(payload, len(usable))
        except Exception as exc:
            facts = VisionFacts(image_count=len(usable), provider=self.name, confidence=0.0)
            facts.raw["error"] = f"{type(exc).__name__}: {exc}"
            facts.flag("vision_error")
            return facts

    def _call_api(self, images: Sequence[DownloadedImage], hints: VisionHints) -> dict:
        from tencentcloud.hunyuan.v20230901 import models as hunyuan_models

        content: list[dict] = [
            {
                "Type": "text",
                "Text": build_prompt(hints),
            }
        ]
        for image in images[:8]:  # cap: 8 screenshots per submission
            with open(image.path, "rb") as handle:
                encoded = base64.b64encode(handle.read()).decode("ascii")
            content.append(
                {"Type": "image_url", "ImageUrl": {"Url": f"data:image/jpeg;base64,{encoded}"}}
            )

        request = hunyuan_models.ChatCompletionsRequest()
        request.from_json_string(
            json.dumps(
                {
                    "Model": config.TENCENT_VISION_MODEL,
                    "Messages": [{"Role": "user", "Content": content}],
                    "Stream": False,
                }
            )
        )
        response = self._client.ChatCompletions(request)
        text = response.Choices[0].Message.Content or ""
        return _extract_json(text)

    def _to_facts(self, payload: dict, image_count: int) -> VisionFacts:
        return facts_from_payload(payload, image_count, self.name)


def _context_text(hints: VisionHints) -> str:
    return json.dumps(
        {
            "task_id": hints.task_id,
            "task_type": hints.task_type,
            "platforms": hints.platforms,
            "submitted_at": hints.submitted_at.isoformat() if hints.submitted_at else None,
            "expected_mentions": hints.required_mentions,
            "expected_hashtags": hints.required_hashtags,
            "expected_caption": hints.caption_reference[:600],
            "target_url": hints.target_url,
            "blast_notes": hints.blast_notes,
        },
        ensure_ascii=False,
    )


def _extract_json(text: str) -> dict:
    """Pull the first JSON object out of a model response."""
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    match = re.search(r"\{.*\}", text, flags=re.DOTALL)
    if not match:
        raise ValueError("no JSON object in response")
    return json.loads(match.group(0))


def _content_to_text(content: object) -> str:
    """OpenAI `message.content` is a string, but some routers return parts."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for part in content:
            if isinstance(part, str):
                parts.append(part)
            elif isinstance(part, dict):
                text = part.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "\n".join(parts)
    return "" if content is None else str(content)


def facts_from_payload(payload: dict, image_count: int, provider: str) -> VisionFacts:
    """
    Map the model's JSON answer onto VisionFacts.

    Shared by every real backend so Tencent and OpenRouter can never drift apart.
    Unknown/missing keys stay None, which makes the checks return INCONCLUSIVE
    instead of guessing.
    """
    facts = VisionFacts(
        image_count=image_count,
        provider=provider,
        confidence=float(payload.get("confidence") or 0.0),
        caption_text=str(payload.get("caption_text") or ""),
        comment_text=str(payload.get("comment_text") or ""),
        review_text=str(payload.get("review_text") or ""),
        hashtags=list(payload.get("hashtags") or []),
        mentions=list(payload.get("mentions") or []),
        followers=payload.get("followers"),
        is_private=payload.get("is_private"),
        posted_at=parse_dt(payload.get("posted_at")),
        story_live=payload.get("story_live"),
        story_uploading_text=bool(payload.get("story_uploading_text")),
        likes=payload.get("likes"),
        like_platforms=list(payload.get("like_platforms") or []),
        rating=payload.get("rating"),
        vote_target=payload.get("vote_target"),
        ig_username_seen=payload.get("ig_username_seen"),
        aspect_ratio=payload.get("aspect_ratio"),
        carousel_slides=[str(slide) for slide in payload.get("carousel_slides") or []],
        anomaly_flags=list(payload.get("anomaly_flags") or []),
        raw=payload,
    )
    return facts


# ─── OpenRouter (OpenAI-compatible) ────────────────────────────────────────────
class HTTPStatusError(RuntimeError):
    """HTTP 4xx/5xx from the router, with the status kept for diagnostics."""

    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"HTTP {status_code}: {body[:300]}")
        self.status_code = status_code
        self.body = body


class _Response:
    """Minimal response object so tests can inject a fake session."""

    def __init__(self, status_code: int, body: bytes | str, headers: dict | None = None) -> None:
        self.status_code = status_code
        self._body = body.decode("utf-8", "replace") if isinstance(body, bytes) else body
        self.headers = headers or {}

    @property
    def text(self) -> str:
        return self._body

    def json(self) -> dict:
        return json.loads(self._body)


class _UrllibSession:
    """
    Stdlib-only HTTP transport.

    Deliberately avoids `requests`: the vision path must work on a bare Python
    install, and a missing dependency should never look like a broken model.
    """

    def get(self, url: str, headers: dict | None = None, timeout: float | None = None):
        return self._request("GET", url, headers, None, timeout)

    def post(
        self,
        url: str,
        headers: dict | None = None,
        json: dict | None = None,
        timeout: float | None = None,
    ):
        payload = __import__("json").dumps(json).encode("utf-8") if json is not None else None
        return self._request("POST", url, headers, payload, timeout)

    @staticmethod
    def _request(method, url, headers, data, timeout):
        import urllib.error
        import urllib.request

        request = urllib.request.Request(
            url, data=data, headers=headers or {}, method=method
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return _Response(
                    getattr(response, "status", 200),
                    response.read(),
                    dict(response.headers or {}),
                )
        except urllib.error.HTTPError as exc:
            return _Response(exc.code, exc.read(), dict(exc.headers or {}))
        except urllib.error.URLError as exc:
            raise ConnectionError(str(exc.reason)) from exc
        except TimeoutError as exc:
            raise TimeoutError("request timeout") from exc


class OpenRouterVision(VisionBackend):
    """
    Vision through OpenRouter (https://openrouter.ai/api/v1).

        export VISION_PROVIDER=openrouter
        export OPENROUTER_API_KEY=sk-or-v1-...
        export OPENROUTER_MODEL=google/gemini-2.0-flash-001   # must accept images

    OpenRouter is OpenAI-compatible, so the request is a normal chat completion
    with `image_url` data-URL parts. Any failure returns empty facts flagged
    `vision_error`, which the checks turn into INCONCLUSIVE — it never invents a
    verdict and never falls back to mock, because a mock verdict would look like
    real evidence for a real panitia.
    """

    name = "openrouter"

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        session=None,
        timeout: int | None = None,
        site_url: str | None = None,
        app_name: str | None = None,
    ) -> None:
        self.base_url = (base_url or config.OPENROUTER_BASE_URL).rstrip("/")
        self.api_key = api_key if api_key is not None else config.OPENROUTER_API_KEY
        # `is not None` (not `or`): an explicit empty model must stay empty so
        # health() can report it instead of silently inheriting the env value.
        self.model = model if model is not None else config.OPENROUTER_MODEL
        self.timeout = timeout or config.OPENROUTER_TIMEOUT
        self.site_url = site_url if site_url is not None else config.OPENROUTER_SITE_URL
        self.app_name = app_name if app_name is not None else config.OPENROUTER_APP_NAME
        self._session = session
        self.last_error = ""

    # ── plumbing ───────────────────────────────────────────────────────────
    @property
    def session(self):
        if self._session is None:
            self._session = _UrllibSession()
        return self._session

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        # Optional attribution; only affects OpenRouter's leaderboards.
        if self.site_url:
            headers["HTTP-Referer"] = self.site_url
        if self.app_name:
            headers["X-OpenRouter-Title"] = self.app_name
        return headers

    def list_models(self) -> list[str]:
        """GET /models — public on OpenRouter, so this works before a key exists."""
        response = self.session.get(
            f"{self.base_url}/models",
            headers=self._headers(),
            timeout=min(20, self.timeout),
        )
        if response.status_code >= 400:
            raise HTTPStatusError(response.status_code, response.text)
        data = response.json()
        return [
            str(item.get("id"))
            for item in (data.get("data") or [])
            if isinstance(item, dict) and item.get("id")
        ]

    def list_vision_models(self) -> list[dict]:
        """
        Models whose `architecture.input_modalities` includes image.

        OpenRouter publishes this, so we can refuse a text-only model before
        spending a credit on a request that would 400.
        """
        response = self.session.get(
            f"{self.base_url}/models",
            headers=self._headers(),
            timeout=min(20, self.timeout),
        )
        if response.status_code >= 400:
            raise HTTPStatusError(response.status_code, response.text)
        data = response.json()

        vision: list[dict] = []
        for item in data.get("data") or []:
            if not isinstance(item, dict) or not item.get("id"):
                continue
            architecture = item.get("architecture") or {}
            modalities = architecture.get("input_modalities") or []
            if "image" not in modalities:
                continue
            pricing = item.get("pricing") or {}
            vision.append(
                {
                    "id": str(item["id"]),
                    "name": item.get("name", ""),
                    "context": item.get("context_length"),
                    "prompt_price": pricing.get("prompt"),
                }
            )
        vision.sort(key=lambda model: model["id"])
        return vision

    def health(self) -> tuple[bool, str]:
        if not self.model:
            return False, (
                "OPENROUTER_MODEL kosong — pilih model vision, lihat "
                "`vision-status --models 20`"
            )
        try:
            vision_models = self.list_vision_models()
        except Exception as exc:
            return False, self._explain(exc)

        ids = {model["id"] for model in vision_models}
        if self.model not in ids:
            examples = ", ".join(sorted(ids)[:5])
            return False, (
                f"'{self.model}' bukan model vision yang terdaftar "
                f"({len(ids)} model vision tersedia). Contoh: {examples}. "
                "Jalankan `vision-status --models 20` untuk daftar lengkap."
            )
        if not self.api_key:
            return False, (
                f"Model '{self.model}' OK, tapi OPENROUTER_API_KEY kosong — "
                "request chat akan 401. Buat key di https://openrouter.ai/keys"
            )
        return True, f"OK — '{self.model}' terdaftar sebagai model vision"

    # ── analyze ────────────────────────────────────────────────────────────
    def analyze(
        self, images: Sequence[DownloadedImage], hints: VisionHints
    ) -> VisionFacts:
        usable = [image for image in images if image.ok and image.path]
        if not usable:
            facts = VisionFacts(image_count=0, provider=self.name, confidence=0.0)
            facts.flag("no_evidence")
            return facts
        if not self.model:
            facts = VisionFacts(
                image_count=len(usable), provider=self.name, confidence=0.0
            )
            facts.raw["error"] = "OPENROUTER_MODEL kosong"
            facts.flag("vision_error")
            self.last_error = "OPENROUTER_MODEL kosong"
            return facts

        try:
            payload = self._call(usable, hints)
        except Exception as exc:
            self.last_error = self._explain(exc)
            facts = VisionFacts(
                image_count=len(usable), provider=self.name, confidence=0.0
            )
            facts.raw["error"] = self.last_error
            facts.flag("vision_error")
            return facts

        facts = facts_from_payload(payload, len(usable), self.name)
        usage = payload.get("__usage__")
        if usage:
            facts.raw["usage"] = usage
        self.last_error = ""
        return facts

    def _call(self, images: Sequence[DownloadedImage], hints: VisionHints) -> dict:
        content = self._content(images, hints)
        return self._chat(content)

    def smoke_test(self, image_path: str | Path) -> tuple[bool, str, dict]:
        """
        Send one real image with a trivial prompt.

        Verifies transport + auth + vision + JSON parsing in a single call, which
        is the fastest way to find out whether a router model can actually see.
        """
        try:
            with open(image_path, "rb") as handle:
                encoded = base64.b64encode(handle.read()).decode("ascii")
        except OSError as exc:
            return False, f"tidak bisa membaca gambar: {exc}", {}

        content = [
            {
                "type": "text",
                "text": (
                    "Read the text in this image. Reply with ONLY this JSON: "
                    '{"text": "<the text you see>", "confidence": <0-1>}'
                ),
            },
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/png;base64,{encoded}"},
            },
        ]
        try:
            payload = self._chat(content)
        except Exception as exc:
            self.last_error = self._explain(exc)
            return False, self.last_error, {}
        text = str(payload.get("text") or "").strip()
        if not text:
            return False, "model membalas tanpa teks — kemungkinan tidak vision-capable", payload
        return True, text, payload

    def _chat(self, content: list, max_tokens: int | None = None) -> dict:
        budget = max_tokens or config.OPENROUTER_MAX_TOKENS
        body = {
            "model": self.model,
            "messages": [{"role": "user", "content": content}],
            "temperature": 0,
            "max_tokens": budget,
            "stream": False,
        }
        if config.OPENROUTER_JSON_MODE:
            body["response_format"] = {"type": "json_object"}
        if config.OPENROUTER_REASONING_EFFORT:
            # Reasoning models otherwise spend the whole token budget thinking
            # and return empty content.
            body["reasoning"] = {"effort": config.OPENROUTER_REASONING_EFFORT}

        data = self._post(body)
        if data is None:  # provider rejected response_format — retry plainly
            retry_body = {key: value for key, value in body.items() if key != "response_format"}
            data = self._post(retry_body)
        if data is None:
            raise RuntimeError("request ditolak router (400) dua kali")

        try:
            choice = data["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ValueError(f"respons tidak sesuai format OpenAI: {data}") from exc

        finish = choice.get("finish_reason")
        text = _content_to_text(message.get("content"))
        if not text.strip():
            # A reasoning model can burn the whole budget before answering.
            # Escalate once; if it still truncates, report instead of guessing.
            if finish == "length" and budget < 8000:
                return self._chat(content, max_tokens=8000)
            reasoning = message.get("reasoning") or message.get("reasoning_content") or ""
            hint = (
                f"content kosong (finish_reason={finish}). "
                "Model menghabiskan token untuk reasoning — pakai model vision "
                "non-reasoning (mis. google/gemini-2.0-flash-001) atau naikkan "
                "OPENROUTER_MAX_TOKENS."
            )
            if reasoning:
                hint += f" Reasoning: {str(reasoning)[:120]}"
            raise ValueError(hint)

        try:
            payload = _extract_json(text)
        except ValueError:
            if finish == "length" and budget < 8000:
                # Truncated mid-answer: retry once with a bigger budget.
                return self._chat(content, max_tokens=min(8000, budget * 2))
            raise ValueError(
                f"tidak ada JSON di respons (finish_reason={finish}): {text[:160]!r}"
            )

        if data.get("usage"):
            payload["__usage__"] = data["usage"]
        return payload

    def _post(self, body: dict) -> dict | None:
        """POST /chat/completions. Returns None when JSON mode is rejected."""
        attempts = 2
        response = None
        for attempt in range(1, attempts + 1):
            response = self.session.post(
                f"{self.base_url}/chat/completions",
                headers=self._headers(),
                json=body,
                timeout=self.timeout,
            )
            if response.status_code != 429 or attempt == attempts:
                break
            # Free models are rate limited; honour Retry-After but never stall a
            # whole validation run for long.
            delay = 2.0
            try:
                delay = min(8.0, float(response.headers.get("Retry-After", 2)))
            except (TypeError, ValueError):
                pass
            time.sleep(max(0.5, delay))

        if (
            response.status_code == 400
            and "response_format" in body
            and "response_format" in (response.text or "").lower()
        ):
            return None
        if response.status_code >= 400:
            raise HTTPStatusError(response.status_code, response.text or "")
        return response.json()

    def _content(self, images: Sequence[DownloadedImage], hints: VisionHints) -> list:
        parts: list[dict] = [{"type": "text", "text": build_prompt(hints)}]
        for image in list(images)[: config.OPENROUTER_MAX_IMAGES]:
            try:
                with open(image.path, "rb") as handle:
                    encoded = base64.b64encode(handle.read()).decode("ascii")
            except OSError:
                continue
            mime = "image/png" if str(image.path).lower().endswith(".png") else "image/jpeg"
            parts.append(
                {
                    "type": "image_url",
                    "image_url": {"url": f"data:{mime};base64,{encoded}"},
                }
            )
        return parts

    def _explain(self, exc: Exception) -> str:
        """Turn transport errors into an actionable message for the operator."""
        text = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, ConnectionError):
            return (
                f"Tidak bisa konek ke {self.base_url}. Cek koneksi internet / proxy, "
                "lalu `python -m src.main vision-status`."
            )
        if isinstance(exc, TimeoutError):
            return (
                f"Timeout setelah {self.timeout}s. Model vision bisa lambat untuk "
                "banyak gambar — naikkan OPENROUTER_TIMEOUT atau turunkan "
                "OPENROUTER_MAX_IMAGES."
            )
        if isinstance(exc, HTTPStatusError):
            status = exc.status_code
            if status == 401:
                return (
                    "401 Unauthorized — OPENROUTER_API_KEY salah atau kosong. "
                    "Buat key di https://openrouter.ai/keys lalu isi ulang."
                )
            if status == 402:
                return (
                    "402 Payment Required — kredit OpenRouter habis. Top up di "
                    "https://openrouter.ai/credits, atau pilih model yang lebih murah."
                )
            if status == 404:
                return (
                    f"404 — model '{self.model}' tidak dikenal. "
                    "Jalankan `vision-status --models 20` untuk daftar model vision."
                )
            if status == 429:
                return (
                    "429 — rate limit OpenRouter. Model ':free' dibatasi ketat "
                    "(±20 req/menit, dan kuota harian lebih kecil bila saldo < $10). "
                    "Tunggu 1 menit, atau pakai model berbayar murah. "
                    "Validasi penuh 76 baris bisa kena limit ini berkali-kali."
                )
            if status in (502, 503, 504):
                return (
                    f"{status} — provider di balik OpenRouter sedang gangguan. "
                    "Coba lagi, atau pilih model lain. "
                    f"Respons: {exc.body[:120]}"
                )
            if status in (400, 422):
                return (
                    f"{status} — request ditolak. Kemungkinan model '{self.model}' "
                    f"tidak mendukung image input. Respons: {exc.body[:160]}"
                )
            return f"{status} — {exc.body[:200]}"
        return text


# ─── Unavailable (misconfigured) ────────────────────────────────────────────
class UnavailableVision(VisionBackend):
    """
    Returned when a real backend was requested but cannot run (missing SDK,
    missing credentials). Every call yields `vision_error`, which the checks turn
    into INCONCLUSIVE / PENDING_MANUAL_REVIEW.

    This exists so a misconfigured install can never quietly produce mock
    verdicts that look like real evidence.
    """

    name = "unavailable"

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def analyze(
        self, images: Sequence[DownloadedImage], hints: VisionHints
    ) -> VisionFacts:
        usable = [image for image in images if image.ok]
        facts = VisionFacts(
            image_count=len(usable), provider=self.name, confidence=0.0
        )
        facts.raw["error"] = self.reason
        facts.flag("vision_error")
        return facts


# ─── Factory ────────────────────────────────────────────────────────────────
def build_vision_backend(provider: str | None = None) -> VisionBackend:
    provider = (provider or config.VISION_PROVIDER).lower()
    if provider in {"openrouter", "or", "openrouter.ai", "openai"}:
        return OpenRouterVision()
    if provider == "tencent":
        backend = TencentVision()
        if backend._client is not None:
            return backend
        return UnavailableVision(
            "Tencent Vision tidak siap: "
            f"{backend._init_error or 'client gagal dibuat'}. "
            "Install `pip install tencentcloud-sdk-python` dan isi "
            "TENCENT_SECRET_ID/KEY, atau pindah ke OpenRouter "
            "(VISION_PROVIDER=openrouter)."
        )
    return MockVision()
