"""Conversations between a node and Clio, kept in SQLite.

Messages are stored in the provider's native format, exactly as sent and
received, and are only ever inserted: Claude requires earlier turns (thinking
blocks included) to come back unchanged. Instead of trimming, a fresh
conversation starts after ``idle_minutes`` of quiet, once one reaches
``max_messages``, when the provider or model changes, or on request.
Conversations untouched for ``keep_days`` are deleted.
"""

from __future__ import annotations

import json
import time
from collections.abc import Callable
from dataclasses import dataclass

from musehost.store import Store
from musehost.tokens import _transaction

MAX_MESSAGES = 40
KEEP_DAYS = 30


@dataclass(frozen=True)
class Conversation:
    id: int
    messages: list[dict]


class History:
    def __init__(
        self,
        store: Store,
        clock: Callable[[], float] = time.time,
        idle_minutes: int = 30,
        max_messages: int = MAX_MESSAGES,
        keep_days: int = KEEP_DAYS,
    ) -> None:
        self.store = store
        self._clock = clock
        self.idle_s = idle_minutes * 60
        self.max_messages = max_messages
        self.keep_s = keep_days * 24 * 3600

    def current(
        self, node_id: str, provider: str, model: str, fingerprint: str | None = None
    ) -> Conversation:
        """The node's ongoing conversation, or a new empty one.

        ``fingerprint`` identifies the system prompt and tool set: Claude binds
        thinking blocks to them, so a conversation can't continue across a
        change (the API rejects the history), and a fresh one starts instead.
        """
        now = self._clock()
        db = self.store.db
        db.execute("DELETE FROM conversations WHERE last_at < ?", (now - self.keep_s,))
        row = db.execute(
            "SELECT * FROM conversations WHERE node_id = ? ORDER BY id DESC LIMIT 1", (node_id,)
        ).fetchone()
        fresh = (
            row is None
            or row["closed"]
            or now - row["last_at"] > self.idle_s
            or row["message_count"] >= self.max_messages
            or (row["provider"], row["model"]) != (provider, model)
            or row["fingerprint"] != fingerprint
        )
        if fresh:
            cursor = db.execute(
                "INSERT INTO conversations "
                "(node_id, provider, model, started_at, last_at, fingerprint) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (node_id, provider, model, now, now, fingerprint),
            )
            return Conversation(cursor.lastrowid, [])
        rows = db.execute(
            "SELECT content_json FROM conversation_messages WHERE conversation_id = ? ORDER BY seq",
            (row["id"],),
        ).fetchall()
        return Conversation(row["id"], [json.loads(r["content_json"]) for r in rows])

    def append(self, conversation_id: int, messages: list[dict]) -> None:
        if not messages:
            return
        db = self.store.db
        with _transaction(db):
            start = db.execute(
                "SELECT coalesce(max(seq), -1) + 1 FROM conversation_messages "
                "WHERE conversation_id = ?",
                (conversation_id,),
            ).fetchone()[0]
            db.executemany(
                "INSERT INTO conversation_messages (conversation_id, seq, role, content_json) "
                "VALUES (?, ?, ?, ?)",
                [
                    (conversation_id, start + i, m.get("role", ""), json.dumps(m))
                    for i, m in enumerate(messages)
                ],
            )
            db.execute(
                "UPDATE conversations SET last_at = ?, message_count = message_count + ? "
                "WHERE id = ?",
                (self._clock(), len(messages), conversation_id),
            )

    def start_new(self, node_id: str) -> None:
        """End the node's conversation; its next turn starts afresh."""
        self.store.db.execute(
            "UPDATE conversations SET closed = 1 WHERE node_id = ? AND closed = 0", (node_id,)
        )
