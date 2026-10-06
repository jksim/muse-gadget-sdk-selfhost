"""Clio on the Claude API, through the official ``anthropic`` SDK (1.x, async).

Every turn is one streamed Messages request:
- the configured model (default Claude Opus 5.5) at the configured effort; Opus
  5.5 thinks adaptively on its own, so ``thinking`` isn't sent;
- top-level ``cache_control`` so the stable prefix (system, tools, history) is
  reused;
- ``fallbacks: "default"`` (beta ``server-side-fallback-2026-07-01``) so a
  classifier decline is re-run on Anthropic's recommended fallback model
  instead of leaving Clio silent.

The assistant turn is kept as the API returned it (``to_dict`` keeps the wire
names and omits unset fields), so history can be sent back unchanged.
"""

from __future__ import annotations

import logging

import anthropic

from musehost.brain.providers.base import Done, ProviderError, Status, Text, ToolCall

log = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-opus-5-5"
FALLBACK_BETA = "server-side-fallback-2026-07-01"
# The 400 Claude gives when history no longer matches the prefix its thinking
# blocks were bound to (system prompt, tools, or earlier messages changed).
STALE_HISTORY = "bound to a different conversation"
WEB_SEARCH = {"type": "web_search_20260209", "name": "web_search", "max_uses": 2}


class ClaudeProvider:
    name = "claude"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str = "",
        effort: str = "low",
        max_tokens: int = 4096,
        web_search: bool = False,
        base_url: str | None = None,
        max_retries: int = 2,
    ) -> None:
        self.model = model or DEFAULT_MODEL
        self.effort = effort
        self.max_tokens = max_tokens
        self.web_search = web_search
        self._client = anthropic.AsyncAnthropic(
            api_key=api_key, base_url=base_url, max_retries=max_retries
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
        conversation=None,
        node_id=None,
    ):
        """One turn: stream text, run tool calls through ``run_tool``, repeat.

        Tool results for a round go back in a single user message. ``pause_turn``
        (a long server-side web search) is resumed. Tools never run when the
        reply hit ``max_tokens`` or was refused. After ``max_rounds`` tool
        rounds, ``tool_choice: none`` makes Claude answer with what it has.
        """
        user = {"role": "user", "content": user_text}
        messages = [*history, user]
        new_messages = [user]
        tool_params = [
            {
                "name": spec.name,
                "description": spec.description,
                "input_schema": spec.input_schema,
                "eager_input_streaming": True,
            }
            for spec in tools
        ]
        if self.web_search:
            tool_params.append(WEB_SEARCH)
        usage = dict.fromkeys(_USAGE_FIELDS, 0)
        rounds = 0
        parse_failures = 0
        spoke = False
        while True:
            extra = {"tools": tool_params} if tool_params else {}
            if tool_params and rounds >= max_rounds:
                extra["tool_choice"] = {"type": "none"}
            round_spoke = False
            try:
                async with self._client.beta.messages.stream(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    system=system,
                    messages=messages,
                    output_config={"effort": self.effort},
                    cache_control={"type": "ephemeral"},
                    fallbacks="default",
                    betas=[FALLBACK_BETA],
                    **extra,
                ) as stream:
                    async for event in stream:
                        if event.type == "text" and event.text:
                            if spoke and not round_spoke:
                                yield Text(" ")  # between rounds: "Let me check." "Your…"
                            round_spoke = True
                            yield Text(event.text)
                        elif (
                            event.type == "content_block_start"
                            and event.content_block.type == "server_tool_use"
                        ):
                            yield Status("working")
                    final = await stream.get_final_message()
            except ValueError:
                # Tool input the SDK couldn't parse at all; no tool_use id to
                # answer, so ask again (bounded). API errors aren't ValueError.
                parse_failures += 1
                if parse_failures > 2:
                    raise ProviderError("unavailable", "unparseable tool input") from None
                continue
            except (anthropic.AuthenticationError, anthropic.PermissionDeniedError) as exc:
                raise ProviderError("auth", f"HTTP {exc.status_code}") from None
            except anthropic.RateLimitError:
                raise ProviderError("unavailable", "rate limited") from None
            except anthropic.APIStatusError as exc:
                # Overloaded (529), server errors, and request problems on our
                # side (400/404) all leave Clio unable to answer this turn.
                if exc.status_code < 500:
                    log.error(
                        "Claude rejected the request: HTTP %d: %s",
                        exc.status_code,
                        str(exc.message)[:300],
                    )
                    if STALE_HISTORY in str(exc.message):
                        raise ProviderError("stale_history", "thinking bound elsewhere") from None
                raise ProviderError("unavailable", f"HTTP {exc.status_code}") from None
            except anthropic.APIConnectionError as exc:
                raise ProviderError("unavailable", type(exc).__name__) from None
            parse_failures = 0
            spoke = spoke or round_spoke
            for name in _USAGE_FIELDS:
                usage[name] += getattr(final.usage, name, None) or 0

            if final.stop_reason == "refusal":
                category = (
                    getattr(final.stop_details, "category", None) if final.stop_details else None
                )
                raise ProviderError("refused", str(category or ""))
            assistant = {
                "role": "assistant",
                "content": history_content([block.to_dict() for block in final.content]),
            }
            messages.append(assistant)
            new_messages.append(assistant)
            if final.stop_reason == "pause_turn":
                continue
            tool_uses = [b for b in final.content if b.type == "tool_use"]
            if tool_uses and final.stop_reason == "max_tokens":
                # Cut off mid tool call: that tool_use can never get its result,
                # and history is append-only, so keep none of this turn.
                yield Done(final.stop_reason, usage, [])
                return
            if final.stop_reason != "tool_use" or not tool_uses:
                yield Done(final.stop_reason or "", usage, new_messages)
                return
            results = []
            for block in tool_uses:
                result = await run_tool(ToolCall(block.id, block.name, block.input))
                results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result.content,
                        "is_error": result.is_error,
                    }
                )
            tool_message = {"role": "user", "content": results}
            messages.append(tool_message)
            new_messages.append(tool_message)
            rounds += 1


_USAGE_FIELDS = (
    "input_tokens",
    "output_tokens",
    "cache_read_input_tokens",
    "cache_creation_input_tokens",
)


_KEEP_BEFORE_FALLBACK = {"text"}


def history_content(content: list[dict]) -> list[dict]:
    """Content as it must be echoed back on later turns.

    After a mid-output fallback, blocks before the last ``fallback`` marker that
    the fallback model can't take back are dropped: thinking, tool_use, unpaired
    server-tool blocks, anything unrecognised. Text and paired server-tool
    blocks stay, as does everything after the marker. The marker itself is an
    audit note and is dropped.
    """
    boundary = max((i for i, b in enumerate(content) if b.get("type") == "fallback"), default=-1)
    if boundary < 0:
        return content
    before, after = content[:boundary], content[boundary + 1 :]
    results = {
        b.get("tool_use_id") for b in before if str(b.get("type", "")).endswith("_tool_result")
    }
    calls = {b.get("id") for b in before if b.get("type") == "server_tool_use"}
    kept = []
    for block in before:
        kind = block.get("type")
        if kind in _KEEP_BEFORE_FALLBACK:
            kept.append(block)
        elif kind == "server_tool_use" and block.get("id") in results:
            kept.append(block)
        elif str(kind).endswith("_tool_result") and block.get("tool_use_id") in calls:
            kept.append(block)
    return kept + after
