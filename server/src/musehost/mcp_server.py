"""The paired gadgets as MCP tools, for Hermes Agent or any MCP client on the Pi.

A Model Context Protocol server over Streamable HTTP on 127.0.0.1 only, behind
a bearer token kept in ``<state>/mcp.token`` (0600). It runs inside the
musehost process beside the gadget-facing server, which owns signal handling,
and offers the same commands Clio may use (``brain_tools``), never
``device.ota``. Arguments and results are never logged.
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import json
import logging
import os
import secrets
import socket
from pathlib import Path

import uvicorn
from mcp import types
from mcp.server.lowlevel import Server

from musehost.brain.tools import build_toolset
from musehost.hub import Hub

log = logging.getLogger(__name__)

TOKEN_FILE = "mcp.token"  # noqa: S105 (a file name, not a secret)
PATH = "/mcp"
NEVER = frozenset({"device.ota"})  # not over MCP, whatever brain_tools says


# -- the token ---------------------------------------------------------------------------


def _write_token(path: Path) -> str:
    token = secrets.token_urlsafe(32)
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(token + "\n")
    os.replace(tmp, path)
    return token


def load_token(state: Path) -> str:
    """The MCP token, created on first use."""
    path = state / TOKEN_FILE
    if path.exists():
        return path.read_text().strip()
    return _write_token(path)


def rotate_token(state: Path) -> str:
    return _write_token(state / TOKEN_FILE)


def _require_token(app, token_path: Path):
    """ASGI wrapper: 401 unless ``Authorization: Bearer <current token>``.

    The token is read per request, so a rotation takes effect at once.
    """

    async def wrapped(scope, receive, send):
        if scope["type"] == "http":
            given = dict(scope.get("headers") or []).get(b"authorization", b"")
            try:
                expected = b"Bearer " + token_path.read_text().strip().encode()
            except OSError:
                expected = None
            if expected is None or not hmac.compare_digest(given, expected):
                await send({"type": "http.response.start", "status": 401, "headers": []})
                await send({"type": "http.response.body", "body": b""})
                return
        await app(scope, receive, send)

    return wrapped


# -- the tools ---------------------------------------------------------------------------


class GadgetTools:
    def __init__(self, hub: Hub, allowlist) -> None:
        self.hub = hub
        self.allowlist = frozenset(allowlist) - NEVER

    def commands_of(self, device) -> list[str]:
        toolset = build_toolset(device.commands, self.allowlist)
        return sorted(toolset.command(spec.name).name for spec in toolset.specs)

    def list_gadgets(self) -> dict:
        return {
            "gadgets": [
                {
                    "node_id": d.node_id,
                    "name": d.display_name,
                    "online": d.online,
                    "commands": self.commands_of(d),
                }
                for d in self.hub.devices()
            ]
        }

    async def list_tools(self, ctx, params) -> types.ListToolsResult:
        tools = [
            types.Tool(
                name="list_gadgets",
                description=(
                    "The paired gadgets: node id, name, online, and the commands each offers."
                ),
                input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                annotations=types.ToolAnnotations(read_only_hint=True),
            )
        ]
        return types.ListToolsResult(tools=tools)

    async def call_tool(self, ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        if params.name == "list_gadgets":
            log.info("MCP list_gadgets")
            return _text(json.dumps(self.list_gadgets()))
        log.info("MCP unknown tool %s", params.name)
        return _text(f"unknown tool {params.name}", error=True)


def _text(text: str, error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=error)


def build_app(hub: Hub, allowlist, token_path: Path):
    tools = GadgetTools(hub, allowlist)
    server = Server(
        "musehost",
        instructions="Paired Muse gadgets. Call list_gadgets to see them.",
        on_list_tools=tools.list_tools,
        on_call_tool=tools.call_tool,
    )
    return _require_token(server.streamable_http_app(streamable_http_path=PATH), token_path)


# -- serving -----------------------------------------------------------------------------


class _Listener(uvicorn.Server):
    """A uvicorn server that leaves signals to the main musehost server."""

    @contextlib.contextmanager
    def capture_signals(self):
        yield


async def start(hub: Hub, config, state: Path) -> tuple[_Listener, object] | None:
    """Serve MCP on 127.0.0.1:config.mcp_port; None when off or the port is busy."""
    if not config.mcp_port:
        return None
    load_token(state)
    # Bind first: a busy port must not stop musehost (uvicorn would exit).
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("127.0.0.1", config.mcp_port))
    except OSError as exc:
        sock.close()
        log.error("MCP off: can't listen on 127.0.0.1:%d (%s)", config.mcp_port, exc.strerror)
        return None
    app = build_app(hub, config.brain_tools, state / TOKEN_FILE)
    listener = _Listener(
        uvicorn.Config(
            app,
            host="127.0.0.1",
            port=config.mcp_port,
            log_level="warning",
            access_log=False,
            lifespan="on",
            # An MCP client may hold a stream open; don't let it stall a stop.
            timeout_graceful_shutdown=3,
        )
    )
    task = asyncio.create_task(listener.serve(sockets=[sock]))
    log.info("MCP: gadgets as tools on http://127.0.0.1:%d%s", config.mcp_port, PATH)
    return listener, task


async def stop(running: tuple[_Listener, object] | None) -> None:
    if running is None:
        return
    listener, task = running
    listener.should_exit = True
    with contextlib.suppress(Exception):
        await task
