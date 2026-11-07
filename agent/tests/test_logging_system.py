from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.logging_config import (
    AIChatLoggingCallbackHandler,
    ConsoleFilter,
    DailyDateFolderHandler,
    WIBFormatter,
    get_date_log_dir,
    get_wib_now,
    log_ai_interaction,
    setup_logging,
)


def test_wib_now_and_date_dir(tmp_path: Path):
    now = get_wib_now()
    assert now.tzinfo is not None
    # WIB offset is +07:00
    offset = now.utcoffset()
    assert offset is not None
    assert offset.total_seconds() == 7 * 3600

    date_dir = get_date_log_dir(base_dir=tmp_path)
    today_str = now.strftime("%Y-%m-%d")
    assert date_dir == tmp_path / today_str
    assert date_dir.is_dir()


def test_daily_date_folder_handler(tmp_path: Path):
    handler = DailyDateFolderHandler(base_dir=tmp_path)
    formatter = WIBFormatter(
        "[%(asctime)s WIB] [%(levelname)s] %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    handler.setFormatter(formatter)

    test_logger = logging.getLogger("test_daily_folder")
    test_logger.setLevel(logging.DEBUG)
    test_logger.addHandler(handler)

    test_logger.info("Hello WIB log line")
    handler.close()

    today_str = get_wib_now().strftime("%Y-%m-%d")
    log_file = tmp_path / today_str / "bot.log"
    assert log_file.exists()
    content = log_file.read_text(encoding="utf-8")
    assert "Hello WIB log line" in content
    assert "WIB" in content


def test_log_ai_interaction(tmp_path: Path):
    log_ai_interaction(
        "api_request",
        {
            "run_id": "test_run_123",
            "model": "mamazon",
            "messages": [{"role": "user", "content": "halo"}],
        },
        base_dir=tmp_path,
    )

    today_str = get_wib_now().strftime("%Y-%m-%d")
    jsonl_file = tmp_path / today_str / "ai_chat.jsonl"
    assert jsonl_file.exists()

    lines = [json.loads(line) for line in jsonl_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 1
    assert lines[0]["event"] == "api_request"
    assert lines[0]["run_id"] == "test_run_123"
    assert lines[0]["model"] == "mamazon"


def test_console_filter_suppresses_httpx_info():
    c_filter = ConsoleFilter()

    # httpx INFO or DEBUG should be blocked
    rec_httpx_info = logging.LogRecord(
        name="httpx",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="HTTP Request: POST",
        args=(),
        exc_info=None,
    )
    assert not c_filter.filter(rec_httpx_info)

    rec_httpx_debug = logging.LogRecord(
        name="httpx",
        level=logging.DEBUG,
        pathname="",
        lineno=0,
        msg="HTTP Request debug",
        args=(),
        exc_info=None,
    )
    assert not c_filter.filter(rec_httpx_debug)

    # httpx WARNING should pass
    rec_httpx_warn = logging.LogRecord(
        name="httpx",
        level=logging.WARNING,
        pathname="",
        lineno=0,
        msg="HTTP connection timeout warning",
        args=(),
        exc_info=None,
    )
    assert c_filter.filter(rec_httpx_warn)

    # Other loggers INFO should pass
    rec_bot_info = logging.LogRecord(
        name="discord.client",
        level=logging.INFO,
        pathname="",
        lineno=0,
        msg="connected",
        args=(),
        exc_info=None,
    )
    assert c_filter.filter(rec_bot_info)


def test_ai_chat_logging_callback_handler(tmp_path: Path):
    handler = AIChatLoggingCallbackHandler(base_dir=tmp_path)
    msg = SimpleNamespace(type="human", content="yg divisi fletch aja", tool_calls=[], tool_call_id=None)

    handler.on_chat_model_start(
        serialized={"name": "ChatOpenAI"},
        messages=[[msg]],
        run_id="run_999",
    )

    gen = SimpleNamespace(text="Rencana validasi disiapkan.", message=SimpleNamespace(content="Rencana validasi disiapkan.", tool_calls=[]))
    llm_result = SimpleNamespace(
        generations=[[gen]],
        llm_output={"token_usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}},
    )

    handler.on_llm_end(llm_result, run_id="run_999")

    today_str = get_wib_now().strftime("%Y-%m-%d")
    jsonl_file = tmp_path / today_str / "ai_chat.jsonl"
    assert jsonl_file.exists()

    lines = [json.loads(line) for line in jsonl_file.read_text(encoding="utf-8").splitlines() if line.strip()]
    assert len(lines) == 2
    assert lines[0]["event"] == "api_request"
    assert lines[0]["run_id"] == "run_999"
    assert lines[1]["event"] == "api_response"
    assert lines[1]["run_id"] == "run_999"
    assert lines[1]["token_usage"]["total_tokens"] == 120
    assert lines[1]["latency_ms"] is not None
