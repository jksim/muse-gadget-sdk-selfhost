"""The /v1/noise endpoint: upgrade auth, handshake, and stream handling."""

import asyncio
import json
import socket
import ssl
import time

import pytest
from cryptography.hazmat.primitives import serialization
from noise_client import NoiseClient
from websockets.exceptions import InvalidStatus

from musehost import pki


def run(coro):
    return asyncio.run(coro)


def test_identity_names_clio_over_noise_with_the_hosts_static_key(live, state):
    async def scenario():
        client = await NoiseClient.connect(live.port, live.ca, live.vm_token())
        try:
            sid = await client.request("GET", "/identity")
            return client.remote_static, await client.response(sid)
        finally:
            await client.close()

    remote, (status, body) = run(scenario())
    key = pki.load_noise_key(state / "noise_static.key")
    assert remote == key.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw
    )
    assert status == 200
    assert json.loads(body) == {"ok": True, "result": {"name": "Clio"}}


def test_an_unknown_path_is_404(live):
    async def scenario():
        client = await NoiseClient.connect(live.port, live.ca, live.vm_token())
        try:
            return await client.response(await client.request("GET", "/nope"))
        finally:
            await client.close()

    status, _ = run(scenario())
    assert status == 404


def upgrade_status(live, token: str, vm_id: str = "home") -> int:
    async def scenario():
        try:
            client = await NoiseClient.connect(live.port, live.ca, token, vm_id=vm_id)
        except InvalidStatus as exc:
            return exc.response.status_code
        await client.close()
        return 101

    return run(scenario())


def test_a_bad_vm_bearer_is_401(live):
    assert upgrade_status(live, "not-a-token") == 401


def test_an_expired_vm_bearer_is_401(live):
    from musehost.tokens import Tokens

    live.vm_token()  # enroll
    stale = Tokens(live.tokens.store, clock=lambda: time.time() - 3600)
    assert upgrade_status(live, stale.issue_vm_token("homelink-abcdef", "home")) == 401


def test_another_vms_id_is_403(live):
    assert upgrade_status(live, live.vm_token(), vm_id="den") == 403


def test_the_firmwares_hand_built_upgrade_gets_101(live):
    # Byte for byte what esp32/main/noise_upgrade.h sends.
    token = live.vm_token()
    request = (
        "GET /v1/noise?vm_id=home HTTP/1.1\r\n"
        f"Host: localhost\r\n"
        f"Authorization: Bearer {token}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\n"
        "\r\n"
    ).encode()
    context = ssl.create_default_context(cafile=str(live.ca))
    with socket.create_connection(("localhost", live.port), timeout=5) as raw:
        with context.wrap_socket(raw, server_hostname="localhost") as tls:
            tls.sendall(request)
            head = tls.recv(4096).decode()
    assert head.startswith("HTTP/1.1 101")
    assert "s3pPLMBiTxaQ9kYGzzhZRbK+xOo=" in head  # the RFC 6455 accept for that key


@pytest.mark.parametrize("name", ["Clio", "Ada"])
def test_the_agent_name_comes_from_host_toml(state, name):
    from musehost.config import HostConfig

    config = HostConfig.load(state / "host.toml")
    assert config.agent_name == "Clio"
    import dataclasses

    dataclasses.replace(config, agent_name=name).save(state / "host.toml")
    assert HostConfig.load(state / "host.toml").agent_name == name


# -- Limits and revocation ------------------------------------------------------------

from musehost import noise_server  # noqa: E402


async def identity_works(client) -> bool:
    status, body = await client.response(await client.request("GET", "/identity"))
    return status == 200


def test_too_many_open_streams_are_refused_but_the_session_lives_on(live):
    async def scenario():
        client = await NoiseClient.connect(live.port, live.ca, live.vm_token())
        try:
            for _ in range(noise_server.MAX_STREAMS):
                sid = await client.request("POST", "/chat/subscribe", b"{}")
                await client.read_lines(sid, 1)
            refused = await client.request("POST", "/chat/subscribe", b"{}")
            frame = await client.next_frame(refused)
            return frame.kind
        finally:
            await client.close()

    assert run(scenario()) == "reset"


def test_a_fifth_session_for_one_node_is_refused(live):
    async def scenario():
        token = live.vm_token()
        clients = [
            await NoiseClient.connect(live.port, live.ca, token)
            for _ in range(noise_server.MAX_SESSIONS_PER_NODE)
        ]
        try:
            try:
                extra = await NoiseClient.connect(live.port, live.ca, token)
            except InvalidStatus as exc:
                return exc.response.status_code, await identity_works(clients[0])
            await extra.close()
            return 101, True
        finally:
            for c in clients:
                await c.close()

    status, still_fine = run(scenario())
    assert status == 429 and still_fine


def test_an_oversized_control_message_resets_that_stream_only(live):
    async def scenario():
        client = await NoiseClient.connect(live.port, live.ca, live.vm_token())
        try:
            sid = await client.request("POST", "/link-control", end_body=False)
            await client.next_frame(sid)
            import struct as _struct

            await client.send_chunk(sid, _struct.pack("<I", 5 * 1024 * 1024) + b"x" * 1024)
            kind = (await client.next_frame(sid)).kind
            return kind, await identity_works(client)
        finally:
            await client.close()

    assert run(scenario()) == ("reset", True)


def test_unread_bytes_piling_up_on_a_stream_reset_it(live):
    async def scenario():
        client = await NoiseClient.connect(live.port, live.ca, live.vm_token())
        try:
            # A subscription never reads its request body; keep feeding it.
            sid = await client.request("POST", "/chat/subscribe", end_body=False)
            await client.read_lines(sid, 1)
            for _ in range(6):
                await client.send_chunk(sid, b"y" * (1024 * 1024))
            while (frame := await client.next_frame(sid)).kind != "reset":
                pass
            return frame.kind, await identity_works(client)
        finally:
            await client.close()

    assert run(scenario()) == ("reset", True)


def test_a_device_revoked_behind_the_servers_back_is_dropped_on_its_next_frame(live, monkeypatch):
    monkeypatch.setattr(noise_server, "REVOKE_CHECK_S", 0)

    async def scenario():
        client = await NoiseClient.connect(live.port, live.ca, live.vm_token())
        try:
            assert await identity_works(client)
            live.tokens.revoke("homelink-abcdef")
            await client.request("GET", "/identity")
            await asyncio.wait_for(client.ws.wait_closed(), 5)
            return True
        finally:
            await client.close()

    assert run(scenario())


def test_refresh_token_reuse_unpairs_a_connected_device(live):
    from musegadget.link_client import Outcome
    from test_link import link_session, wait_for

    async def scenario():
        _, refresh = live.tokens.enroll("homelink-abcdef", "pi")
        session = link_session(live)
        task = asyncio.ensure_future(session.run(asyncio.Event()))
        await wait_for(lambda: session.registered_at is not None)
        access2, _ = live.tokens.refresh(refresh, "homelink-abcdef")
        live.tokens.device_for_access(access2)
        # The stolen old refresh token comes back through the REST API.
        import http.client
        import json as _json

        def reuse():
            conn = http.client.HTTPSConnection(
                "localhost", live.port, context=ssl.create_default_context(cafile=str(live.ca))
            )
            conn.request(
                "POST",
                "/device_token/refresh",
                _json.dumps({"device_id": "homelink-abcdef"}),
                {"Authorization": f"Bearer hatch_refresh:{refresh}"},
            )
            status = conn.getresponse().status
            conn.close()
            return status

        assert await asyncio.to_thread(reuse) == 401
        return await asyncio.wait_for(task, 5)

    assert run(scenario()) is Outcome.UNPAIRED
