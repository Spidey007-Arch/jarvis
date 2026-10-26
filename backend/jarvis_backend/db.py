"""SQLite storage for memories and conversations."""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id               TEXT PRIMARY KEY,
    type             TEXT NOT NULL,
    subject          TEXT NOT NULL,
    content          TEXT NOT NULL,
    importance       REAL NOT NULL DEFAULT 0.5,
    status           TEXT NOT NULL DEFAULT 'active',   -- active | superseded | summarized | expired
    superseded_by    TEXT,
    source           TEXT,                             -- conversation id, 'manual' or 'import'
    created_at       REAL NOT NULL,
    updated_at       REAL NOT NULL,
    last_accessed_at REAL,
    access_count     INTEGER NOT NULL DEFAULT 0,
    embedding        BLOB,
    embedding_model  TEXT
);
CREATE INDEX IF NOT EXISTS idx_mem_status_type ON memories(status, type);
CREATE INDEX IF NOT EXISTS idx_mem_subject ON memories(subject);

CREATE TABLE IF NOT EXISTS conversations (
    id         TEXT PRIMARY KEY,
    title      TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    pinned     INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS messages (
    id              TEXT PRIMARY KEY,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role            TEXT NOT NULL,                     -- user | model
    content         TEXT NOT NULL,
    created_at      REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_conv ON messages(conversation_id, created_at);

CREATE TABLE IF NOT EXISTS reminders (
    id         TEXT PRIMARY KEY,
    text       TEXT NOT NULL,
    due_at     REAL NOT NULL,
    created_at REAL NOT NULL,
    status     TEXT NOT NULL,                          -- pending | done | cancelled | missed
    fired_at   REAL
);
CREATE INDEX IF NOT EXISTS idx_rem_due ON reminders(status, due_at);

CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
"""


class DB:
    """One shared connection guarded by a lock; every call is short."""

    def __init__(self, path: Path) -> None:
        self._conn = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._conn.executescript(SCHEMA)
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(messages)")}
            if "actions" not in cols:  # added in milestone 3: what JARVIS did while answering
                self._conn.execute("ALTER TABLE messages ADD COLUMN actions TEXT")
            if "kind" not in {r[1] for r in self._conn.execute("PRAGMA table_info(reminders)")}:  # alarms and timers
                self._conn.execute("ALTER TABLE reminders ADD COLUMN kind TEXT NOT NULL DEFAULT 'reminder'")

    def all(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]

    def one(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        with self._lock:
            r = self._conn.execute(sql, tuple(params)).fetchone()
            return dict(r) if r else None

    def run(self, sql: str, params: Iterable[Any] = ()) -> int:
        with self._lock:
            return self._conn.execute(sql, tuple(params)).rowcount

    def transaction(self, statements: list[tuple[str, Iterable[Any]]]) -> None:
        with self._lock:
            self._conn.execute("BEGIN")
            try:
                for sql, params in statements:
                    self._conn.execute(sql, tuple(params))
                self._conn.execute("COMMIT")
            except Exception:
                self._conn.execute("ROLLBACK")
                raise

    def close(self) -> None:
        with self._lock:
            self._conn.close()
