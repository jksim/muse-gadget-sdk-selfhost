"""No token, grant or private key ever reaches a log line."""

import json
import logging

from conftest import serving
from musegadget import muse_api, tls

from musehost.cli import main
from musehost.store import Store
from musehost.tokens import Tokens


def test_every_flow_logs_no_secrets(state, caplog, capfd):
    caplog.set_level(logging.DEBUG)
    out = state.parent / "pairing.json"
    main(["--state-dir", str(state), "-v", "enroll", "homelink-abcdef", "--out", str(out)])
    pairing = json.loads(out.read_text())
    grant = Tokens(Store.open(state / "musehost.db")).create_grant()
    secrets = [pairing["access_token"], pairing["refresh_token"], grant]
    secrets += [
        line
        for name in ("ca.key", "server.key", "noise_static.key")
        for line in (state / name).read_text().splitlines()
        if "PRIVATE KEY" not in line
    ]

    context = tls.context_for(pairing["ca_cert"])
    root = muse_api.api_root(pairing["api_url_v2"])
    with serving(state):
        vms, _ = muse_api.fetch_vms_with_status(pairing["access_token"], root, context=context)
        secrets.append(vms[0]["vm_auth_token"])
        rotated, _ = muse_api.refresh_device_token(
            pairing["refresh_token"], "homelink-abcdef", root, context=context
        )
        secrets += [rotated["access_token"], rotated["refresh_token"]]
        # Reuse after the new pair is used revokes, and logs that it did.
        muse_api.fetch_vms_with_status(rotated["access_token"], root, context=context)
        muse_api.refresh_device_token(
            pairing["refresh_token"], "homelink-abcdef", root, context=context
        )
    main(["--state-dir", str(state), "-v", "devices", "list"])

    captured = capfd.readouterr()
    logs = caplog.text + captured.err
    assert "revoking the device" in logs  # the logs were really captured
    leaked = [secret for secret in secrets if secret in logs]
    assert leaked == []


def test_noise_link_flows_log_no_secrets(live, state, caplog, capfd):
    import asyncio
    import base64

    from noise_client import NoiseClient
    from test_chat import NOTE_HEAD, NOTE_TAIL, firmware_wav, post, subscribe, turn_events
    from test_link import link_session, on_server, wait_for

    caplog.set_level(logging.DEBUG)
    access, refresh = live.tokens.enroll("homelink-abcdef", "pi")
    vm_token = live.vm_token()
    note = base64.b64encode(firmware_wav(0.5))

    async def scenario():
        client = await NoiseClient.connect(live.port, live.ca, vm_token)
        try:
            sub = await subscribe(client)
            await post(client, {"message": "the secret word is swordfish"})
            await turn_events(client, sub)
            sid = await client.request("POST", "/chat/stream", NOTE_HEAD + note + NOTE_TAIL)
            await client.response(sid)
            await turn_events(client, sub)
        finally:
            await client.close()
        session = link_session(live, run_command=lambda *a: {"ok": True, "payload": "fine"})
        stop = asyncio.Event()
        task = asyncio.ensure_future(session.run(stop))
        await wait_for(lambda: session.registered_at is not None)
        await asyncio.to_thread(
            lambda: on_server(live, live.app.state.hub.invoke("homelink-abcdef", "device.health"))
        )
        stop.set()
        await asyncio.wait_for(task, 5)

    asyncio.run(scenario())
    # Only what the host logs: the test's own client (websockets.client, also
    # used by the SDK's LinkSession) logs request headers at DEBUG.
    host_records = [r for r in caplog.records if not r.name.startswith("websockets.client")]
    logs = "\n".join(r.getMessage() for r in host_records) + capfd.readouterr().err
    assert "registered" in logs and "voice note" in logs  # the flows really ran and logged
    secrets = [access, refresh, vm_token, note[100:160].decode(), "swordfish"]
    secrets += [
        line
        for line in (state / "noise_static.key").read_text().splitlines()
        if "PRIVATE KEY" not in line
    ]
    assert [s for s in secrets if s in logs] == []


def test_transcripts_never_reach_the_logs(live, caplog, capfd):
    from test_chat import FakeEngine, use_transcriber, voice_turn

    caplog.set_level(logging.DEBUG)
    use_transcriber(live, FakeEngine("the vault code is 4711"))
    events = voice_turn(live)
    assert "4711" in events[-2]["payload"]["display_text"]  # it was transcribed
    host = [r for r in caplog.records if not r.name.startswith("websockets.client")]
    logs = "\n".join(r.getMessage() for r in host) + capfd.readouterr().err
    assert "transcribed a note" in logs and "4711" not in logs


def test_brain_turns_log_no_content_tool_data_or_keys(caplog):
    import asyncio

    from fake_llm import claude_message, fake_server
    from test_brain import CONFIG
    from test_brain_tools import hub_with_cores3

    from musehost.brain import Brain
    from musehost.brain.providers.claude import ClaudeProvider
    from musehost.hub import ChatTurn, InvokeResult

    caplog.set_level(logging.DEBUG)

    async def secret_payload(command, params):
        return InvokeResult(ok=True, payload={"wifi_password": "hunter2"})

    hub = hub_with_cores3(invoke=secret_payload)
    script = [
        claude_message([("tool_use", "toolu_1", "device_health", ["{}"])], stop_reason="tool_use"),
        claude_message([("text", "All good, swordfish.", ["All good, swordfish."])]),
    ]
    with fake_server(script) as (url, _):
        provider = ClaudeProvider(api_key="sk-ant-SECRET-KEY", base_url=url, max_retries=0)
        brain = Brain(hub, provider, CONFIG)
        turn = ChatTurn(node_id="homelink-4d6734", message_id="u", text="the vault code is 4711")

        async def run():
            return "".join([p async for p in brain(turn)])

        assert "swordfish" in asyncio.run(run())
    host = [r for r in caplog.records if not r.name.startswith(("httpx", "httpcore", "anthropic"))]
    logs = "\n".join(r.getMessage() for r in host)
    assert "device.health" in logs and "answered" in logs
    for secret in ("hunter2", "4711", "swordfish", "sk-ant-SECRET-KEY"):
        assert secret not in logs, secret
