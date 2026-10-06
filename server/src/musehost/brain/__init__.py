"""Clio's mind: the chat handler that answers turns with a language model.

``Brain`` is installed with ``hub.set_chat_handler``. For each turn it skips
the model when a voice note couldn't be understood, builds the prompt, lets the
provider stream a reply (running tools through the hub), and turns provider
failures into short spoken explanations. It never logs what was said.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time

from musehost.brain import prompt
from musehost.brain.history import History
from musehost.brain.providers.base import (
    Done,
    Provider,
    ProviderError,
    Status,
    Text,
    ToolCall,
    ToolResult,
)
from musehost.brain.tools import Toolset, build_toolset, validate
from musehost.chat import placeholder
from musehost.config import HostConfig
from musehost.hub import ChatTurn, DeviceOffline, Hub, InvokeTimeout

log = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 5
MAX_TOOL_CALLS = 10  # per turn, across rounds
FAILURE_REPLIES = {
    "auth": "My brain's credentials aren't working.",
    "unavailable": "I couldn't reach my brain just now. Try again in a moment.",
    "refused": "I can't help with that one.",
}


class Brain:
    def __init__(
        self, hub: Hub, provider: Provider, config: HostConfig, history: History | None = None
    ) -> None:
        self.hub = hub
        self.provider = provider
        self.config = config
        self.history = history

    async def __call__(self, turn: ChatTurn):
        if turn.audio_path is not None and turn.speech_status != "ok":
            async for piece in placeholder(turn):
                yield piece
            return
        tool_node = turn.tool_node_id or turn.node_id
        device = next((d for d in self.hub.devices() if d.node_id == tool_node), None)
        user_text = prompt.user_message(
            turn.text or "",
            device.display_name if device else turn.node_id,
            device.platform if device else "",
        )
        toolset = build_toolset(device.commands if device else {}, self.config.brain_tools)
        fingerprint = self._fingerprint(toolset)
        calls = 0

        async def run_tool(call: ToolCall) -> ToolResult:
            nonlocal calls
            calls += 1
            if calls > MAX_TOOL_CALLS:
                return _error("tool limit reached for this turn; answer with what you have")
            return await self._run_tool(tool_node, toolset, call)

        started = time.monotonic()
        first_text = None
        said = ""
        for attempt in (1, 2):
            conversation = (
                self.history.current(
                    turn.node_id, self.provider.name, self.provider.model, fingerprint
                )
                if self.history
                else None
            )
            try:
                async for event in self.provider.stream_turn(
                    system=prompt.SYSTEM_PROMPT,
                    history=conversation.messages if conversation else [],
                    user_text=user_text,
                    tools=toolset.specs,
                    run_tool=run_tool,
                    max_rounds=MAX_TOOL_ROUNDS,
                    conversation=f"musehost-{conversation.id}" if conversation else None,
                    node_id=turn.node_id,
                ):
                    if isinstance(event, Text) and event.text:
                        if first_text is None:
                            first_text = time.monotonic() - started
                        said += event.text
                        yield event.text
                    elif isinstance(event, Status):
                        await self.hub.publish(
                            turn.node_id, "agent.status", {"activity_code": event.activity}
                        )
                    elif isinstance(event, Done):
                        if conversation is not None:
                            self.history.append(conversation.id, event.messages)
                        log.info(
                            "%s/%s answered %s: stop %s, first text %.2f s, done %.2f s, usage %s",
                            self.provider.name,
                            self.provider.model,
                            turn.node_id,
                            event.stop_reason,
                            first_text or 0.0,
                            time.monotonic() - started,
                            event.usage,
                        )
                return
            except ProviderError as exc:
                if exc.kind == "stale_history" and attempt == 1 and not said and self.history:
                    # The model rejected the stored history (e.g. tools changed
                    # mid-conversation): start a fresh conversation, try once more.
                    log.warning(
                        "%s history for %s was stale; starting fresh",
                        self.provider.name,
                        turn.node_id,
                    )
                    self.history.start_new(turn.node_id)
                    continue
                log.warning("%s turn for %s failed: %s", self.provider.name, turn.node_id, exc.kind)
                reply = FAILURE_REPLIES.get(exc.kind, FAILURE_REPLIES["unavailable"])
                yield (" " if said else "") + reply
                return

    def _fingerprint(self, toolset: Toolset) -> str:
        """Identifies what Claude binds a conversation's thinking to: prompt and tools."""
        basis = {
            "system": prompt.SYSTEM_PROMPT,
            "tools": [[s.name, s.description, s.input_schema] for s in toolset.specs],
            "web_search": bool(getattr(self.provider, "web_search", False)),
        }
        return hashlib.sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()[:32]

    async def _run_tool(self, node_id: str, toolset: Toolset, call: ToolCall) -> ToolResult:
        command = toolset.command(call.name)
        if command is None:
            log.warning("refused tool %r for %s: not available", call.name, node_id)
            return _error(f"no tool named {call.name!r} is available")
        problem = validate(call.input, command.schema)
        if problem:
            log.info("tool %s for %s: invalid input", command.name, node_id)
            return _error(f"invalid input: {problem}")
        await self.hub.publish(node_id, "agent.status", {"activity_code": "working"})
        try:
            result = await self.hub.invoke(node_id, command.name, call.input, command.timeout_s)
        except DeviceOffline:
            log.info("tool %s for %s: device offline", command.name, node_id)
            return _error("the device is offline")
        except InvokeTimeout:
            log.info("tool %s for %s: timed out", command.name, node_id)
            return _error("the device didn't answer in time")
        log.info("tool %s for %s: %s", command.name, node_id, "ok" if result.ok else "error")
        body = {"ok": result.ok, "payload": result.payload, "error": result.error}
        return ToolResult(json.dumps(body), is_error=not result.ok)


def _error(message: str) -> ToolResult:
    return ToolResult(json.dumps({"ok": False, "error": message}), is_error=True)
