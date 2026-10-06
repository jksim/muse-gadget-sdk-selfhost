"""What every language-model provider looks like to the brain.

A provider runs one turn in its own native message format, including its own
tool-call loop: it calls ``run_tool`` for each tool call and feeds the result
back to its model. To the brain it yields neutral events and finally ``Done``,
which carries the native messages to append to the conversation, so history
round-trips exactly (Claude requires its thinking blocks back unchanged).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class Text:
    text: str


@dataclass(frozen=True)
class Status:
    activity: str  # e.g. "working" while a tool or web search runs


@dataclass(frozen=True)
class Done:
    stop_reason: str  # end_turn | max_tokens | tool_rounds | ...
    usage: dict = field(default_factory=dict)
    messages: list[dict] = field(default_factory=list)  # native, to append to history


@dataclass(frozen=True)
class ToolSpec:
    name: str  # API-safe name, e.g. display_draw_url
    description: str
    input_schema: dict


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    input: Any


@dataclass(frozen=True)
class ToolResult:
    content: str  # JSON text for the model
    is_error: bool = False


Event = Text | Status | Done
RunTool = Callable[[ToolCall], Awaitable[ToolResult]]


class ProviderError(Exception):
    """A turn the provider couldn't complete.

    ``kind`` is ``auth`` (credentials rejected), ``unavailable`` (rate limits,
    overload, server or network trouble after retries) or ``refused``.
    """

    def __init__(self, kind: str, detail: str = "") -> None:
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail


class Provider(Protocol):
    name: str
    model: str

    def stream_turn(
        self,
        *,
        system: str,
        history: list[dict],
        user_text: str,
        tools: list[ToolSpec],
        run_tool: RunTool,
        max_rounds: int,
        conversation: str | None = None,  # e.g. musehost-12, for providers that keep history
        node_id: str | None = None,  # the gadget the turn comes from
    ) -> AsyncIterator[Event]: ...
