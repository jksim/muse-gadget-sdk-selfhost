"""The gadgets over MCP: a loopback Streamable HTTP server behind a token.

Uses the MCP SDK's own client against a live musehost, with a fake gadget
registered on the hub.
"""

import asyncio
import contextlib
import dataclasses
import json
import stat
from types import SimpleNamespace

import httpx2
import pytest
from conftest import free_port, serving
from mcp.client.client import Client
from mcp.client.streamable_http import streamable_http_client

from musehost.config import HostConfig
from musehost.hub import InvokeResult

COMMANDS = {
    "device.health": {"description": "Report health", "timeout_ms": 5000},
    "display.draw_url": {
        "description": "Show an image from a URL",
        "required": {"url": {"type": "string"}},
        "optional": {"duration_s": {"type": "integer"}},
    },
    "display.show_animation": {
        "description": "Play an animation",
        "required": {"name": {"type": "string"}},
    },
    "device.ota": {"description": "Install firmware", "required": {"url": {"type": "string"}}},
}


class FakeLink:
    """Stands in for a gadget's /link-control stream."""

    def __init__(self, results=None):
        self.calls = []
        self.results = results or {}

    async def invoke(self, command, params, timeout_s):
        self.calls.append((command, params, timeout_s))
        result = self.results.get(command, InvokeResult(ok=True, payload={"overall": "ok"}))
        if isinstance(result, Exception):
            raise result
        return result


def set_config(state, **changes):
    path = state / "host.toml"
    dataclasses.replace(HostConfig.load(path), **changes).save(path)


@contextlib.contextmanager
def mcp_host(state, port=None):
    port = port or free_port()
    set_config(state, mcp_port=port)
    with serving(state) as server:
        hub = server.config.app.state.hub
        yield SimpleNamespace(
            url=f"http://127.0.0.1:{port}/mcp",
            port=port,
            token=(state / "mcp.token").read_text().strip(),
            hub=hub,
            app=server.config.app,
        )


def register(hub, node_id="homelink-abcdef", commands=COMMANDS, link=None):
    link = link or FakeLink()
    hub.register(node_id, {"display_name": "Kitchen", "commands_v2": commands}, link)
    return link


async def with_client(url, token, action):
    async with httpx2.AsyncClient(headers={"Authorization": f"Bearer {token}"}) as http:
        async with Client(streamable_http_client(url, http_client=http)) as client:
            return await action(client)


def run(coro):
    return asyncio.run(coro)


def text_of(result):
    return "".join(part.text for part in result.content if getattr(part, "text", None))


# -- T1: listener, token, list_gadgets ----------------------------------------------------


def test_list_gadgets_shows_registered_gadgets_and_their_allowed_commands(state):
    with mcp_host(state) as host:
        register(host.hub)

        async def action(client):
            tools = await client.list_tools()
            names = [t.name for t in tools.tools]
            result = await client.call_tool("list_gadgets", {})
            return names, result

        names, result = run(with_client(host.url, host.token, action))
    assert "list_gadgets" in names
    assert not result.is_error
    [gadget] = json.loads(text_of(result))["gadgets"]
    assert gadget["node_id"] == "homelink-abcdef" and gadget["online"] is True
    assert gadget["name"] == "Kitchen"
    assert sorted(gadget["commands"]) == [
        "device.health",
        "display.draw_url",
        "display.show_animation",
    ]  # the default brain_tools; never device.ota


@pytest.mark.parametrize("header", [None, "Bearer wrong", "Basic abc"])
def test_requests_without_the_token_are_refused(state, header):
    with mcp_host(state) as host:
        headers = {"Authorization": header} if header else {}
        response = httpx2.post(host.url, json={}, headers=headers, timeout=5)
    assert response.status_code == 401


def test_the_listener_binds_loopback_only(state):
    with mcp_host(state) as host:
        config = host.app.state.mcp_server.config
    assert config.host == "127.0.0.1"


def test_mcp_port_zero_turns_it_off(state):
    port = free_port()
    set_config(state, mcp_port=0)
    with serving(state) as server:
        assert server.config.app.state.mcp_server is None
        with pytest.raises(httpx2.ConnectError):
            httpx2.post(f"http://127.0.0.1:{port}/mcp", json={}, timeout=2)


def test_the_token_is_private_and_kept_across_restarts(state):
    with mcp_host(state) as first:
        pass
    mode = stat.S_IMODE((state / "mcp.token").stat().st_mode)
    with mcp_host(state, port=first.port) as second:
        pass
    assert mode == 0o600
    assert len(first.token) >= 32 and second.token == first.token


def test_a_busy_port_leaves_musehost_serving_gadgets(state, caplog):
    import socket

    blocker = socket.socket()
    blocker.bind(("127.0.0.1", 0))
    blocker.listen()
    busy = blocker.getsockname()[1]
    try:
        set_config(state, mcp_port=busy)
        with serving(state) as server:
            assert server.started
            assert server.config.app.state.mcp_server is None
    finally:
        blocker.close()
    assert "MCP" in caplog.text and str(busy) in caplog.text


def test_the_token_never_appears_in_logs(state, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    with mcp_host(state) as host:
        register(host.hub)
        run(with_client(host.url, host.token, lambda c: c.call_tool("list_gadgets", {})))
        token = host.token
    assert token not in caplog.text
