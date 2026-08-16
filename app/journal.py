"""Write-ahead journal (SQLite).

Two jobs:

1. **Durable dedupe.** `claim()` inserts the platform message id under a UNIQUE constraint and
   returns None if it was already seen. This replaces the spike's in-memory set, which lost
   state on restart — and a restart is exactly when Meta is most likely to retry a delivery,
   so an in-memory set would double-log a feed after every deploy.

2. **Write-ahead log.** Every parsed intent is recorded *before* it is dispatched to
   Huckleberry, so a downstream failure can be inspected and replayed rather than lost.

Statuses: received -> parsed -> ok | failed | unparsed | voided
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS entries (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    message_id    TEXT    NOT NULL UNIQUE,
    sender        TEXT    NOT NULL,
    raw_text      TEXT    NOT NULL,
    skill         TEXT,
    args_json     TEXT,
    status        TEXT    NOT NULL,
    error         TEXT,
    created_at    TEXT    NOT NULL,
    dispatched_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_entries_sender  ON entries(sender);
CREATE INDEX IF NOT EXISTS idx_entries_status  ON entries(status);
CREATE INDEX IF NOT EXISTS idx_entries_created ON entries(created_at);

-- Pending clarifying questions, keyed PER SENDER so one person's answer can never
-- complete a question the bot asked someone else (see PLAN.md decision 6).
CREATE TABLE IF NOT EXISTS pending (
    sender     TEXT PRIMARY KEY,
    skill      TEXT NOT NULL,
    args_json  TEXT NOT NULL,
    question   TEXT NOT NULL,
    created_at TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(UTC).isoformat()


class Journal:
    def __init__(self, db_path: str) -> None:
        self._path = db_path
        if db_path != ":memory:":
            Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(SCHEMA)
        self._conn.commit()
        self._lock = asyncio.Lock()

    # ---- entries ------------------------------------------------------------

    async def claim(self, message_id: str, sender: str, raw_text: str) -> int | None:
        """Record an inbound message. Returns its row id, or None if already seen."""

        def _claim() -> int | None:
            try:
                cur = self._conn.execute(
                    "INSERT INTO entries (message_id, sender, raw_text, status, created_at) "
                    "VALUES (?, ?, ?, 'received', ?)",
                    (message_id, sender, raw_text, _now()),
                )
                self._conn.commit()
                return cur.lastrowid
            except sqlite3.IntegrityError:
                return None

        async with self._lock:
            return await asyncio.to_thread(_claim)

    async def set_parsed(self, row_id: int, skill: str, args: dict[str, Any]) -> None:
        await self._update(
            row_id,
            "skill = ?, args_json = ?, status = 'parsed'",
            (skill, json.dumps(args, default=str)),
        )

    async def mark_ok(self, row_id: int) -> None:
        await self._update(row_id, "status = 'ok', dispatched_at = ?", (_now(),))

    async def mark_failed(self, row_id: int, error: str) -> None:
        await self._update(row_id, "status = 'failed', error = ?, dispatched_at = ?", (error, _now()))

    async def mark_unparsed(self, row_id: int) -> None:
        await self._update(row_id, "status = 'unparsed'", ())

    async def _update(self, row_id: int, set_clause: str, params: tuple) -> None:
        def _run() -> None:
            self._conn.execute(f"UPDATE entries SET {set_clause} WHERE id = ?", (*params, row_id))
            self._conn.commit()

        async with self._lock:
            await asyncio.to_thread(_run)

    async def failed_entries(self, limit: int = 20) -> list[sqlite3.Row]:
        def _run() -> list[sqlite3.Row]:
            return list(
                self._conn.execute(
                    "SELECT * FROM entries WHERE status = 'failed' ORDER BY id DESC LIMIT ?",
                    (limit,),
                )
            )

        return await asyncio.to_thread(_run)

    async def recent(self, sender: str | None = None, limit: int = 10) -> list[sqlite3.Row]:
        def _run() -> list[sqlite3.Row]:
            if sender:
                sql = "SELECT * FROM entries WHERE sender = ? AND status = 'ok' ORDER BY id DESC LIMIT ?"
                return list(self._conn.execute(sql, (sender, limit)))
            sql = "SELECT * FROM entries WHERE status = 'ok' ORDER BY id DESC LIMIT ?"
            return list(self._conn.execute(sql, (limit,)))

        return await asyncio.to_thread(_run)

    # ---- pending clarifications --------------------------------------------

    async def set_pending(self, sender: str, skill: str, args: dict, question: str) -> None:
        def _run() -> None:
            self._conn.execute(
                "INSERT INTO pending (sender, skill, args_json, question, created_at) "
                "VALUES (?, ?, ?, ?, ?) ON CONFLICT(sender) DO UPDATE SET "
                "skill=excluded.skill, args_json=excluded.args_json, "
                "question=excluded.question, created_at=excluded.created_at",
                (sender, skill, json.dumps(args, default=str), question, _now()),
            )
            self._conn.commit()

        async with self._lock:
            await asyncio.to_thread(_run)

    async def get_pending(self, sender: str) -> dict | None:
        def _run() -> dict | None:
            row = self._conn.execute("SELECT * FROM pending WHERE sender = ?", (sender,)).fetchone()
            if row is None:
                return None
            return {
                "skill": row["skill"],
                "args": json.loads(row["args_json"]),
                "question": row["question"],
                "created_at": row["created_at"],
            }

        return await asyncio.to_thread(_run)

    async def clear_pending(self, sender: str) -> None:
        def _run() -> None:
            self._conn.execute("DELETE FROM pending WHERE sender = ?", (sender,))
            self._conn.commit()

        async with self._lock:
            await asyncio.to_thread(_run)

    def close(self) -> None:
        self._conn.close()
