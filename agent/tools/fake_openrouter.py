"""
Minimal stand-in for the OpenRouter API (OpenAI-compatible).

Why: the Vision adapter should be testable end-to-end (HTTP, Bearer header,
base64 images, JSON parsing, model filtering) without spending credits. This
server speaks just enough of the protocol:

    GET  /api/v1/models            -> models with architecture.input_modalities
    POST /api/v1/chat/completions  -> reads the prompt context and answers with
                                      facts matching the expected caption/tags,
                                      so a validation run can run offline

Run:
    python -m tools.fake_openrouter --port 20129
    python -m tools.fake_openrouter --port 20129 --violate   # drop a hashtag

Then:
    VISION_PROVIDER=openrouter \
    OPENROUTER_BASE_URL=http://127.0.0.1:20129/api/v1 \
    OPENROUTER_API_KEY=test-key \
    OPENROUTER_MODEL=google/gemini-2.0-flash-001 \
    python -m src.main vision-status --models 5 --test-image
"""

from __future__ import annotations

import argparse
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# id, name, input modalities, prompt price per token
MODELS = [
    ("google/gemini-2.0-flash-001", "Google: Gemini 2.0 Flash", ["text", "image"], "0.0000001"),
    ("openai/gpt-4o-mini", "OpenAI: GPT-4o mini", ["text", "image"], "0.00000015"),
    ("anthropic/claude-3.5-sonnet", "Anthropic: Claude 3.5 Sonnet", ["text", "image"], "0.000003"),
    ("meta-llama/llama-3.1-8b-instruct", "Meta: Llama 3.1 8B", ["text"], "0.00000006"),
]

_CONTEXT_RE = re.compile(r"Task context:\s*(\{.*\})", re.DOTALL)


class Handler(BaseHTTPRequestHandler):
    server_version = "FakeOpenRouter/1.0"
    violate = False
    require_key: str | None = None

    # ── plumbing ───────────────────────────────────────────────────────────
    def log_message(self, fmt, *args):
        if getattr(self.server, "verbose", False):
            super().log_message(fmt, *args)

    def _send(self, status: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _authorized(self) -> bool:
        if not self.require_key:
            return True
        return self.headers.get("Authorization", "") == f"Bearer {self.require_key}"

    def _path(self) -> str:
        return self.path.split("?")[0].rstrip("/")

    # ── routes ─────────────────────────────────────────────────────────────
    def do_GET(self) -> None:  # noqa: N802
        if not self._path().endswith("/models"):
            self._send(404, {"error": {"message": "not found"}})
            return
        if not self._authorized():
            self._send(401, {"error": {"message": "unauthorized"}})
            return
        self._send(
            200,
            {
                "data": [
                    {
                        "id": model_id,
                        "name": name,
                        "context_length": 128000,
                        "architecture": {
                            "input_modalities": modalities,
                            "output_modalities": ["text"],
                        },
                        "pricing": {"prompt": price, "completion": price},
                    }
                    for model_id, name, modalities, price in MODELS
                ]
            },
        )

    def do_POST(self) -> None:  # noqa: N802
        if not self._path().endswith("/chat/completions"):
            self._send(404, {"error": {"message": "not found"}})
            return
        if not self._authorized():
            self._send(401, {"error": {"message": "no auth credentials found"}})
            return

        length = int(self.headers.get("Content-Length") or 0)
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            self._send(400, {"error": {"message": "invalid json"}})
            return

        answer = self._answer(_prompt_text(body))
        self._send(
            200,
            {
                "id": "gen-fake",
                "object": "chat.completion",
                "model": body.get("model", "fake"),
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": json.dumps(answer)},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {"prompt_tokens": 1100, "completion_tokens": 120, "total_tokens": 1220},
            },
        )

    # ── fake reasoning ─────────────────────────────────────────────────────
    def _answer(self, prompt: str) -> dict:
        if "Read the text in this image" in prompt:
            return {"text": "MAMAZON TEST 4711", "confidence": 0.97}

        match = _CONTEXT_RE.search(prompt)
        context: dict = {}
        if match:
            try:
                context = json.loads(match.group(1))
            except json.JSONDecodeError:
                context = {}

        hashtags = list(context.get("expected_hashtags") or [])
        if self.violate and hashtags:
            hashtags = hashtags[:-1]

        return {
            "caption_text": context.get("expected_caption") or "Caption sesuai brief",
            "comment_text": "keren banget postingannya bermanfaat buat mahasiswa",
            "review_text": "",
            "hashtags": hashtags,
            "mentions": list(context.get("expected_mentions") or []),
            "followers": 842,
            "is_private": False,
            "posted_at": context.get("submitted_at"),
            "story_live": True,
            "story_uploading_text": False,
            "likes": 1,
            "like_platforms": ["instagram", "tiktok"],
            "rating": 5,
            "vote_target": context.get("target_url") or "",
            "ig_username_seen": "",
            "aspect_ratio": "4:5",
            "carousel_slides": ["1", "2", "3", "4"],
            "anomaly_flags": [],
            "confidence": 0.91,
        }


def _prompt_text(body: dict) -> str:
    parts: list[str] = []
    for message in body.get("messages") or []:
        content = message.get("content")
        if isinstance(content, str):
            parts.append(content)
        elif isinstance(content, list):
            for part in content:
                if isinstance(part, dict) and part.get("type") == "text":
                    parts.append(str(part.get("text") or ""))
    return "\n".join(parts)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fake OpenRouter for offline testing")
    parser.add_argument("--port", type=int, default=20129)
    parser.add_argument("--violate", action="store_true", help="drop one hashtag")
    parser.add_argument("--api-key", default=None, help="require this Bearer token")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    Handler.violate = args.violate
    Handler.require_key = args.api_key

    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    server.verbose = args.verbose  # type: ignore[attr-defined]
    print(f"Fake OpenRouter listening on http://127.0.0.1:{args.port}/api/v1")
    print("models: " + ", ".join(model[0] for model in MODELS))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")


if __name__ == "__main__":
    main()
