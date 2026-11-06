"""
Image manager: download evidence files, deduplicate by SHA-256, compress large
images before Vision, and sweep the temp folder on a 24h TTL.

Guarantees:
  • Nothing is stored permanently — `tmp/images/` is disposable.
  • A file already downloaded for the same Drive id is reused (no second GET).
  • Identical bytes are stored once (hash dedup), even across panitia.
  • Files above IMAGE_MAX_BYTES are re-encoded below IMAGE_TARGET_BYTES.
  • Restricted links return DownloadedImage(error=...) instead of raising, so a
    single bad link cannot abort a 76-row validation run.
"""

from __future__ import annotations

import io
import json
import time
from dataclasses import asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Sequence

from . import config
from .drive_client import (
    DriveAccessError,
    DriveBackend,
    DriveError,
    DriveRateLimitError,
    build_drive_backend,
)
from .models import DownloadedImage
from .utils import ensure_dir, human_size, sha256_bytes, wib_now


def _suffix_for(mime_type: str) -> str:
    mapping = {
        "image/png": ".png",
        "image/webp": ".webp",
        "image/gif": ".gif",
        "image/heic": ".heic",
        "image/jpeg": ".jpg",
        "image/jpg": ".jpg",
    }
    return mapping.get((mime_type or "").lower(), ".jpg")


class ImageManager:
    def __init__(
        self,
        drive: DriveBackend | None = None,
        tmp_dir: Path | None = None,
        ttl_hours: int | None = None,
        max_bytes: int | None = None,
        target_bytes: int | None = None,
        cache_path: Path | None = None,
    ) -> None:
        self.drive = drive or build_drive_backend()
        self.tmp_dir = ensure_dir(Path(tmp_dir or config.IMAGE_TMP_DIR))
        self.cache_path = Path(cache_path or config.HASH_CACHE_PATH)
        self.ttl_hours = ttl_hours if ttl_hours is not None else config.IMAGE_TTL_HOURS
        self.max_bytes = max_bytes if max_bytes is not None else config.IMAGE_MAX_BYTES
        self.target_bytes = (
            target_bytes if target_bytes is not None else config.IMAGE_TARGET_BYTES
        )
        self._cache: dict[str, dict] = self._load_cache()

    # ── cache ──────────────────────────────────────────────────────────────
    def _load_cache(self) -> dict[str, dict]:
        if not self.cache_path.exists():
            return {}
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return {}

    def _save_cache(self) -> None:
        ensure_dir(self.cache_path.parent)
        try:
            self.cache_path.write_text(
                json.dumps(self._cache, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    # ── public API ─────────────────────────────────────────────────────────
    def fetch_many(self, file_ids: Sequence[str], progress=None) -> list[DownloadedImage]:
        results: list[DownloadedImage] = []
        for index, file_id in enumerate(file_ids, start=1):
            results.append(self.fetch(file_id))
            if progress is not None:
                progress(index, len(file_ids), file_id)
        return results

    def fetch(self, file_id: str) -> DownloadedImage:
        if not file_id:
            return DownloadedImage(file_id="", error="empty file id")

        cached = self._cache.get(file_id)
        if cached:
            path = Path(cached["path"])
            if path.exists():
                return DownloadedImage(
                    file_id=file_id,
                    path=str(path),
                    sha256=cached["sha256"],
                    size_bytes=cached["size"],
                    dedup_hit=True,
                    mime_type=cached.get("mime_type", "image/jpeg"),
                )

        try:
            drive_file = self._fetch_with_retry(file_id)
        except DriveAccessError as exc:
            return DownloadedImage(file_id=file_id, error=str(exc))
        except DriveError as exc:
            return DownloadedImage(file_id=file_id, error=str(exc))
        except Exception as exc:  # defensive: never abort a batch
            return DownloadedImage(file_id=file_id, error=f"{type(exc).__name__}: {exc}")

        digest = sha256_bytes(drive_file.data)
        existing = self._find_by_hash(digest)
        if existing is not None:
            self._cache[file_id] = {
                "path": str(existing),
                "sha256": digest,
                "size": existing.stat().st_size,
                "mime_type": drive_file.mime_type,
                "cached_at": wib_now().isoformat(),
            }
            self._save_cache()
            return DownloadedImage(
                file_id=file_id,
                path=str(existing),
                sha256=digest,
                size_bytes=existing.stat().st_size,
                dedup_hit=True,
                mime_type=drive_file.mime_type,
            )

        suffix = _suffix_for(drive_file.mime_type)
        path = self.tmp_dir / f"{digest[:16]}{suffix}"
        path.write_bytes(drive_file.data)
        size = len(drive_file.data)

        if size > self.max_bytes:
            compressed = self._compress(path)
            if compressed is not None:
                size = compressed

        self._cache[file_id] = {
            "path": str(path),
            "sha256": digest,
            "size": size,
            "mime_type": drive_file.mime_type,
            "cached_at": wib_now().isoformat(),
        }
        self._save_cache()

        return DownloadedImage(
            file_id=file_id,
            path=str(path),
            sha256=digest,
            size_bytes=size,
            mime_type=drive_file.mime_type,
        )

    def _fetch_with_retry(self, file_id: str):
        attempts = max(1, config.DRIVE_MAX_RETRIES)
        delay = 0.5
        last_error: Exception | None = None
        for attempt in range(1, attempts + 1):
            try:
                return self.drive.fetch(file_id)
            except DriveRateLimitError as exc:
                last_error = exc
                if attempt == attempts:
                    break
                time.sleep(delay)
                delay *= 2
        raise DriveError(str(last_error) if last_error else "download failed")

    def _find_by_hash(self, digest: str) -> Path | None:
        for entry in self._cache.values():
            if entry.get("sha256") == digest:
                path = Path(entry["path"])
                if path.exists():
                    return path
        return None

    def _compress(self, path: Path) -> int | None:
        """Re-encode to JPEG under target_bytes. Returns the new size."""
        try:
            from PIL import Image

            with Image.open(path) as image:
                image = image.convert("RGB")
                max_side = 1600
                if max(image.size) > max_side:
                    ratio = max_side / max(image.size)
                    image = image.resize(
                        (max(1, int(image.width * ratio)), max(1, int(image.height * ratio)))
                    )
                quality = 82
                for _ in range(6):
                    buffer = io.BytesIO()
                    image.save(buffer, format="JPEG", quality=quality, optimize=True)
                    data = buffer.getvalue()
                    if len(data) <= self.target_bytes:
                        break
                    quality = max(35, quality - 10)
            path.write_bytes(data)
            return len(data)
        except Exception:
            return None

    # ── housekeeping ───────────────────────────────────────────────────────
    def cleanup(self, force: bool = False) -> dict:
        """Delete temp images older than the TTL. Returns a small report."""
        cutoff = wib_now() - timedelta(hours=self.ttl_hours)
        removed = 0
        freed = 0
        if not self.tmp_dir.exists():
            return {"removed": 0, "freed": 0, "kept": 0}

        for path in self.tmp_dir.glob("*"):
            if not path.is_file():
                continue
            modified = datetime.fromtimestamp(path.stat().st_mtime)
            if force or modified < cutoff.replace(tzinfo=None):
                size = path.stat().st_size
                try:
                    path.unlink()
                    removed += 1
                    freed += size
                except OSError:
                    continue

        live_paths = {str(path) for path in self.tmp_dir.glob("*")}
        self._cache = {
            key: value
            for key, value in self._cache.items()
            if value.get("path") in live_paths
        }
        self._save_cache()

        kept = len(self._cache)
        return {"removed": removed, "freed": freed, "kept": kept}

    def stats(self) -> dict:
        files = [path for path in self.tmp_dir.glob("*") if path.is_file()]
        total = sum(path.stat().st_size for path in files)
        return {
            "files": len(files),
            "size_bytes": total,
            "size_human": human_size(total),
            "cache_entries": len(self._cache),
            "ttl_hours": self.ttl_hours,
        }
