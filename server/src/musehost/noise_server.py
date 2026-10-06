"""The ``/v1/noise`` endpoint: the encrypted session gadgets keep with the host.

After a WebSocket upgrade authorised by a VM bearer from ``/fetch_vms``, the
gadget runs Noise XX as initiator and the host answers as responder with its
static key (the one Linux gadgets pin). Every WebSocket message after that is
one Noise ciphertext of a framed chunk; reassembled chunks are service-frame
envelopes carrying HTTP-style streams: a request, a response, then body
chunks either way, all tagged with a stream id. This module is the server
side of ``musegadget.noise.transport``; stream handlers (``link``, ``chat``)
sit on top.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from musegadget.noise import (
    ApplicationResponse,
    BodyChunk,
    Header,
    NoiseFrameDecoder,
    NoiseXXResponder,
    Reset,
    ResetCode,
    ServiceFrame,
    encode_noise_frames,
)
from musegadget.noise.transport import decode_request_envelope, encode_response_envelope
from starlette.responses import Response
from starlette.websockets import WebSocket, WebSocketDisconnect

log = logging.getLogger(__name__)

HANDSHAKE_TIMEOUT_S = 20
MAX_STREAMS = 16  # open streams per session
MAX_SESSIONS_PER_NODE = 4  # the ESP32 uses two: Link and chat
MAX_STREAM_BUFFER = 4 * 1024 * 1024  # request body not yet read by its handler
REVOKE_CHECK_S = 30  # how often a live session re-checks its device isn't revoked
EMPTY_AD = b""


@dataclass(eq=False)  # identity, so streams can live in sets
class Stream:
    """One request stream inside a session, as a handler sees it."""

    session: NoiseSession
    stream_id: int
    verb: str
    path: str
    headers: dict[str, str]
    _body: asyncio.Queue[bytes | None] = field(default_factory=asyncio.Queue)
    # Set when the device resets the stream or the session ends, as opposed
    # to the request body merely ending.
    gone: asyncio.Event = field(default_factory=asyncio.Event)
    responded: bool = False
    ended_by_us: bool = False
    buffered: int = 0

    def feed(self, data: bytes, end: bool) -> None:
        if data:
            self.buffered += len(data)
            self._body.put_nowait(data)
        if end:
            self._body.put_nowait(None)

    async def chunks(self):
        """The request body as it arrives; ends when the device ends it."""
        while (data := await self._body.get()) is not None:
            self.buffered -= len(data)
            yield data

    async def read_body(self, limit: int) -> bytes:
        body = bytearray()
        async for data in self.chunks():
            body += data
            if len(body) > limit:
                raise ValueError(f"request body over {limit} bytes")
        return bytes(body)

    async def respond(
        self,
        status: int,
        body: bytes = b"",
        end: bool = True,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.responded = True
        self.ended_by_us = end
        response = ApplicationResponse(
            status=status,
            headers=[Header(k, v) for k, v in (headers or {}).items()],
            body=body,
            end_body=end,
        )
        await self.session.send_frame(ServiceFrame.response(self.stream_id, response))

    async def respond_json(self, status: int, obj: dict) -> None:
        body = json.dumps(obj, separators=(",", ":")).encode()
        await self.respond(status, body, headers={"content-type": "application/json"})

    async def send(self, data: bytes, end: bool = False) -> None:
        self.ended_by_us = end
        chunk = BodyChunk(data=data, end_body=end)
        await self.session.send_frame(ServiceFrame.body_chunk(self.stream_id, chunk))

    async def reset(self, reason: str, code: ResetCode = ResetCode.CANCELLED) -> None:
        self.ended_by_us = True
        await self.session.send_frame(
            ServiceFrame.reset(self.stream_id, Reset(code=code, reason=reason))
        )


Handler = Callable[[Stream], Awaitable[None]]


class NoiseSession:
    """One authenticated, encrypted connection from a gadget."""

    def __init__(
        self,
        ws: WebSocket,
        node_id: str,
        handlers: dict[str, Handler],
        still_allowed: Callable[[str], bool] = lambda node_id: True,
    ) -> None:
        self.ws = ws
        self.node_id = node_id
        self._handlers = handlers
        self._still_allowed = still_allowed
        self._checked_at = time.monotonic()
        self._send_lock = asyncio.Lock()
        self._streams: dict[int, Stream] = {}
        self._tasks: set[asyncio.Task] = set()
        self._decoder = NoiseFrameDecoder()
        self._send_cipher = None
        self._recv_cipher = None
        self.closed = asyncio.Event()

    async def handshake(self, static_key) -> None:
        responder = NoiseXXResponder(static_private_key=static_key)
        responder.initialize()
        async with asyncio.timeout(HANDSHAKE_TIMEOUT_S):
            message2 = responder.read_message1_and_write_message2(await self.ws.receive_bytes())
            await self.ws.send_bytes(message2)
            responder.read_message3(await self.ws.receive_bytes())
        self._send_cipher, self._recv_cipher = responder.split()

    async def run(self) -> None:
        """Read frames until the gadget goes away, dispatching streams."""
        try:
            while True:
                ciphertext = await self.ws.receive_bytes()
                if time.monotonic() - self._checked_at >= REVOKE_CHECK_S:
                    self._checked_at = time.monotonic()
                    if not self._still_allowed(self.node_id):
                        log.warning("%s was revoked; closing its session", self.node_id)
                        await self.close()
                        return
                plain = self._recv_cipher.decrypt_with_ad(EMPTY_AD, ciphertext)
                envelope = self._decoder.decode(plain)
                if envelope is not None:
                    self._dispatch(decode_request_envelope(envelope))
        except (WebSocketDisconnect, RuntimeError):
            pass
        finally:
            self.closed.set()
            for task in self._tasks:
                task.cancel()
            for stream in self._streams.values():
                stream.feed(b"", end=True)
                stream.gone.set()

    def _dispatch(self, frame: ServiceFrame) -> None:
        if frame.kind == "request":
            if len(self._streams) >= MAX_STREAMS:
                log.warning("%s opened too many streams; refusing one", self.node_id)
                self._spawn(self._refuse(frame.stream_id, "too many streams"))
                return
            request = frame.value
            stream = Stream(
                session=self,
                stream_id=frame.stream_id,
                verb=request.verb,
                path=request.path.split("?", 1)[0],
                headers={h.key.lower(): h.value for h in request.headers},
            )
            stream.feed(request.body, request.end_body)
            self._streams[frame.stream_id] = stream
            handler = self._handlers.get(stream.path, _not_found)
            self._spawn(self._handle(handler, stream))
        elif frame.kind == "body_chunk":
            stream = self._streams.get(frame.stream_id)
            if stream is None:
                return
            if stream.buffered + len(frame.value.data) > MAX_STREAM_BUFFER:
                log.warning("%s sent %s more than it reads; resetting", self.node_id, stream.path)
                del self._streams[frame.stream_id]
                stream.feed(b"", end=True)
                stream.gone.set()
                self._spawn(self._refuse(frame.stream_id, "too much unread data"))
                return
            stream.feed(frame.value.data, frame.value.end_body)
        elif frame.kind == "reset":
            stream = self._streams.pop(frame.stream_id, None)
            if stream is not None:
                stream.feed(b"", end=True)
                stream.gone.set()

    def _spawn(self, coro) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _refuse(self, stream_id: int, reason: str) -> None:
        reset = Reset(code=ResetCode.REFUSED_STREAM, reason=reason)
        await self.send_frame(ServiceFrame.reset(stream_id, reset))

    async def _handle(self, handler: Handler, stream: Stream) -> None:
        try:
            await handler(stream)
        except Exception:
            log.exception("%s on %s failed", stream.path, self.node_id)
            if not stream.ended_by_us and not self.closed.is_set():
                try:
                    if stream.responded:
                        await stream.reset("internal error", ResetCode.INTERNAL_ERROR)
                    else:
                        await stream.respond(500)
                except Exception as exc:  # the connection may be gone already
                    log.debug("could not report the failure: %s", exc)
        finally:
            if stream.ended_by_us:
                self._streams.pop(stream.stream_id, None)

    async def send_frame(self, frame: ServiceFrame) -> None:
        # Encrypt under the lock: nonces must reach the wire in order.
        async with self._send_lock:
            for chunk in encode_noise_frames(encode_response_envelope(frame)):
                await self.ws.send_bytes(self._send_cipher.encrypt_with_ad(EMPTY_AD, chunk))

    async def close(self) -> None:
        try:
            await self.ws.close()
        except RuntimeError:
            pass


async def _not_found(stream: Stream) -> None:
    await stream.respond(404)


async def identity(stream: Stream) -> None:
    name = stream.session.ws.app.state.config.agent_name
    await stream.respond_json(200, {"ok": True, "result": {"name": name}})


def bearer(ws: WebSocket) -> str:
    scheme, _, token = ws.headers.get("authorization", "").partition(" ")
    return token.strip() if scheme.lower() == "bearer" else ""


async def noise_endpoint(ws: WebSocket) -> None:
    state = ws.app.state
    vm_id = ws.query_params.get("vm_id", "")
    if vm_id != state.config.vm_id:
        await ws.send_denial_response(Response(status_code=403))
        return
    node_id = state.tokens.verify_vm_token(bearer(ws), vm_id)
    if node_id is None:
        await ws.send_denial_response(Response(status_code=401))
        return
    if state.hub.session_count(node_id) >= MAX_SESSIONS_PER_NODE:
        log.warning("%s already has %d sessions; refusing another", node_id, MAX_SESSIONS_PER_NODE)
        await ws.send_denial_response(Response(status_code=429))
        return
    await ws.accept()
    session = NoiseSession(ws, node_id, state.stream_handlers, state.tokens.is_active)
    try:
        await session.handshake(state.noise_key)
    except Exception as exc:
        log.warning("Noise handshake with %s failed: %s", node_id, type(exc).__name__)
        await session.close()
        return
    log.info("Noise session with %s", node_id)
    state.hub.attach(node_id, session)
    try:
        await session.run()
    finally:
        state.hub.detach(node_id, session)
    log.info("Noise session with %s ended", node_id)
