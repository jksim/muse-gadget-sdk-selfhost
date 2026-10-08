"""The dashboard's password and sessions.

One password and no username. With no password file the dashboard is off.
The password is stored as salted scrypt in ``<state>/dashboard.pw`` (0600)
and is only ever compared in constant time. Sessions live in the
``dashboard_sessions`` table; setting or removing the password ends them all.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
from pathlib import Path

PASSWORD_FILE = "dashboard.pw"  # noqa: S105 (a file name)
MIN_LENGTH = 12
_SCRYPT = {"n": 2**14, "r": 8, "p": 1}


def _hash(password: str, salt: bytes, n: int, r: int, p: int) -> bytes:
    return hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, dklen=32)


def is_enabled(state: Path) -> bool:
    return (state / PASSWORD_FILE).is_file()


def check_problem(password: str) -> str | None:
    """Why ``password`` can't be the dashboard password, or None."""
    if len(password) < MIN_LENGTH:
        return f"the password needs at least {MIN_LENGTH} characters"
    return None


def set_password(state: Path, password: str, store=None) -> None:
    problem = check_problem(password)
    if problem:
        raise ValueError(problem)
    salt = secrets.token_bytes(16)
    record = {
        "scheme": "scrypt",
        **_SCRYPT,
        "salt": salt.hex(),
        "hash": _hash(password, salt, **_SCRYPT).hex(),
    }
    path = state / PASSWORD_FILE
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(record, f)
    os.replace(tmp, path)
    if store is not None:
        clear_sessions(store)


def check_password(state: Path, password: str) -> bool:
    try:
        record = json.loads((state / PASSWORD_FILE).read_text())
        salt = bytes.fromhex(record["salt"])
        expected = bytes.fromhex(record["hash"])
        params = {k: int(record[k]) for k in ("n", "r", "p")}
    except (OSError, ValueError, KeyError, TypeError):
        return False
    return hmac.compare_digest(_hash(password, salt, **params), expected)


def clear_password(state: Path, store=None) -> None:
    (state / PASSWORD_FILE).unlink(missing_ok=True)
    if store is not None:
        clear_sessions(store)


def clear_sessions(store) -> None:
    store.db.execute("DELETE FROM dashboard_sessions")
