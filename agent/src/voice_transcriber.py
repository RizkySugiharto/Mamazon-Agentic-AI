"""Voice transcription component routing audio via 9Router (Groq Whisper).

Transcribes Discord voice notes and audio attachments to text for the LangGraph
chatbot agent.
"""

from __future__ import annotations

import logging
import time
from typing import Any
import requests

from . import config

logger = logging.getLogger(__name__)


class TranscriptionError(Exception):
    """Base exception for transcription failures."""


class TranscriptionConfigError(TranscriptionError):
    """Raised when 9Router gateway key or base URL is unconfigured."""


class VoiceDisabledError(TranscriptionError):
    """Raised when voice detection is disabled via VOICE_DETECTION_ENABLED."""


class TranscriptionEmptyError(TranscriptionError):
    """Raised when audio payload yields no speech or empty transcription."""


class TranscriptionRateLimitError(TranscriptionError):
    """Raised on HTTP 429 rate limit after retries."""


class VoiceTranscriber:
    """Client for 9Router audio transcription endpoint."""

    def __init__(
        self,
        base_url: str | None = None,
        api_key: str | None = None,
        model: str | None = None,
        timeout: int | None = None,
        user_agent: str | None = None,
    ) -> None:
        self.base_url = (base_url or config.effective_ninerouter_base_url()).rstrip("/")
        self.api_key = api_key if api_key is not None else config.NINEROUTER_API_KEY
        self.model = model or config.NINEROUTER_WHISPER_MODEL
        self.timeout = timeout or config.NINEROUTER_WHISPER_TIMEOUT
        self.user_agent = user_agent or config.NINEROUTER_USER_AGENT
        self.session = requests.Session()

    def transcribe_bytes(
        self,
        audio_data: bytes,
        filename: str = "voice-message.ogg",
        content_type: str = "audio/ogg",
    ) -> str:
        """Upload raw audio bytes to 9Router whisper endpoint and return text transcript.

        ponytail: synchronous requests upload with single 429 retry; upgrade to async aiohttp
        if voice note concurrency exceeds single-bot message dispatch limit.
        """
        if not config.VOICE_DETECTION_ENABLED:
            raise VoiceDisabledError("Fitur pesan suara (voice note) sedang dinonaktifkan.")

        if not self.api_key:
            raise TranscriptionConfigError("NINEROUTER_API_KEY is not configured")

        if not audio_data:
            raise TranscriptionEmptyError("Audio payload is empty")

        endpoint = f"{self.base_url}/audio/transcriptions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "User-Agent": self.user_agent,
        }
        data: dict[str, Any] = {
            "model": self.model,
            "response_format": "json",
            "temperature": "0.0",
        }
        files = {
            "file": (filename or "voice-message.ogg", audio_data, content_type or "audio/ogg"),
        }

        response = None
        for attempt in (1, 2):
            try:
                response = self.session.post(
                    endpoint,
                    headers=headers,
                    data=data,
                    files=files,
                    timeout=self.timeout,
                )
            except requests.RequestException as exc:
                if attempt == 2:
                    raise TranscriptionError(f"Network error contacting 9Router: {exc}") from exc
                time.sleep(1.0)
                continue

            if response.status_code != 429 or attempt == 2:
                break

            retry_after = 2.0
            try:
                retry_after = min(8.0, float(response.headers.get("Retry-After", 2.0)))
            except (TypeError, ValueError):
                pass
            time.sleep(retry_after)

        if response is None:
            raise TranscriptionError("No response received from 9Router whisper endpoint")

        if response.status_code == 401:
            raise TranscriptionConfigError(f"Invalid NINEROUTER_API_KEY (401): {response.text}")
        if response.status_code == 429:
            raise TranscriptionRateLimitError(f"9Router rate limit exceeded (429): {response.text}")
        if response.status_code >= 400:
            raise TranscriptionError(
                f"9Router transcription error ({response.status_code}): {response.text}"
            )

        try:
            payload = response.json()
            text = str(payload.get("text", "")).strip()
        except ValueError as exc:
            raise TranscriptionError(f"Invalid JSON returned by 9Router: {response.text}") from exc

        if not text:
            raise TranscriptionEmptyError("Transcription returned empty text")

        return text
