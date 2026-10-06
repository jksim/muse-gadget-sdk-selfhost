"""Hermes Agent (Nous Research) as Clio's brain, through its API server.

Hermes runs on the Pi as its own program: its memory, skills and tools are its
own, and the gadgets reach it through musehost's MCP server, so no tools are
sent from here. Each musehost conversation is one named Hermes conversation
(Responses API ``conversation``), and Hermes keeps that history itself.

Streamed events, as Hermes's API server sends them:
- ``response.output_text.delta`` is Clio's text, except for message items
  marked ``phase: "commentary"`` (progress chatter, never spoken);
- function-call items and ``hermes.tool.progress`` mean Hermes is working;
- ``response.completed`` ends the turn; ``response.failed`` fails it;
- ``:`` comment lines are keepalives; unknown events are ignored.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator

import httpx

from musehost.brain.providers.base import Done, Event, ProviderError, Status, Text

log = logging.getLogger(__name__)

DEFAULT_BASE_URL = "http://127.0.0.1:8642/v1"
DEFAULT_MODEL = "hermes-agent"
DEFAULT_TIMEOUT_S = 120.0


class HermesProvider:
    name = "hermes"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = DEFAULT_BASE_URL,
        model: str = DEFAULT_MODEL,
        timeout_s: float = DEFAULT_TIMEOUT_S,
    ) -> None:
        self._api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_s = timeout_s

    def _instructions(self, system: str, node_id: str | None) -> str:
        if not node_id:
            return system
        return (
            f"{system}\n\nThis conversation is with the gadget {node_id}. When you use the "
            f'musehost tools to act on it, pass gadget="{node_id}".'
        )

    async def stream_turn(
        self,
        *,
        system,
        history,
        user_text,
        tools,
        run_tool,
        max_rounds,
        conversation: str | None = None,
        node_id: str | None = None,
    ) -> AsyncIterator[Event]:
        """One turn. ``history``, ``tools`` and ``run_tool`` are unused: Hermes
        keeps the conversation and runs its own tools."""
        body = {
            "model": self.model,
            "instructions": self._instructions(system, node_id),
            "input": user_text,
            "stream": True,
            "store": True,
        }
        if conversation:
            body["conversation"] = conversation
        said = []
        done = None
        try:
            async with asyncio.timeout(self.timeout_s):
                async for event in self._stream(body):
                    if isinstance(event, Text):
                        said.append(event.text)
                    if isinstance(event, Done):
                        done = event
                        continue
                    yield event
        except TimeoutError:
            raise ProviderError("unavailable", f"no answer within {self.timeout_s:.0f} s") from None
        if done is None:
            raise ProviderError("unavailable", "the stream ended before the answer was complete")
        yield Done(
            done.stop_reason,
            done.usage,
            [
                {"role": "user", "content": user_text},
                {"role": "assistant", "content": "".join(said)},
            ],
        )

    async def _stream(self, body: dict) -> AsyncIterator[Event]:
        headers = {"Authorization": f"Bearer {self._api_key}", "Accept": "text/event-stream"}
        timeout = httpx.Timeout(self.timeout_s, connect=10.0)
        try:
            async with (
                httpx.AsyncClient(timeout=timeout) as client,
                client.stream(
                    "POST", f"{self.base_url}/responses", json=body, headers=headers
                ) as response,
            ):
                if response.status_code in (401, 403):
                    raise ProviderError("auth", f"Hermes said {response.status_code}")
                if response.status_code >= 400:
                    raise ProviderError("unavailable", f"Hermes said {response.status_code}")
                async for event in self._events(response):
                    yield event
        except httpx.HTTPError as exc:
            raise ProviderError("unavailable", type(exc).__name__) from None

    async def _events(self, response) -> AsyncIterator[Event]:
        commentary: set[str] = set()
        working = False
        name, data = "", []
        async for line in response.aiter_lines():
            if line.startswith(":"):
                continue  # keepalive
            if line.startswith("event:"):
                name = line[6:].strip()
                continue
            if line.startswith("data:"):
                data.append(line[5:].strip())
                continue
            if line or not data:
                continue
            try:
                payload = json.loads("\n".join(data))
            except ValueError:
                payload = {}
            kind = name or payload.get("type", "")
            name, data = "", []

            if kind == "response.output_item.added":
                item = payload.get("item") or {}
                if item.get("type") == "message" and item.get("phase") == "commentary":
                    commentary.add(item.get("id"))
                elif item.get("type") == "function_call" and not working:
                    working = True
                    yield Status("working")
            elif kind == "hermes.tool.progress":
                if not working:
                    working = True
                    yield Status("working")
            elif kind == "response.output_text.delta":
                if payload.get("item_id") not in commentary and payload.get("delta"):
                    working = False
                    yield Text(payload["delta"])
            elif kind == "response.completed":
                usage = (payload.get("response") or {}).get("usage") or {}
                yield Done("completed", usage)
                return
            elif kind in ("response.failed", "error", "response.incomplete"):
                raise ProviderError("unavailable", f"Hermes reported {kind}")
