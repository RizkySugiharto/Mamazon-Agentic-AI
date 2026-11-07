"""Centralized timezone-aware logging configuration for Mamazon Discord Bot.

Features:
- Date-divided directory structure in WIB (UTC+7, Asia/Jakarta):
  agent/logs/{YYYY-MM-DD}/
- bot.log: Full system debug logs with timestamps in WIB.
- ai_chat.jsonl: Detailed API requests, responses, tool calls, token counts, and latency.
- Console output: Clean and non-verbose (INFO level, suppresses httpx debug spam).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import threading
from typing import Any

try:
    from zoneinfo import ZoneInfo

    WIB_TZ: Any = ZoneInfo("Asia/Jakarta")
except Exception:
    WIB_TZ = timezone(timedelta(hours=7), name="WIB")


def get_wib_now() -> datetime:
    """Return current timezone-aware datetime in WIB (UTC+7)."""
    return datetime.now(WIB_TZ)


def get_date_log_dir(base_dir: Path | None = None) -> Path:
    """Return and create folder agent/logs/{YYYY-MM-DD}/ for current WIB date."""
    from . import config

    root = Path(base_dir or config.LOGS_DIR)
    date_str = get_wib_now().strftime("%Y-%m-%d")
    folder = root / date_str
    folder.mkdir(parents=True, exist_ok=True)
    return folder


class WIBFormatter(logging.Formatter):
    """Logging formatter displaying timestamps in WIB (Asia/Jakarta)."""

    def formatTime(self, record: logging.LogRecord, datefmt: str | None = None) -> str:
        dt = datetime.fromtimestamp(record.created, tz=WIB_TZ)
        if datefmt:
            return dt.strftime(datefmt)
        return dt.strftime("%Y-%m-%d %H:%M:%S")


class DailyDateFolderHandler(logging.Handler):
    """Thread-safe logging handler routing logs into agent/logs/{YYYY-MM-DD}/bot.log."""

    def __init__(self, base_dir: Path | None = None) -> None:
        super().__init__()
        self.base_dir = base_dir
        self._lock = threading.RLock()
        self._current_date: str | None = None
        self._stream: Any = None

    def _get_stream(self) -> Any:
        today = get_wib_now().strftime("%Y-%m-%d")
        if self._stream is not None and self._current_date == today:
            return self._stream

        if self._stream is not None:
            try:
                self._stream.flush()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

        log_dir = get_date_log_dir(self.base_dir)
        log_file = log_dir / "bot.log"
        self._stream = open(log_file, "a", encoding="utf-8")
        self._current_date = today
        return self._stream

    def emit(self, record: logging.LogRecord) -> None:
        try:
            with self._lock:
                stream = self._get_stream()
                msg = self.format(record)
                stream.write(msg + "\n")
                stream.flush()
        except Exception:
            self.handleError(record)

    def close(self) -> None:
        with self._lock:
            if self._stream is not None:
                try:
                    self._stream.flush()
                    self._stream.close()
                except Exception:
                    pass
                self._stream = None
            super().close()


class ConsoleFilter(logging.Filter):
    """Filter out noisy debug/info logs from httpx and httpcore on CLI console."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name.startswith(("httpx", "httpcore", "sentence_transformers")):
            return record.levelno >= logging.WARNING
        return True


_AI_CHAT_LOCK = threading.RLock()


def log_ai_interaction(event_type: str, data: dict[str, Any], base_dir: Path | None = None) -> None:
    """Append structured JSON line to agent/logs/{YYYY-MM-DD}/ai_chat.jsonl."""
    try:
        log_dir = get_date_log_dir(base_dir)
        jsonl_file = log_dir / "ai_chat.jsonl"
        record = {
            "timestamp": get_wib_now().isoformat(),
            "event": event_type,
            **data,
        }
        line = json.dumps(record, ensure_ascii=False, default=str)
        with _AI_CHAT_LOCK:
            with open(jsonl_file, "a", encoding="utf-8") as f:
                f.write(line + "\n")
    except Exception as exc:
        logging.getLogger(__name__).warning("Failed to write to ai_chat.jsonl: %s", exc)


try:
    from langchain_core.callbacks import BaseCallbackHandler
    from langchain_core.outputs import LLMResult

    class AIChatLoggingCallbackHandler(BaseCallbackHandler):
        """LangChain callback streaming detailed prompt, completion, token usage, and latency to ai_chat.jsonl."""

        def __init__(self, base_dir: Path | None = None) -> None:
            super().__init__()
            self.base_dir = base_dir
            self._start_times: dict[str, float] = {}

        def on_chat_model_start(
            self,
            serialized: dict[str, Any],
            messages: list[list[Any]],
            *,
            run_id: Any = None,
            parent_run_id: Any = None,
            tags: list[str] | None = None,
            metadata: dict[str, Any] | None = None,
            **kwargs: Any,
        ) -> None:
            import time

            rid = str(run_id or "")
            self._start_times[rid] = time.perf_counter()

            serialized_messages = []
            for batch in messages:
                for msg in batch:
                    item: dict[str, Any] = {
                        "type": getattr(msg, "type", type(msg).__name__),
                        "content": getattr(msg, "content", ""),
                    }
                    if hasattr(msg, "tool_calls") and msg.tool_calls:
                        item["tool_calls"] = msg.tool_calls
                    if hasattr(msg, "tool_call_id") and msg.tool_call_id:
                        item["tool_call_id"] = msg.tool_call_id
                    serialized_messages.append(item)

            log_ai_interaction(
                "api_request",
                {
                    "run_id": rid,
                    "model_info": serialized,
                    "metadata": metadata or {},
                    "messages": serialized_messages,
                    "invocation_params": kwargs.get("invocation_params", {}),
                },
                base_dir=self.base_dir,
            )

        def on_llm_end(
            self,
            response: LLMResult,
            *,
            run_id: Any = None,
            parent_run_id: Any = None,
            **kwargs: Any,
        ) -> None:
            import time

            rid = str(run_id or "")
            start = self._start_times.pop(rid, None)
            latency_ms = round((time.perf_counter() - start) * 1000, 2) if start else None

            generations = []
            for batch in response.generations:
                for gen in batch:
                    item: dict[str, Any] = {"text": gen.text}
                    msg = getattr(gen, "message", None)
                    if msg:
                        item["content"] = getattr(msg, "content", "")
                        item["tool_calls"] = getattr(msg, "tool_calls", [])
                    generations.append(item)

            token_usage = response.llm_output.get("token_usage", {}) if response.llm_output else {}

            log_ai_interaction(
                "api_response",
                {
                    "run_id": rid,
                    "latency_ms": latency_ms,
                    "generations": generations,
                    "token_usage": token_usage,
                    "llm_output": response.llm_output,
                },
                base_dir=self.base_dir,
            )

        def on_llm_error(
            self,
            error: BaseException,
            *,
            run_id: Any = None,
            parent_run_id: Any = None,
            **kwargs: Any,
        ) -> None:
            import time

            rid = str(run_id or "")
            start = self._start_times.pop(rid, None)
            latency_ms = round((time.perf_counter() - start) * 1000, 2) if start else None

            log_ai_interaction(
                "api_error",
                {
                    "run_id": rid,
                    "latency_ms": latency_ms,
                    "error": str(error),
                    "error_type": type(error).__name__,
                },
                base_dir=self.base_dir,
            )

        def on_tool_start(
            self,
            serialized: dict[str, Any],
            input_str: str,
            *,
            run_id: Any = None,
            parent_run_id: Any = None,
            **kwargs: Any,
        ) -> None:
            log_ai_interaction(
                "tool_start",
                {
                    "run_id": str(run_id or ""),
                    "tool": serialized.get("name"),
                    "input": input_str,
                },
                base_dir=self.base_dir,
            )

        def on_tool_end(
            self,
            output: Any,
            *,
            run_id: Any = None,
            parent_run_id: Any = None,
            **kwargs: Any,
        ) -> None:
            log_ai_interaction(
                "tool_end",
                {
                    "run_id": str(run_id or ""),
                    "output": str(output)[:3000],
                },
                base_dir=self.base_dir,
            )

        def on_tool_error(
            self,
            error: BaseException,
            *,
            run_id: Any = None,
            parent_run_id: Any = None,
            **kwargs: Any,
        ) -> None:
            log_ai_interaction(
                "tool_error",
                {
                    "run_id": str(run_id or ""),
                    "error": str(error),
                },
                base_dir=self.base_dir,
            )

except ImportError:
    AIChatLoggingCallbackHandler = None  # type: ignore[assignment,misc]


def setup_logging(
    console_level: int = logging.INFO,
    file_level: int = logging.DEBUG,
    base_dir: Path | None = None,
) -> None:
    """Configure unified logging: non-verbose console + verbose date-divided file logs."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Remove existing handlers to avoid duplicate prints
    for h in list(root.handlers):
        root.removeHandler(h)

    # 1. Console Handler (terse, clean, INFO)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(console_level)
    console_formatter = WIBFormatter(
        "[%(asctime)s WIB] [%(levelname)-7s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )
    console_handler.setFormatter(console_formatter)
    console_handler.addFilter(ConsoleFilter())
    root.addHandler(console_handler)

    # 2. File Handler (verbose, DEBUG, date-divided folder)
    file_handler = DailyDateFolderHandler(base_dir=base_dir)
    file_handler.setLevel(file_level)
    file_formatter = WIBFormatter(
        "[%(asctime)s.%(msecs)03d WIB] [%(levelname)-8s] [%(name)s:%(lineno)d] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(file_formatter)
    root.addHandler(file_handler)

    logging.getLogger("discord").setLevel(logging.INFO)
    logging.getLogger("httpx").setLevel(logging.DEBUG)  # Full debug to file, filtered on console
