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


# -- T2: gadget command tools -------------------------------------------------------------


def tools_by_name(host):
    async def action(client):
        return {t.name: t for t in (await client.list_tools()).tools}

    return run(with_client(host.url, host.token, action))


def call(host, name, arguments):
    return run(with_client(host.url, host.token, lambda c: c.call_tool(name, arguments)))


def test_one_tool_per_allowed_command_and_never_ota(state):
    with mcp_host(state) as host:
        register(host.hub)
        tools = tools_by_name(host)
    assert sorted(tools) == [
        "device_health",
        "display_draw_url",
        "display_show_animation",
        "list_gadgets",
    ]
    draw = tools["display_draw_url"]
    assert draw.description == "Show an image from a URL"
    assert set(draw.input_schema["properties"]) == {"gadget", "url", "duration_s"}
    assert draw.input_schema["required"] == ["url"]
    assert tools["device_health"].annotations.read_only_hint is True
    assert not (draw.annotations and draw.annotations.read_only_hint)


def test_a_call_runs_the_command_on_the_only_online_gadget(state):
    with mcp_host(state) as host:
        link = register(host.hub)
        result = call(host, "device_health", {})
    assert not result.is_error
    assert json.loads(text_of(result)) == {"ok": True, "payload": {"overall": "ok"}, "error": None}
    assert link.calls == [("device.health", {}, 5.0)]


def test_arguments_are_checked_before_anything_is_sent(state):
    with mcp_host(state) as host:
        link = register(host.hub)
        wrong_type = call(host, "display_draw_url", {"url": 42})
        missing = call(host, "display_draw_url", {})
        extra = call(host, "display_draw_url", {"url": "https://x/y.png", "volume": 3})
    for result in (wrong_type, missing, extra):
        assert result.is_error
    assert "url" in text_of(wrong_type) and "url" in text_of(missing)
    assert link.calls == []


def test_a_gadget_can_be_named_and_must_be_when_several_are_online(state):
    with mcp_host(state) as host:
        first = register(host.hub, "homelink-111111")
        second = register(host.hub, "homelink-222222")
        ambiguous = call(host, "device_health", {})
        named = call(host, "device_health", {"gadget": "homelink-222222"})
    assert ambiguous.is_error and "homelink-111111" in text_of(ambiguous)
    assert not named.is_error
    assert first.calls == [] and [c[0] for c in second.calls] == ["device.health"]


def test_offline_unknown_and_slow_gadgets_are_errors(state):
    from musehost.hub import InvokeTimeout

    with mcp_host(state) as host:
        link = register(host.hub)
        host.hub.unregister("homelink-abcdef", link)
        offline = call(host, "device_health", {"gadget": "homelink-abcdef"})
        unknown = call(host, "device_health", {"gadget": "homelink-nope00"})
        register(host.hub, link=FakeLink({"device.health": InvokeTimeout()}))
        slow = call(host, "device_health", {})
        failed_link = FakeLink({"device.health": InvokeResult(ok=False, error="busy")})
        register(host.hub, link=failed_link)
        failed = call(host, "device_health", {})
    assert offline.is_error and "offline" in text_of(offline)
    assert unknown.is_error and "homelink-nope00" in text_of(unknown)
    assert slow.is_error and "in time" in text_of(slow)
    assert failed.is_error and "busy" in text_of(failed)


def test_the_tool_list_follows_gadgets(state):
    with mcp_host(state) as host:

        async def scenario(client):
            before = [t.name for t in (await client.list_tools()).tools]
            link = register(host.hub)
            during = [t.name for t in (await client.list_tools()).tools]
            host.hub.unregister("homelink-abcdef", link)
            after = [t.name for t in (await client.list_tools()).tools]
            return before, during, after

        before, during, after = run(with_client(host.url, host.token, scenario))
    assert before == ["list_gadgets"]
    assert "device_health" in during
    # Offline gadgets keep their tools listed; calling them says they're offline.
    assert "device_health" in after


def test_current_protocol_clients_hear_of_changes_by_listening(state):
    with mcp_host(state) as host:

        async def scenario(client):
            async with client.listen(tools_list_changed=True) as sub:
                register(host.hub)
                async with asyncio.timeout(5):
                    async for event in sub:
                        return type(event).__name__

        event = run(with_client(host.url, host.token, scenario))
    assert event == "ToolsListChanged"


def test_older_protocol_clients_are_notified_on_their_session(state):
    notices = []

    async def on_message(message):
        root = getattr(message, "root", message)
        if getattr(root, "method", "") == "notifications/tools/list_changed":
            notices.append(root)

    with mcp_host(state) as host:

        async def scenario():
            headers = {"Authorization": f"Bearer {host.token}"}
            async with httpx2.AsyncClient(headers=headers) as http:
                transport = streamable_http_client(host.url, http_client=http)
                async with Client(transport, mode="legacy", message_handler=on_message) as client:
                    await client.list_tools()
                    register(host.hub)
                    for _ in range(100):
                        if notices:
                            return
                        await asyncio.sleep(0.05)

        run(scenario())
    assert notices, "no notifications/tools/list_changed on the legacy session"


def test_arguments_and_results_stay_out_of_the_logs(state, caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    secret_url = "https://private.example/kids-photo.png"
    with mcp_host(state) as host:
        register(
            host.hub,
            link=FakeLink(
                {
                    "display.draw_url": InvokeResult(ok=True, payload={"shown": "SECRET-PAYLOAD"}),
                }
            ),
        )
        result = call(host, "display_draw_url", {"url": secret_url})
    assert not result.is_error
    # The test's own MCP client logs what it sends; musehost's side must not.
    server_side = "\n".join(
        r.getMessage() for r in caplog.records if not r.name.startswith(("mcp.client", "http"))
    )
    assert secret_url not in server_side and "SECRET-PAYLOAD" not in server_side
    assert "display_draw_url" in caplog.text or "display.draw_url" in caplog.text
