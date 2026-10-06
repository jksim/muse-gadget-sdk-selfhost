"""A minimal Noise client for tests, built from the SDK's own initiator code.

It speaks the wire protocol the way the gadgets do: WebSocket upgrade with a
VM bearer, Noise XX as initiator, then HTTP-style streams in service frames.
"""

from __future__ import annotations

import asyncio
import json
import ssl
import struct

from musegadget.link_client import MessageDecoder
from musegadget.noise import Header, NoiseTransport, NoiseXXInitiator
from websockets.asyncio.client import connect


class NoiseClient:
    def __init__(self, ws, initiator: NoiseXXInitiator, transport: NoiseTransport) -> None:
        self.ws = ws
        self.remote_static = initiator.remote_static_public_key()
        self.transport = transport
        self._pending: dict[int, list] = {}
        self._decoders: dict[int, MessageDecoder] = {}
        self._messages: dict[int, list] = {}

    @classmethod
    async def connect(
        cls, port: int, ca_pem_path, token: str, vm_id: str = "home", host: str = "localhost"
    ) -> NoiseClient:
        context = ssl.create_default_context(cafile=str(ca_pem_path))
        ws = await connect(
            f"wss://{host}:{port}/v1/noise?vm_id={vm_id}",
            additional_headers={"Authorization": f"Bearer {token}"},
            ssl=context,
            max_size=None,
        )
        initiator = NoiseXXInitiator()
        initiator.initialize()
        await ws.send(initiator.write_message1())
        initiator.read_message2(await ws.recv())
        remote = initiator.remote_static_public_key()
        await ws.send(initiator.write_message3())
        send, recv = initiator.split()
        client = cls(ws, initiator, NoiseTransport(send, recv))
        client.remote_static = remote
        return client

    async def close(self) -> None:
        await self.ws.close()

    # -- Sending ------------------------------------------------------------------

    async def request(
        self, verb: str, path: str, body: bytes = b"", end_body: bool = True, headers=None
    ) -> int:
        hdrs = [Header(k, v) for k, v in (headers or {}).items()]
        if end_body:
            encrypted = self.transport.encrypt_http_request(verb, path, body, headers=hdrs)
        else:
            encrypted = self.transport.start_stream_request(verb, path, headers=hdrs)
        for frame in encrypted.frames:
            await self.ws.send(frame)
        if body and not end_body:
            await self.send_chunk(encrypted.stream_id, body)
        return encrypted.stream_id

    async def send_chunk(self, stream_id: int, data: bytes, end_body: bool = False) -> None:
        for frame in self.transport.encrypt_body_chunk(stream_id, data, end_body=end_body):
            await self.ws.send(frame)

    async def send_message(self, stream_id: int, message: dict) -> None:
        """A u32-LE length-prefixed JSON message, as on /link-control."""
        data = json.dumps(message, separators=(",", ":")).encode()
        await self.send_chunk(stream_id, struct.pack("<I", len(data)) + data)

    # -- Receiving ----------------------------------------------------------------

    async def next_frame(self, stream_id: int, timeout: float = 5.0):
        """The next frame for ``stream_id``, buffering frames for other streams."""
        async with asyncio.timeout(timeout):
            while True:
                queued = self._pending.get(stream_id)
                if queued:
                    return queued.pop(0)
                raw = await self.ws.recv()
                frame = self.transport.decrypt_frame(bytes(raw))
                if frame is not None:
                    self._pending.setdefault(frame.stream_id, []).append(frame)

    async def response(self, stream_id: int, timeout: float = 5.0) -> tuple[int, bytes]:
        """Status and full body of a stream that ends."""
        status, body = 0, bytearray()
        while True:
            frame = await self.next_frame(stream_id, timeout)
            if frame.kind == "reset":
                raise ConnectionError(f"stream reset: {frame.value.reason}")
            if frame.kind == "response":
                status = frame.value.status
            body += frame.value.body if frame.kind == "response" else frame.value.data
            if frame.value.end_body:
                return status, bytes(body)

    async def read_message(self, stream_id: int, timeout: float = 5.0) -> dict:
        """The next length-prefixed JSON message on a /link-control stream."""
        decoder = self._decoders.setdefault(stream_id, MessageDecoder())
        queued = self._messages.setdefault(stream_id, [])
        while not queued:
            frame = await self.next_frame(stream_id, timeout)
            if frame.kind == "reset":
                raise ConnectionError(f"stream reset: {frame.value.reason}")
            data = frame.value.body if frame.kind == "response" else frame.value.data
            queued.extend(decoder.feed(data))
        return queued.pop(0)

    async def read_lines(self, stream_id: int, count: int, timeout: float = 5.0) -> list[dict]:
        """The next ``count`` NDJSON objects on a streaming response (e.g. /chat/subscribe)."""
        buffer = self.__dict__.setdefault("_line_buffers", {}).setdefault(stream_id, bytearray())
        lines: list[dict] = []
        while len(lines) < count:
            while b"\n" in buffer and len(lines) < count:
                line, _, rest = bytes(buffer).partition(b"\n")
                buffer[:] = rest
                if line.strip():
                    lines.append(json.loads(line))
            if len(lines) >= count:
                break
            frame = await self.next_frame(stream_id, timeout)
            if frame.kind == "reset":
                raise ConnectionError(f"stream reset: {frame.value.reason}")
            if frame.kind == "response":
                assert frame.value.status == 200, frame.value.status
            buffer += frame.value.body if frame.kind == "response" else frame.value.data
        return lines
