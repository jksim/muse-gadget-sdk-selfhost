"""Which gadgets are online, and the in-process API ``brain`` will use.

The hub tracks each node's registered Link stream (one at a time; a newer
session replaces an older one) and what it advertised in ``link.register``.
Everything here runs on the server's event loop.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from musehost.link import LinkConnection

log = logging.getLogger(__name__)


class DeviceOffline(Exception):
    """The node has no registered Link session."""


class InvokeTimeout(Exception):
    """The node didn't answer an invoke in time."""


@dataclass(frozen=True)
class InvokeResult:
    ok: bool
    payload: Any = None
    error: str | None = None


@dataclass(frozen=True)
class ChatTurn:
    """One message a gadget sent to Clio."""

    node_id: str
    message_id: str
    text: str | None = None
    audio_path: Path | None = None
    duration_s: float | None = None
    session_id: str | None = None
    # For voice notes: whether speech-to-text ran (an empty ``text`` then means
    # "said nothing"), and its status (ok, empty, loading, off, failed).
    transcribed: bool = False
    speech_status: str | None = None
    # Whose tools Clio may use, if not the speaker's own (``musehost chat --device``).
    tool_node_id: str | None = None


ChatHandler = Callable[[ChatTurn], AsyncIterator[str]]


@dataclass
class Device:
    node_id: str
    display_name: str = ""
    platform: str = ""
    version: str = ""
    commands: dict = field(default_factory=dict)
    online: bool = False
    registered_at: float | None = None
    last_heartbeat: float | None = None


class Hub:
    def __init__(self) -> None:
        self._devices: dict[str, Device] = {}
        self._links: dict[str, LinkConnection] = {}
        self._sessions: dict[str, set] = {}
        self._subscriptions: dict[str, set] = {}
        self._seq: dict[str, int] = {}
        self.chat_handler: ChatHandler | None = None

    def devices(self) -> list[Device]:
        return sorted(self._devices.values(), key=lambda d: d.node_id)

    def register(self, node_id: str, params: dict, link: LinkConnection) -> None:
        previous = self._links.get(node_id)
        if previous is not None and previous is not link:
            log.info("%s registered again; replacing its older session", node_id)
        self._links[node_id] = link
        self._devices[node_id] = Device(
            node_id=node_id,
            display_name=str(params.get("display_name") or ""),
            platform=str(params.get("platform") or ""),
            version=str(params.get("version") or ""),
            commands=dict(params.get("commands_v2") or {}),
            online=True,
            registered_at=time.time(),
        )
        log.info("%s registered (%d commands)", node_id, len(self._devices[node_id].commands))

    def unregister(self, node_id: str, link: LinkConnection) -> None:
        if self._links.get(node_id) is link:
            del self._links[node_id]
            self._devices[node_id].online = False
            log.info("%s went offline", node_id)

    def heartbeat(self, node_id: str) -> None:
        device = self._devices.get(node_id)
        if device is not None:
            device.last_heartbeat = time.time()

    def attach(self, node_id: str, session) -> None:
        self._sessions.setdefault(node_id, set()).add(session)

    def session_count(self, node_id: str) -> int:
        return len(self._sessions.get(node_id, ()))

    def detach(self, node_id: str, session) -> None:
        self._sessions.get(node_id, set()).discard(session)

    async def unpair(self, node_id: str) -> bool:
        """Tell the node it was removed, then drop its sessions.

        The gadget wipes its setup and goes back to pairing. Returns False if
        the node wasn't online to be told.
        """
        link = self._links.get(node_id)
        told = False
        if link is not None:
            try:
                await link.send({"type": "event", "event": "link.unpaired"})
                told = True
                log.info("told %s it was unpaired", node_id)
            except Exception as exc:
                log.warning("could not tell %s it was unpaired: %s", node_id, type(exc).__name__)
        for session in list(self._sessions.get(node_id, ())):
            await session.close()
        return told

    # -- Chat ------------------------------------------------------------------------

    def set_chat_handler(self, handler: ChatHandler) -> None:
        """Who answers chat turns; ``brain`` replaces the placeholder with this."""
        self.chat_handler = handler

    def subscribe(self, node_id: str, stream) -> None:
        self._subscriptions.setdefault(node_id, set()).add(stream)

    def unsubscribe(self, node_id: str, stream) -> None:
        self._subscriptions.get(node_id, set()).discard(stream)

    async def publish(self, node_id: str, event: str, payload: dict) -> None:
        """Send one chat event to every /chat/subscribe stream of the node."""
        seq = self._seq.get(node_id, 0) + 1
        self._seq[node_id] = seq
        line = json.dumps(
            {"type": "event", "seq": seq, "event": event, "payload": payload},
            separators=(",", ":"),
        ).encode()
        for stream in list(self._subscriptions.get(node_id, ())):
            try:
                await stream.send(line + b"\n")
            except Exception as exc:
                log.info("dropping a chat subscription of %s: %s", node_id, type(exc).__name__)
                self.unsubscribe(node_id, stream)

    def link(self, node_id: str) -> LinkConnection | None:
        return self._links.get(node_id)

    async def invoke(
        self, node_id: str, command: str, params: dict | None = None, timeout_s: float = 30
    ) -> InvokeResult:
        """Run one of the node's ``commands_v2`` and return its ``link.result``."""
        link = self._links.get(node_id)
        if link is None:
            raise DeviceOffline(node_id)
        return await link.invoke(command, params or {}, timeout_s)
