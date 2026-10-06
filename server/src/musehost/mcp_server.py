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
import inspect
import json
import logging
import os
import secrets
import socket
import weakref
from pathlib import Path

import uvicorn
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.subscriptions import InMemorySubscriptionBus, ListenHandler, ToolsListChanged

from musehost.brain.tools import build_toolset, validate
from musehost.hub import DeviceOffline, Hub, InvokeTimeout

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


READ_ONLY = frozenset({"device.health"})


class GadgetTools:
    """``list_gadgets`` plus one tool per allowlisted command the gadgets offer."""

    def __init__(self, hub: Hub, allowlist) -> None:
        self.hub = hub
        self.allowlist = frozenset(allowlist) - NEVER
        self.loop: asyncio.AbstractEventLoop | None = None
        # 2026-07-28 clients listen on the bus; older ones are told on their
        # connection (each request's ServerSession is short-lived; its
        # connection lasts as long as the client's MCP session).
        self.bus = InMemorySubscriptionBus()
        self._connections: weakref.WeakSet = weakref.WeakSet()
        hub.on_change(self._tell_clients)

    def _toolset(self, device):
        return build_toolset(device.commands, self.allowlist)

    def commands_of(self, device) -> list[str]:
        toolset = self._toolset(device)
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

    def _command_tools(self) -> dict:
        """tool name -> (ToolSpec, Command), from the first gadget offering each."""
        found: dict = {}
        for device in sorted(self.hub.devices(), key=lambda d: d.node_id):
            toolset = self._toolset(device)
            for spec in toolset.specs:
                found.setdefault(spec.name, (spec, toolset.command(spec.name)))
        return found

    async def list_tools(self, ctx, params) -> types.ListToolsResult:
        connection = getattr(ctx.session, "_connection", None)  # no public accessor in mcp 2.3
        if connection is not None:
            self._connections.add(connection)  # told when the tool list changes
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
        for name, (spec, command) in sorted(self._command_tools().items()):
            schema = dict(spec.input_schema)
            schema["properties"] = {
                "gadget": {
                    "type": "string",
                    "description": "Node id from list_gadgets; optional when one gadget is online",
                },
                **schema.get("properties", {}),
            }
            tools.append(
                types.Tool(
                    name=name,
                    description=spec.description,
                    input_schema=schema,
                    annotations=types.ToolAnnotations(read_only_hint=command.name in READ_ONLY),
                )
            )
        return types.ListToolsResult(tools=tools)

    async def call_tool(self, ctx, params: types.CallToolRequestParams) -> types.CallToolResult:
        name = params.name
        if name == "list_gadgets":
            log.info("MCP list_gadgets")
            return _text(json.dumps(self.list_gadgets()))
        arguments = dict(params.arguments or {})
        gadget = arguments.pop("gadget", None)
        offering = [d for d in self.hub.devices() if self._toolset(d).command(name) is not None]
        if not offering:
            log.info("MCP %s: no such tool", name)
            return _error(f"no gadget offers {name}")
        if gadget is not None:
            device = next((d for d in self.hub.devices() if d.node_id == gadget), None)
            if device is None or device not in offering:
                log.info("MCP %s: unknown gadget", name)
                return _error(f"no paired gadget {gadget} offers {name}")
        else:
            online = [d for d in offering if d.online]
            if len(online) != 1:
                ids = ", ".join(sorted(d.node_id for d in (online or offering)))
                log.info("MCP %s: gadget not named", name)
                return _error(f"name the gadget for {name}: {ids}")
            device = online[0]
        command = self._toolset(device).command(name)
        problem = validate(arguments, command.schema)
        if problem:
            log.info("MCP %s on %s: invalid input", name, device.node_id)
            return _error(f"invalid input: {problem}")
        try:
            result = await self.hub.invoke(
                device.node_id, command.name, arguments, command.timeout_s
            )
        except DeviceOffline:
            log.info("MCP %s on %s: offline", name, device.node_id)
            return _error(f"{device.node_id} is offline")
        except InvokeTimeout:
            log.info("MCP %s on %s: timed out", name, device.node_id)
            return _error(f"{device.node_id} didn't answer in time")
        log.info("MCP %s on %s: %s", name, device.node_id, "ok" if result.ok else "error")
        body = {"ok": result.ok, "payload": result.payload, "error": result.error}
        return _text(json.dumps(body), error=not result.ok)

    def _tell_clients(self) -> None:
        """Tell MCP clients the tool list changed: listeners on the subscription
        bus (2026-07-28 protocol) and every older session that has listed tools."""
        if self.loop is None:
            return

        async def tell() -> None:
            with contextlib.suppress(Exception):
                await self.bus.publish(ToolsListChanged())
            for connection in list(self._connections):
                with contextlib.suppress(Exception):
                    sent = connection.send_tool_list_changed()
                    if inspect.isawaitable(sent):
                        await sent

        asyncio.run_coroutine_threadsafe(tell(), self.loop)


def _text(text: str, error: bool = False) -> types.CallToolResult:
    return types.CallToolResult(content=[types.TextContent(type="text", text=text)], is_error=error)


def _error(message: str) -> types.CallToolResult:
    return _text(json.dumps({"ok": False, "error": message}), error=True)


def build_app(hub: Hub, allowlist, token_path: Path):
    tools = GadgetTools(hub, allowlist)
    tools.loop = asyncio.get_running_loop()
    server = Server(
        "musehost",
        instructions="Paired Muse gadgets. Call list_gadgets to see them.",
        on_list_tools=tools.list_tools,
        on_call_tool=tools.call_tool,
        on_subscriptions_listen=ListenHandler(tools.bus),
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
    # The SDK logs requests, arguments included, at DEBUG: keep it out even with -v.
    logging.getLogger("mcp").setLevel(logging.WARNING)
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
