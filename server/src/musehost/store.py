"""SQLite storage for devices, tokens and enrollment grants.

Tokens and grants are stored only as SHA-256 hashes; the plaintext exists
only in the response that issued it.
"""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    node_id      TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    enrolled_at  INTEGER NOT NULL,
    last_seen    INTEGER,
    revoked_at   INTEGER
);
CREATE TABLE IF NOT EXISTS tokens (
    hash        TEXT PRIMARY KEY,
    kind        TEXT NOT NULL CHECK (kind IN ('access', 'refresh', 'vm')),
    node_id     TEXT NOT NULL REFERENCES devices (node_id) ON DELETE CASCADE,
    vm_id       TEXT,
    issued_at   INTEGER NOT NULL,
    expires_at  INTEGER,
    used_at     INTEGER,
    rotated_at    INTEGER,
    superseded_at INTEGER,
    parent_hash   TEXT
);
CREATE INDEX IF NOT EXISTS tokens_by_node ON tokens (node_id, kind);
CREATE TABLE IF NOT EXISTS conversations (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    node_id       TEXT NOT NULL,
    provider      TEXT NOT NULL,
    model         TEXT NOT NULL,
    started_at    REAL NOT NULL,
    last_at       REAL NOT NULL,
    message_count INTEGER NOT NULL DEFAULT 0,
    closed        INTEGER NOT NULL DEFAULT 0,
    fingerprint   TEXT
);
CREATE INDEX IF NOT EXISTS conversations_by_node ON conversations (node_id, id);
CREATE TABLE IF NOT EXISTS conversation_messages (
    conversation_id INTEGER NOT NULL REFERENCES conversations (id) ON DELETE CASCADE,
    seq             INTEGER NOT NULL,
    role            TEXT NOT NULL,
    content_json    TEXT NOT NULL,
    PRIMARY KEY (conversation_id, seq)
);
CREATE TABLE IF NOT EXISTS dashboard_sessions (
    token_hash TEXT PRIMARY KEY,
    csrf       TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_seen  REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS grants (
    hash       TEXT PRIMARY KEY,
    created_at INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    used_at    INTEGER
);
"""


class Store:
    def __init__(self, db: sqlite3.Connection) -> None:
        self.db = db

    @classmethod
    def open(cls, path: Path) -> Store:
        new = not path.exists()
        if new:
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT, 0o600))
        db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys = ON")
        # WAL lets the CLI read while the server writes; the short busy wait
        # bounds how long a handler can stall the event loop on a lock.
        db.execute("PRAGMA journal_mode = WAL")
        db.execute("PRAGMA busy_timeout = 1000")
        db.executescript(SCHEMA)
        columns = {row["name"] for row in db.execute("PRAGMA table_info(tokens)")}
        if "superseded_at" not in columns:  # databases created before T8's review fixes
            db.execute("ALTER TABLE tokens ADD COLUMN superseded_at INTEGER")
        columns = {row["name"] for row in db.execute("PRAGMA table_info(conversations)")}
        if "fingerprint" not in columns:  # databases created before tool fingerprints
            db.execute("ALTER TABLE conversations ADD COLUMN fingerprint TEXT")
        return cls(db)

    def devices(self) -> list:
        return self.db.execute(
            "SELECT node_id, display_name, enrolled_at, last_seen, revoked_at "
            "FROM devices ORDER BY enrolled_at, node_id"
        ).fetchall()

    def close(self) -> None:
        self.db.close()
