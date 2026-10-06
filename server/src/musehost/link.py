"""``/link-control``: the long-lived stream a gadget's Link keeps open.

Both directions carry JSON messages, each prefixed by its length as a u32 LE
(a 0-length message is a keepalive). The gadget sends ``link.register`` once,
then heartbeats and ``link.result`` replies; the host sends ``link.invoke``
requests and the ``link.unpaired`` event.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid

from musegadget.link_client import MessageDecoder, encode_message

from musehost.hub import DeviceOffline, InvokeResult, InvokeTimeout
from musehost.noise_server import Stream

log = logging.getLogger(__name__)

# The ESP32 starts its heartbeats only after exactly this reply shape.
REGISTERED = {"status": "registered"}


class LinkConnection:
    """One gadget's /link-control stream."""

    def __init__(self, stream: Stream, hub) -> None:
        self.stream = stream
        self.node_id = stream.session.node_id
        self.hub = hub
        self._pending: dict[str, asyncio.Future] = {}

    async def send(self, message: dict) -> None:
        await self.stream.send(encode_message(message))

    async def handle(self, message: dict) -> None:
        method = message.get("method")
        if method == "link.register":
            await self._register(message)
        elif method == "link.heartbeat":
            self.hub.heartbeat(self.node_id)
        elif method == "link.result":
            self._result(message)
        else:
            log.debug("ignoring %s from %s", method or message.get("type"), self.node_id)

    async def invoke(self, command: str, params: dict, timeout_s: float) -> InvokeResult:
        invoke_id = str(uuid.uuid4())
        done = asyncio.get_running_loop().create_future()
        self._pending[invoke_id] = done
        try:
            await self.send(
                {
                    "method": "link.invoke",
                    "id": invoke_id,
                    "command": command,
                    "params": params,
                    "timeout_ms": int(timeout_s * 1000),
                }
            )
            log.info("invoke %s on %s", command, self.node_id)
            async with asyncio.timeout(timeout_s):
                return await done
        except TimeoutError:
            raise InvokeTimeout(f"{command} on {self.node_id}") from None
        finally:
            self._pending.pop(invoke_id, None)

    def _result(self, message: dict) -> None:
        done = self._pending.get(message.get("id"))
        if done is None or done.done():
            return
        error = message.get("error")
        if error is not None and not isinstance(error, str):
            error = error.get("message") if isinstance(error, dict) else None
            error = error or json.dumps(message.get("error"))
        done.set_result(
            InvokeResult(ok=bool(message.get("ok")), payload=message.get("payload"), error=error)
        )

    def close(self) -> None:
        for done in self._pending.values():
            if not done.done():
                done.set_exception(DeviceOffline(self.node_id))

    async def _register(self, message: dict) -> None:
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        reply = {"type": "res", "id": message.get("id")}
        if params.get("node_id") != self.node_id:
            log.warning("%s tried to register as %r", self.node_id, params.get("node_id"))
            await self.send({**reply, "error": {"message": "node_id does not match the token"}})
            return
        self.hub.register(self.node_id, params, self)
        await self.send({**reply, "result": REGISTERED})


async def link_control(stream: Stream) -> None:
    hub = stream.session.ws.app.state.hub
    connection = LinkConnection(stream, hub)
    await stream.respond(200, end=False)
    decoder = MessageDecoder()
    try:
        async for data in stream.chunks():
            for message in decoder.feed(data):
                await connection.handle(message)
    finally:
        connection.close()
        hub.unregister(connection.node_id, connection)


async def link_tunnel(stream: Stream) -> None:
    # Our firmware builds turn the IP tunnel off; refuse it if one asks.
    await stream.respond(404)
