"""Device, access, refresh and VM tokens.

Every token is an opaque random string, stored only as its SHA-256 hash and
looked up by that hash, so a stolen database yields no usable token.
"""

from __future__ import annotations

import hashlib
import logging
import re
import secrets
import time
from collections.abc import Callable

from musehost.store import Store

log = logging.getLogger(__name__)

ACCESS_TTL_S = 4 * 3600  # the gadget rotates at 3 h
VM_TTL_S = 15 * 60
GRANT_TTL_S = 10 * 60
NODE_ID_RE = re.compile(r"homelink-[0-9a-f]{6}")


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


class Tokens:
    def __init__(self, store: Store, clock: Callable[[], float] = time.time) -> None:
        self.store = store
        self._clock = clock
        # Called with the node id after every revocation (the server uses it to
        # drop the device's live sessions).
        self.on_revoke: Callable[[str], None] | None = None

    def now(self) -> int:
        return int(self._clock())

    # -- Enrollment -------------------------------------------------------------

    def enroll(self, node_id: str, display_name: str = "") -> tuple[str, str]:
        """Create or re-pair ``node_id``; returns a fresh (access, refresh) pair.

        Re-pairing drops every token the device held before.
        """
        if not NODE_ID_RE.fullmatch(node_id):
            raise ValueError(f"not a gadget node id: {node_id!r}")
        db = self.store.db
        with _transaction(db):
            db.execute(
                "INSERT INTO devices (node_id, display_name, enrolled_at) VALUES (?, ?, ?) "
                "ON CONFLICT (node_id) DO UPDATE SET display_name = excluded.display_name, "
                "enrolled_at = excluded.enrolled_at, revoked_at = NULL",
                (node_id, display_name, self.now()),
            )
            db.execute("DELETE FROM tokens WHERE node_id = ?", (node_id,))
            return self._issue_pair(node_id)

    def revoke(self, node_id: str) -> bool:
        """Revoke ``node_id``; returns False if it was never enrolled."""
        db = self.store.db
        with _transaction(db):
            cursor = db.execute(
                "UPDATE devices SET revoked_at = ? WHERE node_id = ?", (self.now(), node_id)
            )
            db.execute("DELETE FROM tokens WHERE node_id = ?", (node_id,))
        if cursor.rowcount == 1 and self.on_revoke is not None:
            self.on_revoke(node_id)
        return cursor.rowcount == 1

    def is_active(self, node_id: str) -> bool:
        """Enrolled and not revoked."""
        row = self.store.db.execute(
            "SELECT revoked_at FROM devices WHERE node_id = ?", (node_id,)
        ).fetchone()
        return row is not None and row["revoked_at"] is None

    # -- Refresh --------------------------------------------------------------------

    def refresh(self, token: str, node_id: str) -> tuple[str, str] | None:
        """Rotate the pair for a refresh token; None means refuse with 401.

        A refresh token works once. Presenting it again is allowed only as a
        retry of a refresh whose response was lost, which is proven by the pair
        it produced never having been used; there is no time limit, because the
        gadget only refreshes between sessions that can last hours. That pair is
        then marked superseded and replaced, since only hashes are stored and it
        can't be sent again. Any other reuse, including presenting a superseded
        token, suggests a stolen token, so the device is revoked.

        Rotated and superseded refresh tokens are kept as tombstones so reuse is
        detected however late it comes.
        """
        db, now = self.store.db, self.now()
        with _transaction(db):
            row = self._live(token, "refresh", superseded_ok=True)
            if row is None or row["node_id"] != node_id:
                return None
            if row["superseded_at"] is None and row["rotated_at"] is None:
                db.execute("UPDATE tokens SET rotated_at = ? WHERE hash = ?", (now, row["hash"]))
                return self._issue_pair(node_id, parent_hash=row["hash"])
            children = db.execute(
                "SELECT used_at, rotated_at FROM tokens "
                "WHERE parent_hash = ? AND superseded_at IS NULL",
                (row["hash"],),
            ).fetchall()
            unused = bool(children) and all(
                child["used_at"] is None and child["rotated_at"] is None for child in children
            )
            if row["superseded_at"] is None and unused:
                db.execute(
                    "UPDATE tokens SET superseded_at = ? "
                    "WHERE parent_hash = ? AND superseded_at IS NULL",
                    (now, row["hash"]),
                )
                return self._issue_pair(node_id, parent_hash=row["hash"])
        log.warning("refresh token reused for %s; revoking the device", node_id)
        self.revoke(node_id)
        return None

    # -- Access and VM tokens -----------------------------------------------------

    def device_for_access(self, token: str) -> str | None:
        """The node id a live access token belongs to, recording its use."""
        row = self._live(token, "access")
        if row is None:
            return None
        now = self.now()
        db = self.store.db
        with _transaction(db):
            if row["used_at"] is None:
                # The first use of a new access token retires the older ones.
                db.execute(
                    "DELETE FROM tokens WHERE kind = 'access' AND node_id = ? "
                    "AND rowid < (SELECT rowid FROM tokens WHERE hash = ?)",
                    (row["node_id"], row["hash"]),
                )
                db.execute("UPDATE tokens SET used_at = ? WHERE hash = ?", (now, row["hash"]))
            db.execute("UPDATE devices SET last_seen = ? WHERE node_id = ?", (now, row["node_id"]))
        return row["node_id"]

    def issue_vm_token(self, node_id: str, vm_id: str) -> str:
        """A bearer for the Noise WebSocket upgrade to ``vm_id``."""
        token, now = new_token(), self.now()
        db = self.store.db
        with _transaction(db):
            db.execute("DELETE FROM tokens WHERE kind = 'vm' AND expires_at <= ?", (now,))
            db.execute(
                "INSERT INTO tokens (hash, kind, node_id, vm_id, issued_at, expires_at) "
                "VALUES (?, 'vm', ?, ?, ?, ?)",
                (hash_token(token), node_id, vm_id, now, now + VM_TTL_S),
            )
        return token

    def verify_vm_token(self, token: str, vm_id: str) -> str | None:
        """The node id a live VM bearer for ``vm_id`` belongs to, else None."""
        row = self._live(token, "vm")
        if row is None or row["vm_id"] != vm_id:
            return None
        return row["node_id"]

    # -- Enrollment grants ----------------------------------------------------------

    def create_grant(self) -> str:
        """A single-use enrollment code for the pairing app."""
        code, now = secrets.token_urlsafe(16), self.now()
        db = self.store.db
        with _transaction(db):
            db.execute("DELETE FROM grants WHERE expires_at <= ?", (now,))
            db.execute(
                "INSERT INTO grants (hash, created_at, expires_at) VALUES (?, ?, ?)",
                (hash_token(code), now, now + GRANT_TTL_S),
            )
        return code

    def redeem_grant(self, code: str) -> bool:
        """Use up ``code``; False if it is unknown, used or expired."""
        if not code:
            return False
        cursor = self.store.db.execute(
            "UPDATE grants SET used_at = ? WHERE hash = ? AND used_at IS NULL AND expires_at > ?",
            (self.now(), hash_token(code), self.now()),
        )
        return cursor.rowcount == 1

    # -- Internals ----------------------------------------------------------------

    def _issue_pair(self, node_id: str, parent_hash: str | None = None) -> tuple[str, str]:
        access, refresh, now = new_token(), new_token(), self.now()
        self.store.db.execute(
            "DELETE FROM tokens WHERE kind = 'access' AND node_id = ? AND expires_at <= ?",
            (node_id, now),
        )
        self.store.db.executemany(
            "INSERT INTO tokens (hash, kind, node_id, issued_at, expires_at, parent_hash) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            [
                (hash_token(access), "access", node_id, now, now + ACCESS_TTL_S, parent_hash),
                (hash_token(refresh), "refresh", node_id, now, None, parent_hash),
            ],
        )
        return access, refresh

    def _live(self, token: str, kind: str, superseded_ok: bool = False):
        """The token's row if it is of ``kind``, unexpired, and its device is active.

        Superseded tokens count only with ``superseded_ok``, so refresh can
        treat them as reuse.
        """
        if not token:
            return None
        row = self.store.db.execute(
            "SELECT t.* FROM tokens t JOIN devices d USING (node_id) "
            "WHERE t.hash = ? AND t.kind = ? AND d.revoked_at IS NULL "
            "AND (t.expires_at IS NULL OR t.expires_at > ?)",
            (hash_token(token), kind, self.now()),
        ).fetchone()
        if row is not None and row["superseded_at"] is not None and not superseded_ok:
            return None
        return row


class _transaction:
    """BEGIN IMMEDIATE ... COMMIT/ROLLBACK on an autocommit connection."""

    def __init__(self, db) -> None:
        self.db = db

    def __enter__(self):
        self.db.execute("BEGIN IMMEDIATE")
        return self.db

    def __exit__(self, exc_type, exc, tb) -> None:
        if exc_type:
            self.db.execute("ROLLBACK")
            return
        try:
            self.db.execute("COMMIT")
        except Exception:
            # A failed COMMIT leaves the transaction open; without this every
            # later BEGIN on the shared connection would fail.
            self.db.execute("ROLLBACK")
            raise
