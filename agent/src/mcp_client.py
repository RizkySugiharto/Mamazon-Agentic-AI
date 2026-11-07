"""Google Sheets MCP client communicating with freema/mcp-gsheets over stdio JSON-RPC.

Uses service account JSON at agent/config/service_account.json to read and write
Google Sheets in realtime.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from threading import Lock, Thread
from typing import Any

from . import config

logger = logging.getLogger(__name__)


class GSheetsMCPClient:
    """Stdio JSON-RPC 2.0 client for freema/mcp-gsheets MCP server."""

    def __init__(
        self,
        service_account_path: Path | str | None = None,
        project_id: str = "mamazon-app",
    ) -> None:
        self.sa_path = Path(service_account_path or config.SERVICE_ACCOUNT_PATH).resolve()
        self.project_id = project_id
        self._proc: subprocess.Popen[str] | None = None
        self._init_lock = Lock()
        self._comm_lock = Lock()
        self._msg_id = 1
        self._pending: dict[int, dict[str, Any]] = {}
        self._reader_thread: Thread | None = None
        self._initialized = False

    def _spawn(self) -> subprocess.Popen[str]:
        env = os.environ.copy()
        env["GOOGLE_APPLICATION_CREDENTIALS"] = str(self.sa_path)
        env["GOOGLE_PROJECT_ID"] = self.project_id

        # Prefer built dist/index.js inside agent/mcp-server if available
        local_dist = config.PROJECT_ROOT / "mcp-server" / "dist" / "index.js"
        if local_dist.exists():
            cmd = ["node", str(local_dist)]
            shell = False
        else:
            npx = shutil.which("npx") or "npx"
            cmd = [npx, "-y", "mcp-gsheets@latest"]
            shell = os.name == "nt"

        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            shell=shell,
            env=env,
            bufsize=1,
        )
        return proc

    def _ensure_started(self) -> None:
        with self._init_lock:
            if self._proc is not None and self._proc.poll() is None and self._initialized:
                return

            if self._proc is None or self._proc.poll() is not None:
                self._proc = self._spawn()
                with self._comm_lock:
                    self._pending.clear()
                self._initialized = False

                # Start background reader thread
                self._reader_thread = Thread(target=self._reader_loop, daemon=True)
                self._reader_thread.start()

                # Perform MCP initialize handshake
                init_res = self._send_request(
                    "initialize",
                    {
                        "protocolVersion": "2024-11-05",
                        "capabilities": {},
                        "clientInfo": {"name": "mamazon-discord-bot", "version": "1.0.0"},
                    },
                )
                # Send initialized notification
                self._send_notification("notifications/initialized")
                self._initialized = True
                logger.info("Google Sheets MCP server started: %s", init_res)

    def _reader_loop(self) -> None:
        proc = self._proc
        if proc is None or proc.stdout is None:
            return
        try:
            for line in proc.stdout:
                line_str = line.strip()
                if not line_str:
                    continue
                try:
                    data = json.loads(line_str)
                    req_id = data.get("id")
                    if req_id is not None:
                        with self._comm_lock:
                            self._pending[req_id] = data
                except Exception:
                    pass
        except Exception:
            pass

    def _send_notification(self, method: str, params: dict[str, Any] | None = None) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None:
            return
        payload = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            payload["params"] = params
        with self._comm_lock:
            proc.stdin.write(json.dumps(payload) + "\n")
            proc.stdin.flush()

    def _send_request(self, method: str, params: dict[str, Any], timeout: float = 20.0) -> dict[str, Any]:
        with self._comm_lock:
            req_id = self._msg_id
            self._msg_id += 1

            payload = {"jsonrpc": "2.0", "id": req_id, "method": method, "params": params}

            proc = self._proc
            if proc is None or proc.stdin is None:
                raise RuntimeError("MCP process stdin not available.")

            proc.stdin.write(json.dumps(payload) + "\n")
            proc.stdin.flush()

        # Wait for response without holding write lock
        import time

        start = time.monotonic()
        while time.monotonic() - start < timeout:
            with self._comm_lock:
                if req_id in self._pending:
                    return self._pending.pop(req_id)
            time.sleep(0.02)

        raise TimeoutError(f"MCP request '{method}' timed out after {timeout}s")

    def call_tool(self, name: str, arguments: dict[str, Any], timeout: float = 25.0) -> Any:
        """Invoke a tool exposed by mcp-gsheets."""
        self._ensure_started()
        resp = self._send_request(
            "tools/call",
            {"name": name, "arguments": arguments},
            timeout=timeout,
        )
        if "error" in resp:
            raise RuntimeError(f"MCP Error: {resp['error']}")
        result = resp.get("result", {})
        content = result.get("content", [])
        if isinstance(content, list) and content:
            texts = [c.get("text", "") for c in content if isinstance(c, dict) and "text" in c]
            return "\n".join(texts)
        return result

    # ── High level spreadsheet methods ──────────────────────────────────────
    def read_range(self, spreadsheet_id: str, range_a1: str) -> str:
        clean_id = (spreadsheet_id or config.GOOGLE_SPREADSHEET_ID or "").strip()
        # Extract spreadsheet id if a full URL was provided
        if "/d/" in clean_id:
            clean_id = clean_id.split("/d/")[1].split("/")[0]

        args = {"spreadsheetId": clean_id, "range": range_a1}
        try:
            return str(self.call_tool("sheets_get_values", args))
        except Exception as exc:
            return f"Gagal membaca sheet via MCP: {exc}"

    def get_metadata(self, spreadsheet_id: str = "") -> str:
        clean_id = (spreadsheet_id or config.GOOGLE_SPREADSHEET_ID or "").strip()
        if "/d/" in clean_id:
            clean_id = clean_id.split("/d/")[1].split("/")[0]

        args = {"spreadsheetId": clean_id}
        try:
            return str(self.call_tool("sheets_get_metadata", args))
        except Exception as exc:
            return f"Gagal membaca metadata sheet via MCP: {exc}"

    def update_range(self, spreadsheet_id: str, range_a1: str, values: list[list[Any]]) -> str:
        clean_id = (spreadsheet_id or config.GOOGLE_SPREADSHEET_ID or "").strip()
        if "/d/" in clean_id:
            clean_id = clean_id.split("/d/")[1].split("/")[0]

        args = {"spreadsheetId": clean_id, "range": range_a1, "values": values}
        try:
            return str(self.call_tool("sheets_update_values", args))
        except Exception as exc:
            return f"Gagal menulis sheet via MCP: {exc}"

    def close(self) -> None:
        with self._init_lock:
            if self._proc is not None:
                try:
                    self._proc.terminate()
                    self._proc.wait(timeout=2.0)
                except Exception:
                    try:
                        self._proc.kill()
                    except Exception:
                        pass
                self._proc = None


# Global singleton client
_GLOBAL_MCP_CLIENT: GSheetsMCPClient | None = None


def get_mcp_client() -> GSheetsMCPClient:
    global _GLOBAL_MCP_CLIENT
    if _GLOBAL_MCP_CLIENT is None:
        _GLOBAL_MCP_CLIENT = GSheetsMCPClient()
    return _GLOBAL_MCP_CLIENT
