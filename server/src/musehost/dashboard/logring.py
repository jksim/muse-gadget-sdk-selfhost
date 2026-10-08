"""musehost's own recent log lines, kept in memory for the dashboard.

Only the ``musehost`` loggers feed it, and they already keep keys, tokens,
transcripts and replies out of their messages; nothing here reads the journal.
"""

from __future__ import annotations

import logging
import threading
from collections import deque

KEEP = 200
_FORMAT = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s", "%H:%M:%S")


class Ring(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.INFO)
        self._lines: deque[str] = deque(maxlen=KEEP)
        self._lock = threading.Lock()

    def emit(self, record: logging.LogRecord) -> None:
        try:
            line = _FORMAT.format(record)
        except Exception:
            return
        with self._lock:
            self._lines.append(line)

    def lines(self) -> list[str]:
        with self._lock:
            return list(self._lines)

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()


_ring: Ring | None = None


def install() -> Ring:
    """Attach the ring to the ``musehost`` logger once; returns it."""
    global _ring
    if _ring is None:
        _ring = Ring()
        logging.getLogger("musehost").addHandler(_ring)
    return _ring
