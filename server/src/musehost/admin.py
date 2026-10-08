"""A local Unix socket through which the CLI asks the running server for things.

The hub (who is online, invokes, unpairing) lives in the server process, so
``musehost invoke`` and friends send it one JSON request per line here and get
one JSON reply. The socket sits in the 0700 state directory (or wherever
``$MUSEHOST_ADMIN_SOCKET`` says) with mode 0600, so only the ``musehost``
account can use it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import socket
from collections.abc import Iterator
from pathlib import Path

from musehost.hub import DeviceOffline, Hub, InvokeTimeout

log = logging.getLogger(__name__)

SOCKET_ENV = "MUSEHOST_ADMIN_SOCKET"
SOCKET_FILE = "admin.sock"
MAX_REQUEST = 1024 * 1024


class AdminUnavailable(Exception):
    """The server isn't running (or its socket can't be reached)."""


def socket_path(state: Path) -> Path:
    return Path(os.environ.get(SOCKET_ENV) or state / SOCKET_FILE)


# -- Server side -------------------------------------------------------------------


async def serve(path: Path, hub: Hub) -> asyncio.AbstractServer:
    path.unlink(missing_ok=True)

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            line = await asyncio.wait_for(reader.readline(), 10)
            request = json.loads(line)
            if isinstance(request, dict) and request.get("op") == "chat":
                await _chat(request, hub, writer)
                return
            reply = await _dispatch(request, hub)
        except Exception as exc:
            reply = {"ok": False, "code": "bad_request", "error": f"{type(exc).__name__}"}
        writer.write(json.dumps(reply).encode() + b"\n")
        try:
            await writer.drain()
        finally:
            writer.close()

    server = await asyncio.start_unix_server(handle, str(path), limit=MAX_REQUEST)
    os.chmod(path, 0o600)
    log.info("admin socket at %s", path)
    return server


async def _chat(request: dict, hub: Hub, writer: asyncio.StreamWriter) -> None:
    """One operator turn: a JSON line per piece of Clio's reply, then ``done``."""
    from musehost.chat import operator_turn

    device = request.get("device")
    try:
        async for piece in operator_turn(
            hub,
            str(request.get("message") or ""),
            device=device if isinstance(device, str) else None,
            new=bool(request.get("new")),
        ):
            writer.write(json.dumps({"text": piece}).encode() + b"\n")
            await writer.drain()
        writer.write(b'{"done": true}\n')
        await writer.drain()
    finally:
        writer.close()


async def _dispatch(request: dict, hub: Hub) -> dict:
    op = request.get("op")
    if op == "devices":
        return {
            "ok": True,
            "devices": [
                {
                    "node_id": d.node_id,
                    "online": d.online,
                    "platform": d.platform,
                    "display_name": d.display_name,
                    "registered_at": d.registered_at,
                    "last_heartbeat": d.last_heartbeat,
                    "commands": sorted(d.commands),
                }
                for d in hub.devices()
            ],
        }
    if op == "invoke":
        try:
            result = await hub.invoke(
                request["node_id"],
                request["command"],
                request.get("params") or {},
                float(request.get("timeout_s", 30)),
            )
        except DeviceOffline:
            return {"ok": False, "code": "offline", "error": f"{request['node_id']} is offline"}
        except InvokeTimeout:
            return {"ok": False, "code": "timeout", "error": "the device didn't answer in time"}
        return {
            "ok": True,
            "result": {"ok": result.ok, "payload": result.payload, "error": result.error},
        }
    if op == "unpair":
        return {"ok": True, "notified": await hub.unpair(request["node_id"])}
    return {"ok": False, "code": "bad_request", "error": f"unknown op {op!r}"}


# -- Client side -------------------------------------------------------------------


def stream(state: Path, message: dict, timeout: float = 120) -> Iterator[dict]:
    """Send one request and yield each JSON line of the reply until ``done``."""
    path = socket_path(state)
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(str(path))
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        raise AdminUnavailable(str(path)) from exc
    with sock:
        sock.sendall(json.dumps(message).encode() + b"\n")
        buffer = b""
        while True:
            data = sock.recv(65536)
            if not data:
                return
            buffer += data
            while b"\n" in buffer:
                line, buffer = buffer.split(b"\n", 1)
                item = json.loads(line)
                yield item
                if item.get("done"):
                    return


def request(state: Path, message: dict, timeout: float = 10) -> dict:
    path = socket_path(state)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(timeout)
            sock.connect(str(path))
            sock.sendall(json.dumps(message).encode() + b"\n")
            reply = bytearray()
            while not reply.endswith(b"\n"):
                data = sock.recv(65536)
                if not data:
                    break
                reply += data
    except (FileNotFoundError, ConnectionRefusedError) as exc:
        raise AdminUnavailable(str(path)) from exc
    except TimeoutError as exc:
        raise AdminUnavailable(f"{path}: no reply in time") from exc
    return json.loads(reply)
