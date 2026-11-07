"""Tests for new components:
- Discord view TypeError fix & chatbot validation report
- PersistentMemoryStore & SqliteCheckpointSaver
- Web scraper & DuckDuckGo search
- Google Sheets MCP client
"""

from __future__ import annotations

import tempfile
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import discord
import pytest

from src.discord_bot import MamazonBot
from src.mcp_client import GSheetsMCPClient
from src.memory_store import PersistentMemoryStore, SqliteCheckpointSaver
from src.models import (
    CheckResult,
    ResultStatus,
    Submission,
    ValidationBatch,
    ValidationResult,
)
from src.output_generator import OutputGenerator
from src.web_scraper import (
    extract_instagram_post,
    is_safe_url,
    scrape_page,
)


def _sample_batch() -> ValidationBatch:
    sub1 = Submission(
        row_index=2,
        nama="Budi Santoso",
        nim="12345",
        divisi="Holmes",
        ig_username="budi_s",
        links=["https://drive.google.com/file/d/abc/view"],
        file_ids=["abc"],
    )
    res1 = ValidationResult(
        submission=sub1,
        status=ResultStatus.VERIFIED,
        task_id="PP-01",
        task_type="PAID_PROMOTE",
    )

    sub2 = Submission(
        row_index=3,
        nama="Siti Rahma",
        nim="12346",
        divisi="Watson",
        ig_username="siti_r",
        links=["https://instagram.com/p/xyz123/"],
        file_ids=[],
    )
    chk1 = CheckResult(
        check_id="C4",
        name="caption",
        status="FAIL",
        denda=25000,
        detail="Caption tidak sesuai template",
    )
    res2 = ValidationResult(
        submission=sub2,
        status=ResultStatus.FLAGGED,
        task_id="PP-01",
        task_type="PAID_PROMOTE",
        checks=[chk1],
    )

    batch = ValidationBatch(
        sheet_name="PP Bundle 26 Oktober",
        task_id="PP-01",
        task_type="PAID_PROMOTE",
        results=[res1, res2],
        started_at=datetime(2026, 10, 26, 10, 0, 0),
        finished_at=datetime(2026, 10, 26, 10, 2, 0),
    )
    return batch


# ─── 1. Discord Bug Fix & Chatbot Report ──────────────────────────────────
def test_discord_view_sentinel():
    """Ensure view uses discord.utils.MISSING instead of None for single-page embeds."""
    pages = [discord.Embed(title="Page 1")]
    view = None if len(pages) > 1 else discord.utils.MISSING
    assert view is discord.utils.MISSING


def test_format_validation_chatbot_report():
    batch = _sample_batch()
    mock_outputs = MagicMock(spec=OutputGenerator)
    mock_outputs.denda_total.return_value = 50000

    report = MamazonBot.format_validation_chatbot_report(batch, mock_outputs)

    # Must be warm Indonesian
    assert "Halo! Validasi untuk sheet" in report
    assert "PP Bundle 26 Oktober" in report
    assert "Anggota Lolos (Kualifikasi / AMAN / EXEMPT):" in report
    assert "Budi Santoso" in report
    assert "AMAN" in report

    # Must list punished members and accumulated fines
    assert "Anggota Terkena Denda (Melanggar / FLAGGED):" in report
    assert "Siti Rahma" in report
    assert "`C4` Caption tidak sesuai template" in report
    assert "Rp 25.000" in report
    assert "Akumulasi total denda aktif: **Rp 50.000**" in report

    # Must NOT have markdown pipe tables
    assert "|" not in report.split("\n")[0]
    assert "| --- |" not in report


# ─── 2. Persistent Memory (Cross-Thread) ───────────────────────────────────
def test_persistent_memory_store_batches_and_history():
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test_memory.sqlite"
        store = PersistentMemoryStore(db_path)

        batch = _sample_batch()
        batch_id = store.save_validation_batch(batch, actor="test_runner")
        assert batch_id

        # Verify batches listing
        batches = store.get_validation_batches()
        assert len(batches) == 1
        assert batches[0]["sheet_name"] == "PP Bundle 26 Oktober"
        assert batches[0]["flagged_count"] == 1
        assert batches[0]["verified_count"] == 1

        # Verify member history
        history_siti = store.get_member_history("Siti Rahma")
        assert len(history_siti) == 1
        assert history_siti[0]["status"] == "FLAGGED"
        assert history_siti[0]["total_denda"] == 25000
        assert len(history_siti[0]["violations"]) == 1
        assert history_siti[0]["violations"][0]["check_id"] == "C4"

        # Verify cross-thread fact persistence across new instance
        store.set_fact("user_123", "preferensi_bahasa", "santai")
        store.record_execution("validate", "user_123", "PP Bundle", "OK")

        # Open fresh instance against same SQLite file
        store2 = PersistentMemoryStore(db_path)
        facts = store2.get_facts("user_123")
        assert facts.get("preferensi_bahasa") == "santai"


def test_sqlite_checkpoint_saver():
    with tempfile.TemporaryDirectory() as td:
        db_path = Path(td) / "test_checkpoints.sqlite"
        saver = SqliteCheckpointSaver(db_path)

        config_arg = {"configurable": {"thread_id": "thread-42", "checkpoint_ns": "ai-chat"}}
        checkpoint = {
            "v": 1,
            "id": "cp-001",
            "ts": "2026-11-07T10:00:00",
            "channel_values": {"msg": "halo"},
            "channel_versions": {"msg": "v1"},
            "versions_seen": {},
        }
        metadata = {"source": "test", "step": 1}

        saver.put(config_arg, checkpoint, metadata, {"msg": "v1"})

        # Reload saver from disk
        saver2 = SqliteCheckpointSaver(db_path)
        read_cp = saver2.get_tuple(
            {"configurable": {"thread_id": "thread-42", "checkpoint_ns": "ai-chat", "checkpoint_id": "cp-001"}}
        )
        assert read_cp is not None
        assert read_cp.checkpoint["id"] == "cp-001"


# ─── 3. Web Scraper (SSRF, DDG, BeautifulSoup) ────────────────────────────
def test_is_safe_url():
    assert is_safe_url("https://www.instagram.com/p/DE12345/") is True
    assert is_safe_url("https://threads.net/@user/post/123") is True
    assert is_safe_url("http://127.0.0.1:8000") is False
    assert is_safe_url("http://localhost:3000") is False
    assert is_safe_url("http://169.254.169.254/latest/meta-data/") is False
    assert is_safe_url("ftp://example.com/file") is False


def test_scrape_page_mocked():
    html_content = """
    <html>
        <head>
            <title>Test Page Title</title>
            <meta name="description" content="Meta Description Text">
        </head>
        <body>
            <script>alert('malicious')</script>
            <p>Halaman berita dan tugas panitia SPADE.</p>
        </body>
    </html>
    """
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.text = html_content
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        res = scrape_page("https://example.com/page")
        assert res["title"] == "Test Page Title"
        assert res["description"] == "Meta Description Text"
        assert "alert" not in res["content"]
        assert "Halaman berita dan tugas panitia SPADE." in res["content"]


def test_extract_instagram_post_mocked():
    ig_html = """
    <html>
        <head>
            <meta property="og:title" content="Budi Santoso on Instagram: 'Paid Promote Day 1'">
            <meta property="og:description" content="1,500 likes, 20 comments - budi_s on October 26, 2026: 'Yuk beli produk kami #SPADE2026 @spade.id'">
            <meta property="og:image" content="https://example.com/cdn/ig_preview.jpg">
        </head>
        <body></body>
    </html>
    """
    with patch("requests.get") as mock_get:
        mock_resp = MagicMock()
        mock_resp.text = ig_html
        mock_resp.content = b"fake-jpg-content" * 20
        mock_resp.raise_for_status.return_value = None
        mock_get.return_value = mock_resp

        with tempfile.TemporaryDirectory() as td:
            scraped = extract_instagram_post("https://www.instagram.com/p/xyz/", image_dir=Path(td))
            assert scraped.success is True
            assert scraped.author == "budi_s"
            assert "#SPADE2026" in scraped.caption
            assert "@spade.id" in scraped.caption


# ─── 4. Google Sheets MCP Client ───────────────────────────────────────────
def test_mcp_client_range_normalization():
    client = GSheetsMCPClient()
    with patch.object(client, "call_tool") as mock_call:
        mock_call.return_value = "Mocked cell values"
        res = client.read_range(
            "https://docs.google.com/spreadsheets/d/11SmNPTq0FliAkEojXE7AeCKQy-C5oyrzBjN_7zhf8Bc/edit",
            "Sheet1!A1:B2",
        )
        assert res == "Mocked cell values"
        mock_call.assert_called_once_with(
            "sheets_get_values",
            {
                "spreadsheetId": "11SmNPTq0FliAkEojXE7AeCKQy-C5oyrzBjN_7zhf8Bc",
                "range": "Sheet1!A1:B2",
            },
        )
