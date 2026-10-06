"""The SDK's own Linux client (musegadget.link_client.LinkSession) against the host."""

import asyncio
import base64
import time

from musegadget import tls
from musegadget.link_client import DeviceDescription, LinkSession, Outcome

DEVICE = DeviceDescription(
    node_id="homelink-abcdef",
    display_name="pi",
    version="0.1.0",
    commands={"device.health": {"description": "health", "required": {}, "optional": {}}},
)


def link_session(live, run_command=None, **kwargs) -> LinkSession:
    pin = base64.urlsafe_b64decode(live.app.state.noise_static_pub + "=")
    return LinkSession(
        noise_host=f"localhost:{live.port}",
        vm_id="home",
        vm_auth_token=live.vm_token(),
        device=DEVICE,
        run_command=run_command or (lambda command, params, timeout_ms: {"ok": True}),
        ssl_context=tls.context_for(live.ca.read_text()),
        noise_static_pub=pin,
        **kwargs,
    )


def hub_device(live, node_id="homelink-abcdef"):
    return next((d for d in live.app.state.hub.devices() if d.node_id == node_id), None)


async def wait_for(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.02)


def test_the_linux_client_registers_and_shows_online_until_it_leaves(live):
    async def scenario():
        session = link_session(live)
        stop = asyncio.Event()
        task = asyncio.ensure_future(session.run(stop))
        await wait_for(lambda: session.registered_at is not None)
        device = hub_device(live)
        assert device.online and device.platform == "linux"
        assert "device.health" in device.commands
        stop.set()
        assert await asyncio.wait_for(task, 5) is Outcome.STOPPED
        await wait_for(lambda: not hub_device(live).online)

    asyncio.run(scenario())


# -- Hub.invoke ----------------------------------------------------------------------

import pytest  # noqa: E402

from musehost.hub import DeviceOffline, InvokeTimeout  # noqa: E402


def on_server(live, coro, timeout=10):
    """Run ``coro`` on the server's event loop, where the hub lives."""
    return asyncio.run_coroutine_threadsafe(coro, live.app.state.loop).result(timeout)


def with_device(live, run_command, check):
    async def scenario():
        session = link_session(live, run_command=run_command)
        stop = asyncio.Event()
        task = asyncio.ensure_future(session.run(stop))
        await wait_for(lambda: session.registered_at is not None)
        try:
            return await asyncio.to_thread(check)
        finally:
            stop.set()
            await asyncio.wait_for(task, 5)

    return asyncio.run(scenario())


def test_invoke_returns_the_devices_payload(live):
    def run_command(command, params, timeout_ms):
        return {"ok": True, "payload": {"command": command, "params": params}}

    result = with_device(
        live,
        run_command,
        lambda: on_server(
            live, live.app.state.hub.invoke("homelink-abcdef", "device.health", {"x": 1})
        ),
    )
    assert result.ok and result.payload == {"command": "device.health", "params": {"x": 1}}
    assert result.error is None


def test_a_failed_command_comes_back_as_not_ok_with_its_error(live):
    result = with_device(
        live,
        lambda *args: {"ok": False, "error": "boom"},
        lambda: on_server(live, live.app.state.hub.invoke("homelink-abcdef", "system.run", {})),
    )
    assert (result.ok, result.error) == (False, "boom")


def test_invoking_an_offline_node_raises_device_offline(live):
    with pytest.raises(DeviceOffline):
        on_server(live, live.app.state.hub.invoke("homelink-123456", "device.health", {}))


def test_a_device_that_never_answers_times_out(live):
    def slow(command, params, timeout_ms):
        time.sleep(2)
        return {"ok": True}

    with pytest.raises(InvokeTimeout):
        with_device(
            live,
            slow,
            lambda: on_server(
                live, live.app.state.hub.invoke("homelink-abcdef", "x", {}, timeout_s=0.3)
            ),
        )


def test_concurrent_invokes_each_get_their_own_result(live):
    def run_command(command, params, timeout_ms):
        time.sleep(0.2 if params["n"] == 1 else 0)
        return {"ok": True, "payload": params["n"]}

    async def both():
        hub = live.app.state.hub
        return await asyncio.gather(
            hub.invoke("homelink-abcdef", "echo", {"n": 1}),
            hub.invoke("homelink-abcdef", "echo", {"n": 2}),
        )

    first, second = with_device(live, run_command, lambda: on_server(live, both()))
    assert (first.payload, second.payload) == (1, 2)


# -- Unpair --------------------------------------------------------------------------


def test_revoking_a_connected_device_unpairs_it_first(live, state, capsys):
    from musehost.cli import main

    async def scenario():
        session = link_session(live)
        task = asyncio.ensure_future(session.run(asyncio.Event()))
        await wait_for(lambda: session.registered_at is not None)
        code = await asyncio.to_thread(
            main, ["--state-dir", str(state), "devices", "revoke", "homelink-abcdef"]
        )
        return code, await asyncio.wait_for(task, 5)

    code, outcome = asyncio.run(scenario())
    assert code == 0 and outcome is Outcome.UNPAIRED
    assert "notified" in capsys.readouterr().out
    row = live.tokens.store.db.execute(
        "SELECT revoked_at FROM devices WHERE node_id = 'homelink-abcdef'"
    ).fetchone()
    assert row["revoked_at"] is not None


def test_unpairing_an_offline_device_reports_false(live):
    assert on_server(live, live.app.state.hub.unpair("homelink-123456")) is False
