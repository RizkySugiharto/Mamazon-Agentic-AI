"""Persistent cross-thread memory and LangGraph checkpointer backed by SQLite.

Stores:
- Validation batches and member outcomes across shutdowns
- Audit and action execution history
- Cross-thread user/entity memory
- LangGraph conversation checkpoints surviving bot restarts
"""

from __future__ import annotations

import json
import logging
import sqlite3
from collections import defaultdict
from collections.abc import Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from threading import RLock
from typing import Any, Iterator

from langchain_core.runnables import RunnableConfig
from langgraph.checkpoint.base import (
    ChannelVersions,
    Checkpoint,
    CheckpointMetadata,
    CheckpointTuple,
    get_checkpoint_id,
    get_checkpoint_metadata,
)
from langgraph.checkpoint.memory import InMemorySaver, WRITES_IDX_MAP

from . import config

logger = logging.getLogger(__name__)


def _now_iso() -> str:
    return datetime.now().isoformat()


class PersistentMemoryStore:
    """Thread-safe SQLite store for cross-thread data and validation history."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path or (config.OUTPUT_DIR / "persistent_memory.sqlite"))
        self._lock = RLock()
        self._init_db()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock:
            conn = sqlite3.connect(str(self.db_path), timeout=30.0)
            conn.row_factory = sqlite3.Row
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise
            finally:
                conn.close()

    def _init_db(self) -> None:
        with self._conn() as conn:
            conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS validation_batches (
                    batch_id TEXT PRIMARY KEY,
                    sheet_name TEXT,
                    task_id TEXT,
                    task_type TEXT,
                    started_at TEXT,
                    finished_at TEXT,
                    total INTEGER,
                    verified_count INTEGER,
                    flagged_count INTEGER,
                    exempt_count INTEGER,
                    total_denda INTEGER,
                    actor TEXT,
                    raw_json TEXT
                );

                CREATE TABLE IF NOT EXISTS member_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    batch_id TEXT,
                    sheet_name TEXT,
                    task_id TEXT,
                    nama TEXT,
                    nim TEXT,
                    divisi TEXT,
                    ig_username TEXT,
                    status TEXT,
                    total_denda INTEGER,
                    violations_json TEXT,
                    notes TEXT,
                    validated_at TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_member_records_nama ON member_records(nama);
                CREATE INDEX IF NOT EXISTS idx_member_records_batch ON member_records(batch_id);

                CREATE TABLE IF NOT EXISTS execution_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT,
                    action TEXT,
                    actor TEXT,
                    target TEXT,
                    result TEXT,
                    rationale TEXT,
                    extra_json TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_exec_history_action ON execution_history(action);

                CREATE TABLE IF NOT EXISTS cross_thread_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id TEXT,
                    thread_id TEXT,
                    key TEXT,
                    value TEXT,
                    updated_at TEXT,
                    UNIQUE(user_id, key)
                );
                CREATE INDEX IF NOT EXISTS idx_cross_thread_user ON cross_thread_memory(user_id);

                CREATE TABLE IF NOT EXISTS langgraph_checkpoints (
                    thread_id TEXT,
                    checkpoint_ns TEXT,
                    checkpoint_id TEXT,
                    parent_id TEXT,
                    checkpoint_type TEXT,
                    checkpoint_blob BLOB,
                    metadata_type TEXT,
                    metadata_blob BLOB,
                    PRIMARY KEY(thread_id, checkpoint_ns, checkpoint_id)
                );

                CREATE TABLE IF NOT EXISTS langgraph_writes (
                    thread_id TEXT,
                    checkpoint_ns TEXT,
                    checkpoint_id TEXT,
                    task_id TEXT,
                    idx INTEGER,
                    channel TEXT,
                    val_type TEXT,
                    val_blob BLOB,
                    task_path TEXT,
                    PRIMARY KEY(thread_id, checkpoint_ns, checkpoint_id, task_id, idx)
                );

                CREATE TABLE IF NOT EXISTS langgraph_blobs (
                    thread_id TEXT,
                    checkpoint_ns TEXT,
                    channel TEXT,
                    version TEXT,
                    blob_type TEXT,
                    blob_data BLOB,
                    PRIMARY KEY(thread_id, checkpoint_ns, channel, version)
                );
                """
            )

    # ── validation batches ──────────────────────────────────────────────────
    def save_validation_batch(self, batch: Any, actor: str = "") -> str:
        """Persist a completed ValidationBatch and all member records."""
        batch_id = f"{batch.sheet_name}:{_now_iso()}"
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO validation_batches
                (batch_id, sheet_name, task_id, task_type, started_at, finished_at,
                 total, verified_count, flagged_count, exempt_count, total_denda, actor, raw_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    getattr(batch, "sheet_name", ""),
                    getattr(batch, "task_id", ""),
                    getattr(batch, "task_type", ""),
                    getattr(batch, "started_at", None).isoformat() if getattr(batch, "started_at", None) else _now_iso(),
                    getattr(batch, "finished_at", None).isoformat() if getattr(batch, "finished_at", None) else _now_iso(),
                    int(getattr(batch, "total", 0)),
                    len(getattr(batch, "verified", [])),
                    len(getattr(batch, "flagged", [])),
                    len(getattr(batch, "exempt", [])),
                    int(getattr(batch, "total_denda", 0)),
                    str(actor or ""),
                    json.dumps(
                        {
                            "skipped_existing": getattr(batch, "skipped_existing", 0),
                            "warnings": getattr(batch, "warnings", []),
                        }
                    ),
                ),
            )

            records = getattr(batch, "results", [])
            for res in records:
                sub = res.submission
                violations = [
                    {
                        "check_id": c.check_id,
                        "name": c.name,
                        "denda": c.denda,
                        "detail": c.detail,
                    }
                    for c in getattr(res, "violations", [])
                ]
                conn.execute(
                    """
                    INSERT INTO member_records
                    (batch_id, sheet_name, task_id, nama, nim, divisi, ig_username,
                     status, total_denda, violations_json, notes, validated_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch_id,
                        getattr(batch, "sheet_name", ""),
                        getattr(res, "task_id", ""),
                        getattr(sub, "nama", ""),
                        getattr(sub, "nim", ""),
                        getattr(sub, "divisi", ""),
                        getattr(sub, "ig_username", ""),
                        str(getattr(res, "status", "")),
                        int(getattr(res, "total_denda", 0)),
                        json.dumps(violations),
                        str(getattr(res, "notes", "")),
                        _now_iso(),
                    ),
                )
        return batch_id

    def get_member_history(self, nama: str) -> list[dict[str, Any]]:
        """Retrieve historical validation results for a committee member."""
        with self._conn() as conn:
            cur = conn.execute(
                """
                SELECT batch_id, sheet_name, task_id, nama, divisi, ig_username,
                       status, total_denda, violations_json, validated_at
                FROM member_records
                WHERE LOWER(TRIM(nama)) = LOWER(TRIM(?))
                ORDER BY id DESC
                """,
                (nama,),
            )
            rows = cur.fetchall()
            out: list[dict[str, Any]] = []
            for r in rows:
                item = dict(r)
                try:
                    item["violations"] = json.loads(item.get("violations_json") or "[]")
                except Exception:
                    item["violations"] = []
                out.append(item)
            return out

    def get_validation_batches(self, limit: int = 10) -> list[dict[str, Any]]:
        with self._conn() as conn:
            cur = conn.execute(
                """
                SELECT batch_id, sheet_name, task_id, task_type, started_at, finished_at,
                       total, verified_count, flagged_count, exempt_count, total_denda, actor
                FROM validation_batches
                ORDER BY finished_at DESC LIMIT ?
                """,
                (limit,),
            )
            return [dict(r) for r in cur.fetchall()]

    # ── execution audit log ────────────────────────────────────────────────
    def record_execution(
        self,
        action: str,
        actor: str,
        target: str,
        result: str,
        rationale: str = "",
        extra: dict[str, Any] | None = None,
    ) -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT INTO execution_history
                (timestamp, action, actor, target, result, rationale, extra_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    _now_iso(),
                    action,
                    actor,
                    target,
                    result,
                    rationale,
                    json.dumps(extra or {}),
                ),
            )

    # ── cross-thread memory ────────────────────────────────────────────────
    def set_fact(self, user_id: str, key: str, value: str, thread_id: str = "") -> None:
        with self._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO cross_thread_memory
                (user_id, thread_id, key, value, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (str(user_id), str(thread_id), str(key).strip().lower(), str(value), _now_iso()),
            )

    def get_facts(self, user_id: str) -> dict[str, str]:
        with self._conn() as conn:
            cur = conn.execute(
                "SELECT key, value FROM cross_thread_memory WHERE user_id = ?",
                (str(user_id),),
            )
            return {row["key"]: row["value"] for row in cur.fetchall()}


class SqliteCheckpointSaver(InMemorySaver):
    """LangGraph checkpointer that persists checkpoints, writes, and blobs to SQLite.

    Survives bot restarts while maintaining in-memory lookup efficiency.
    """

    def __init__(
        self,
        db_path: Path | str | None = None,
        *,
        serde: Any = None,
    ) -> None:
        super().__init__(serde=serde)
        self.store = PersistentMemoryStore(db_path)
        self._load_from_db()

    def _load_from_db(self) -> None:
        """Hydrate in-memory structures from SQLite on startup."""
        with self.store._conn() as conn:
            # Checkpoints
            cur = conn.execute(
                "SELECT thread_id, checkpoint_ns, checkpoint_id, parent_id, "
                "checkpoint_type, checkpoint_blob, metadata_type, metadata_blob "
                "FROM langgraph_checkpoints"
            )
            for r in cur.fetchall():
                t_id = r["thread_id"]
                ns = r["checkpoint_ns"]
                cp_id = r["checkpoint_id"]
                c_tuple = (r["checkpoint_type"], bytes(r["checkpoint_blob"]))
                m_tuple = (r["metadata_type"], bytes(r["metadata_blob"]))
                parent = r["parent_id"]
                self.storage[t_id][ns][cp_id] = (c_tuple, m_tuple, parent)

            # Writes
            cur = conn.execute(
                "SELECT thread_id, checkpoint_ns, checkpoint_id, task_id, idx, "
                "channel, val_type, val_blob, task_path FROM langgraph_writes"
            )
            for r in cur.fetchall():
                outer_key = (r["thread_id"], r["checkpoint_ns"], r["checkpoint_id"])
                inner_key = (r["task_id"], r["idx"])
                val_tuple = (r["val_type"], bytes(r["val_blob"]))
                self.writes[outer_key][inner_key] = (
                    r["task_id"],
                    r["channel"],
                    val_tuple,
                    r["task_path"],
                )

            # Blobs
            cur = conn.execute(
                "SELECT thread_id, checkpoint_ns, channel, version, blob_type, blob_data "
                "FROM langgraph_blobs"
            )
            for r in cur.fetchall():
                b_key = (r["thread_id"], r["checkpoint_ns"], r["channel"], r["version"])
                self.blobs[b_key] = (r["blob_type"], bytes(r["blob_data"]))

    def put(
        self,
        config_arg: RunnableConfig,
        checkpoint: Checkpoint,
        metadata: CheckpointMetadata,
        new_versions: ChannelVersions,
    ) -> RunnableConfig:
        res = super().put(config_arg, checkpoint, metadata, new_versions)
        thread_id = str(config_arg["configurable"]["thread_id"])
        checkpoint_ns = str(config_arg["configurable"].get("checkpoint_ns", ""))
        cp_id = str(checkpoint["id"])
        saved = self.storage[thread_id][checkpoint_ns][cp_id]
        (c_type, c_blob), (m_type, m_blob), parent_id = saved

        with self.store._conn() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO langgraph_checkpoints
                (thread_id, checkpoint_ns, checkpoint_id, parent_id,
                 checkpoint_type, checkpoint_blob, metadata_type, metadata_blob)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (thread_id, checkpoint_ns, cp_id, parent_id, c_type, c_blob, m_type, m_blob),
            )

            # Save newly created channel blobs
            for k, ver in new_versions.items():
                b_key = (thread_id, checkpoint_ns, k, ver)
                if b_key in self.blobs:
                    b_type, b_data = self.blobs[b_key]
                    conn.execute(
                        """
                        INSERT OR REPLACE INTO langgraph_blobs
                        (thread_id, checkpoint_ns, channel, version, blob_type, blob_data)
                        VALUES (?, ?, ?, ?, ?, ?)
                        """,
                        (thread_id, checkpoint_ns, str(k), str(ver), b_type, b_data),
                    )
        return res

    def put_writes(
        self,
        config_arg: RunnableConfig,
        writes: Sequence[tuple[str, Any]],
        task_id: str,
        task_path: str = "",
    ) -> None:
        super().put_writes(config_arg, writes, task_id, task_path)
        thread_id = str(config_arg["configurable"]["thread_id"])
        checkpoint_ns = str(config_arg["configurable"].get("checkpoint_ns", ""))
        checkpoint_id = str(config_arg["configurable"]["checkpoint_id"])
        outer_key = (thread_id, checkpoint_ns, checkpoint_id)
        writes_map = self.writes.get(outer_key, {})

        with self.store._conn() as conn:
            for (t_id, idx), (tid, channel, (val_type, val_blob), tpath) in writes_map.items():
                conn.execute(
                    """
                    INSERT OR REPLACE INTO langgraph_writes
                    (thread_id, checkpoint_ns, checkpoint_id, task_id, idx,
                     channel, val_type, val_blob, task_path)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        thread_id,
                        checkpoint_ns,
                        checkpoint_id,
                        str(tid),
                        int(idx),
                        str(channel),
                        val_type,
                        val_blob,
                        tpath,
                    ),
                )

    def delete_thread(self, thread_id: str) -> None:
        super().delete_thread(thread_id)
        with self.store._conn() as conn:
            conn.execute("DELETE FROM langgraph_checkpoints WHERE thread_id = ?", (str(thread_id),))
            conn.execute("DELETE FROM langgraph_writes WHERE thread_id = ?", (str(thread_id),))
            conn.execute("DELETE FROM langgraph_blobs WHERE thread_id = ?", (str(thread_id),))


# Global singleton store
_GLOBAL_MEMORY_STORE: PersistentMemoryStore | None = None


def get_memory_store() -> PersistentMemoryStore:
    global _GLOBAL_MEMORY_STORE
    if _GLOBAL_MEMORY_STORE is None:
        _GLOBAL_MEMORY_STORE = PersistentMemoryStore()
    return _GLOBAL_MEMORY_STORE
