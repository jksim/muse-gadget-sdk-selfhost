"""Flows shaped exactly like the ESP32 firmware's (noise_control.cpp, muse_chat_session.cpp)."""

import asyncio

from noise_client import NoiseClient

REGISTER = {
    "type": "req",
    "id": "3f0c-register",
    "method": "link.register",
    "params": {
        "node_id": "homelink-abcdef",
        "display_name": "MuseGadget-ABCDEF",
        "platform": "esp32",
        "version": "999.0.0",
        "device_family": "link",
        "model_id": "esp-link",
        "is_wakeup_supported": False,
        "metadata": {"network_ssid": "localnet"},
        "commands_v2": {"device.health": {"description": "health", "required": {}, "optional": {}}},
    },
}


async def connect(live) -> NoiseClient:
    return await NoiseClient.connect(live.port, live.ca, live.vm_token())


async def open_control(client: NoiseClient) -> int:
    sid = await client.request("POST", "/link-control", end_body=False)
    frame = await client.next_frame(sid)
    assert (frame.kind, frame.value.status, frame.value.end_body) == ("response", 200, False)
    return sid


def test_register_gets_exactly_the_reply_the_firmware_checks(live):
    async def scenario():
        client = await connect(live)
        try:
            sid = await open_control(client)
            await client.send_message(sid, REGISTER)
            return await client.read_message(sid)
        finally:
            await client.close()

    reply = asyncio.run(scenario())
    assert reply == {"type": "res", "id": "3f0c-register", "result": {"status": "registered"}}


def test_a_heartbeat_is_recorded_without_a_reply(live):
    async def scenario():
        client = await connect(live)
        try:
            sid = await open_control(client)
            await client.send_message(sid, REGISTER)
            await client.read_message(sid)
            await client.send_message(sid, {"method": "link.heartbeat"})
            for _ in range(100):
                device = live.app.state.hub.devices()[0]
                if device.last_heartbeat:
                    return device
                await asyncio.sleep(0.02)
            return None
        finally:
            await client.close()

    device = asyncio.run(scenario())
    assert device is not None and device.platform == "esp32"


def test_registering_as_another_node_is_rejected(live):
    async def scenario():
        client = await connect(live)
        try:
            sid = await open_control(client)
            other = {**REGISTER, "params": {**REGISTER["params"], "node_id": "homelink-123456"}}
            await client.send_message(sid, other)
            return await client.read_message(sid)
        finally:
            await client.close()

    reply = asyncio.run(scenario())
    assert reply["id"] == "3f0c-register" and reply.get("error")
    assert "result" not in reply


def test_the_tunnel_is_refused(live):
    async def scenario():
        client = await connect(live)
        try:
            return await client.response(
                await client.request("POST", "/link-tunnel", end_body=False)
            )
        finally:
            await client.close()

    status, _ = asyncio.run(scenario())
    assert status == 404
