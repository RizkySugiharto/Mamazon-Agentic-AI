"""
Drive access behind a small interface.

  • MockDrive    — generates deterministic placeholder images so the whole
                   pipeline (hashing, dedup, compression, TTL) runs offline.
                   A small deterministic subset returns 403 so the
                   "link restricted" path is exercised.
  • GoogleDrive  — service-account download of shared Drive files.

The rest of the code only ever sees bytes / metadata, so swapping backends is a
config change (DRIVE_PROVIDER=google).
"""

from __future__ import annotations

import hashlib
import io
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from . import config


class DriveError(RuntimeError):
    """Base error for Drive operations."""


class DriveAccessError(DriveError):
    """403/404 — the file is not shared with the service account."""


class DriveRateLimitError(DriveError):
    """429/5xx — retry with backoff."""


@dataclass
class DriveFile:
    file_id: str
    name: str
    mime_type: str
    size: int
    data: bytes


class DriveBackend:
    name = "base"

    def fetch(self, file_id: str) -> DriveFile:  # pragma: no cover - interface
        raise NotImplementedError

    def list_folder(self, folder_id: str) -> list[dict]:  # pragma: no cover
        return []


# ─── Mock ───────────────────────────────────────────────────────────────────
class MockDrive(DriveBackend):
    name = "mock"

    def __init__(self, restricted_ratio: float = 0.02) -> None:
        self.restricted_ratio = restricted_ratio

    def _bucket(self, file_id: str) -> int:
        return int(hashlib.sha256(file_id.encode()).hexdigest()[:6], 16) % 100

    def fetch(self, file_id: str) -> DriveFile:
        if not file_id:
            raise DriveAccessError("empty file id")

        bucket = self._bucket(file_id)
        if bucket < int(self.restricted_ratio * 100):
            raise DriveAccessError(
                f"403 Forbidden — file {file_id[:8]}… belum di-share public/view-only"
            )

        data = _placeholder_png(file_id)
        return DriveFile(
            file_id=file_id,
            name=f"{file_id[:10]}.png",
            mime_type="image/png",
            size=len(data),
            data=data,
        )

    def list_folder(self, folder_id: str) -> list[dict]:
        return []


def _placeholder_png(file_id: str) -> bytes:
    """Tiny deterministic PNG (no Pillow dependency at import time)."""
    try:
        from PIL import Image

        digest = hashlib.sha256(file_id.encode()).digest()
        colour = (digest[0], digest[1], digest[2])
        image = Image.new("RGB", (16, 16), colour)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()
    except Exception:
        # 1x1 transparent PNG fallback.
        return bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
            "890000000a49444154789c6300010000050001"
            "0d0a2db40000000049454e44ae426082"
        )


# ─── Google ─────────────────────────────────────────────────────────────────
class GoogleDrive(DriveBackend):
    name = "google"

    def __init__(self, service_account_path: Path | None = None) -> None:
        from google.oauth2 import service_account

        path = Path(service_account_path or config.SERVICE_ACCOUNT_PATH)
        if not path.exists():
            raise DriveError(f"service account file not found: {path}")
        # Credential parsing is local (no network); the discovery document is
        # fetched lazily on first use, so importing the bot stays fast.
        self._creds = service_account.Credentials.from_service_account_file(
            str(path), scopes=["https://www.googleapis.com/auth/drive.readonly"]
        )
        self._service = None

    @property
    def service(self):
        if self._service is None:
            from googleapiclient.discovery import build

            self._service = build(
                "drive", "v3", credentials=self._creds, cache_discovery=False
            )
        return self._service

    def fetch(self, file_id: str) -> DriveFile:
        from googleapiclient.errors import HttpError

        try:
            meta = (
                self.service.files()
                .get(fileId=file_id, fields="id,name,mimeType,size")
                .execute()
            )
            data = self.service.files().get_media(fileId=file_id).execute()
        except HttpError as exc:
            status = getattr(exc.resp, "status", 0)
            if status in (403, 404):
                raise DriveAccessError(
                    f"{status} — file {file_id[:8]}… tidak dapat diakses. "
                    "Minta panitia share ke public view-only."
                ) from exc
            if status == 429 or status >= 500:
                raise DriveRateLimitError(f"{status} — rate limited") from exc
            raise DriveError(str(exc)) from exc

        return DriveFile(
            file_id=file_id,
            name=meta.get("name", file_id),
            mime_type=meta.get("mimeType", "image/jpeg"),
            size=int(meta.get("size") or len(data)),
            data=data,
        )

    def list_folder(self, folder_id: str) -> list[dict]:
        from googleapiclient.errors import HttpError

        try:
            result = (
                self.service.files()
                .list(
                    q=f"'{folder_id}' in parents and trashed=false",
                    fields="files(id,name,mimeType,size)",
                    pageSize=1000,
                )
                .execute()
            )
            return result.get("files", [])
        except HttpError as exc:
            status = getattr(exc.resp, "status", 0)
            if status in (403, 404):
                raise DriveAccessError(
                    f"{status} — folder {folder_id} tidak dapat diakses. "
                    "Share folder ke service account (Viewer)."
                ) from exc
            raise DriveError(str(exc)) from exc


class UnavailableDrive(DriveBackend):
    """
    Google Drive was requested but cannot run (missing library or key).

    Every fetch fails with an explanatory error, so the validator marks those
    submissions PENDING_MANUAL_REVIEW instead of feeding synthetic images to the
    Vision model.
    """

    name = "unavailable"

    def __init__(self, reason: str) -> None:
        self.reason = reason

    def fetch(self, file_id: str) -> DriveFile:
        raise DriveError(self.reason)


def build_drive_backend(provider: str | None = None) -> DriveBackend:
    provider = (provider or config.DRIVE_PROVIDER).lower()
    if provider == "google":
        try:
            return GoogleDrive()
        except Exception as exc:
            return UnavailableDrive(
                f"Google Drive tidak siap: {type(exc).__name__}: {exc}. "
                "Install `pip install google-api-python-client google-auth` dan "
                "taruh service account di agent/config/service_account.json."
            )
    return MockDrive()
