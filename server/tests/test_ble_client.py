"""BleLink against a fake bleak client (no radio)."""

import asyncio

import pytest

from musehost import ble_client
from musehost.pair import PairingFailed


class FakeBleakClient:
    """Calls the disconnect callback for each failed connect retry, as bleak does."""

    retries = 0

    def __init__(self, address, disconnected_callback=None, **kwargs):
        self._disconnected = disconnected_callback
        self.is_connected = False
        self.mtu_size = 256
        self.notify = None

    async def connect(self):
        for _ in range(FakeBleakClient.retries):
            self._disconnected(self)  # "retry due to le-connection-abort-by-local"
        self.is_connected = True

    async def start_notify(self, uuid, callback):
        self.notify = callback

    async def write_gatt_char(self, uuid, data, response=None):
        self.notify(None, bytearray(b"pong"))

    async def disconnect(self):
        self.is_connected = False
        self._disconnected(self)


@pytest.fixture
def fake_bleak(monkeypatch):
    monkeypatch.setattr(ble_client, "BleakClient", FakeBleakClient)
    FakeBleakClient.retries = 0
    return FakeBleakClient


def test_disconnects_during_connect_retries_are_not_reported_later(fake_bleak):
    fake_bleak.retries = 6

    async def scenario():
        async with ble_client.BleLink("80:45:6B:4D:67:36") as link:
            await link.write(b"ping")
            return await asyncio.wait_for(link.read(), 1)

    assert asyncio.run(scenario()) == b"pong"


def test_a_disconnect_after_connecting_ends_reads(fake_bleak):
    async def scenario():
        async with ble_client.BleLink("80:45:6B:4D:67:36") as link:
            link._client._disconnected(link._client)
            with pytest.raises(PairingFailed, match="disconnected"):
                await asyncio.wait_for(link.read(), 1)

    asyncio.run(scenario())
