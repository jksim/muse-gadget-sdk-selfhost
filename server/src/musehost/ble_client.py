"""Bluetooth LE access to a gadget's setup service, through bleak (BlueZ on the Pi)."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from bleak import BleakClient, BleakScanner

from musehost.pair import PairingFailed

log = logging.getLogger(__name__)

# From esp32/main/ble_server.c (the Linux SDK uses the same).
SERVICE_UUID = "7fdd3d1c-38ea-46cf-8b46-314ecf5f240c"
RX_UUID = "4d593029-28a2-4a6e-a1f0-3c2d5e8f9b01"  # phone -> gadget, write
TX_UUID = "d75dc4ca-7b2b-4e9c-8f0a-1d2e3f4a5b6c"  # gadget -> phone, notify
NAME_PREFIX = "MuseGadget"


@dataclass(frozen=True)
class Gadget:
    name: str
    address: str
    rssi: int


async def scan(timeout: float = 8.0) -> list[Gadget]:
    """Gadgets advertising the setup service, strongest signal first."""
    found = await BleakScanner.discover(
        timeout=timeout, service_uuids=[SERVICE_UUID], return_adv=True
    )
    gadgets = [
        Gadget(name=adv.local_name or device.name or "", address=device.address, rssi=adv.rssi)
        for device, adv in found.values()
    ]
    return sorted(gadgets, key=lambda g: g.rssi, reverse=True)


class BleLink:
    """A connection to one gadget, as a :class:`musehost.pair.Link`."""

    def __init__(self, address: str) -> None:
        self._client = BleakClient(address, disconnected_callback=self._on_disconnect)
        self._notifications: asyncio.Queue[bytes | None] = asyncio.Queue()
        self._connected = False
        self.mtu = 23

    async def __aenter__(self) -> BleLink:
        await self._client.connect()
        # bleak reports a disconnect for every failed connect retry; only the
        # ones after this point mean the gadget went away.
        self._connected = True
        backend = getattr(self._client, "_backend", None)
        acquire = getattr(backend, "_acquire_mtu", None)  # BlueZ only reports MTU when asked
        if acquire is not None:
            try:
                await acquire()
            except Exception as exc:  # the default MTU still works, just slower
                log.debug("MTU exchange failed: %s", exc)
        self.mtu = self._client.mtu_size
        log.info("connected, MTU %d", self.mtu)
        await self._client.start_notify(TX_UUID, self._on_notify)
        return self

    async def __aexit__(self, *exc) -> None:
        if self._client.is_connected:
            await self._client.disconnect()

    async def write(self, packet: bytes) -> None:
        await self._client.write_gatt_char(RX_UUID, packet, response=True)

    async def read(self) -> bytes:
        packet = await self._notifications.get()
        if packet is None:
            raise PairingFailed("the gadget disconnected")
        return packet

    def _on_notify(self, _characteristic, data: bytearray) -> None:
        self._notifications.put_nowait(bytes(data))

    def _on_disconnect(self, _client) -> None:
        if self._connected:
            self._connected = False
            self._notifications.put_nowait(None)
