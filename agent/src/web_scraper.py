"""Web browsing, search, and page scraping component.

Uses:
- DuckDuckGo Search for web discovery
- requests + BeautifulSoup (bs4) for safe HTML fetching and OpenGraph extraction
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import re
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup
from duckduckgo_search import DDGS

from . import config

logger = logging.getLogger(__name__)

# Standard browser User-Agent to avoid immediate blocking
DEFAULT_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Permitted social / validation domains
PERMITTED_DOMAINS = {
    "instagram.com",
    "www.instagram.com",
    "instagr.am",
    "threads.net",
    "www.threads.net",
    "docs.google.com",
    "drive.google.com",
}


def is_safe_url(url: str, allow_all_public: bool = True) -> bool:
    """Validate that a URL uses http(s) and does not target internal/private networks."""
    try:
        parsed = urllib.parse.urlparse(url.strip())
        if parsed.scheme not in {"http", "https"}:
            return False
        hostname = (parsed.hostname or "").lower()
        if not hostname or hostname in {"localhost", "127.0.0.1", "0.0.0.0", "::1"}:
            return False

        # Reject private IP ranges
        try:
            ip = ipaddress.ip_address(hostname)
            if ip.is_private or ip.is_loopback or ip.is_link_local:
                return False
        except ValueError:
            pass

        if not allow_all_public:
            return any(
                hostname == domain or hostname.endswith("." + domain)
                for domain in PERMITTED_DOMAINS
            )
        return True
    except Exception:
        return False


def duckduckgo_search(query: str, max_results: int = 5) -> list[dict[str, str]]:
    """Search the web using DuckDuckGo.

    Returns a list of dicts with keys 'title', 'href', 'body'.
    """
    clean_query = (query or "").strip()
    if not clean_query:
        return []

    results: list[dict[str, str]] = []
    try:
        with DDGS() as ddgs:
            for item in ddgs.text(clean_query, max_results=max_results):
                results.append(
                    {
                        "title": str(item.get("title") or ""),
                        "href": str(item.get("href") or ""),
                        "body": str(item.get("body") or ""),
                    }
                )
    except Exception as exc:
        logger.warning("DuckDuckGo search error: %s", exc)
    return results


def scrape_page(url: str, timeout: int = 10, max_chars: int = 4000) -> dict[str, Any]:
    """Fetch and extract clean text, title, and metadata from a web page."""
    if not is_safe_url(url):
        return {"error": "URL tidak diizinkan atau tidak aman (SSRF guard)."}

    headers = {"User-Agent": DEFAULT_USER_AGENT}
    try:
        resp = requests.get(url, headers=headers, timeout=timeout)
        resp.raise_for_status()
    except Exception as exc:
        return {"error": f"Gagal mengambil halaman web: {exc}"}

    soup = BeautifulSoup(resp.text, "html.parser")

    # Remove script and style elements
    for script_or_style in soup(["script", "style", "noscript", "svg"]):
        script_or_style.decompose()

    title = soup.title.string.strip() if (soup.title and soup.title.string) else ""
    meta_desc = ""
    desc_tag = soup.find("meta", attrs={"name": "description"}) or soup.find(
        "meta", attrs={"property": "og:description"}
    )
    if desc_tag and hasattr(desc_tag, "get"):
        meta_desc = str(desc_tag.get("content") or "").strip()

    text = " ".join(soup.stripped_strings)
    if len(text) > max_chars:
        text = text[:max_chars] + " … [dipotong]"

    return {
        "url": url,
        "title": title,
        "description": meta_desc,
        "content": text,
    }


@dataclass
class ScrapedPost:
    url: str
    author: str
    caption: str
    image_url: str | None
    local_image_path: Path | None
    raw_title: str
    success: bool
    error: str = ""


def extract_instagram_post(url: str, image_dir: Path | None = None) -> ScrapedPost:
    """Scrape Instagram post metadata and OpenGraph preview image directly."""
    if not is_safe_url(url):
        return ScrapedPost(
            url=url,
            author="",
            caption="",
            image_url=None,
            local_image_path=None,
            raw_title="",
            success=False,
            error="URL Instagram tidak valid atau tidak aman.",
        )

    headers = {"User-Agent": DEFAULT_USER_AGENT}
    try:
        resp = requests.get(url, headers=headers, timeout=12)
        resp.raise_for_status()
    except Exception as exc:
        return ScrapedPost(
            url=url,
            author="",
            caption="",
            image_url=None,
            local_image_path=None,
            raw_title="",
            success=False,
            error=f"Gagal memuat URL Instagram: {exc}",
        )

    soup = BeautifulSoup(resp.text, "html.parser")

    og_title = ""
    title_tag = soup.find("meta", attrs={"property": "og:title"})
    if title_tag and hasattr(title_tag, "get"):
        og_title = str(title_tag.get("content") or "").strip()

    og_desc = ""
    desc_tag = soup.find("meta", attrs={"property": "og:description"}) or soup.find(
        "meta", attrs={"name": "description"}
    )
    if desc_tag and hasattr(desc_tag, "get"):
        og_desc = str(desc_tag.get("content") or "").strip()

    og_image = ""
    img_tag = soup.find("meta", attrs={"property": "og:image"})
    if img_tag and hasattr(img_tag, "get"):
        og_image = str(img_tag.get("content") or "").strip()

    # Extract author and clean caption from og:description
    # Format typically: "1,200 likes, 45 comments - username on date: 'caption'"
    author = ""
    caption = og_desc
    match = re.search(r"-\s*([a-zA-Z0-9_.]+)\s+on\s+[^:]*:\s*[\"']?(.*)[\"']?", og_desc)
    if match:
        author = match.group(1).strip()
        caption = match.group(2).strip()
    elif not author and og_title:
        author = og_title.split("on Instagram")[0].strip()

    # Download preview image if available
    local_path: Path | None = None
    target_dir = image_dir or config.IMAGE_TMP_DIR
    if og_image and is_safe_url(og_image):
        local_path = download_image(og_image, target_dir)

    return ScrapedPost(
        url=url,
        author=author,
        caption=caption,
        image_url=og_image or None,
        local_image_path=local_path,
        raw_title=og_title,
        success=True,
    )


def download_image(image_url: str, dest_dir: Path) -> Path | None:
    """Download image to dest_dir with SHA-256 filename deduplication."""
    if not is_safe_url(image_url):
        return None
    dest_dir.mkdir(parents=True, exist_ok=True)
    try:
        resp = requests.get(
            image_url,
            headers={"User-Agent": DEFAULT_USER_AGENT},
            timeout=10,
            stream=True,
        )
        resp.raise_for_status()
        content = resp.content
        if not content or len(content) < 100:
            return None

        sha = hashlib.sha256(content).hexdigest()
        dest_file = dest_dir / f"web_{sha[:16]}.jpg"
        if not dest_file.exists():
            with open(dest_file, "wb") as f:
                f.write(content)
        return dest_file
    except Exception as exc:
        logger.warning("Failed to download image from %s: %s", image_url, exc)
        return None
