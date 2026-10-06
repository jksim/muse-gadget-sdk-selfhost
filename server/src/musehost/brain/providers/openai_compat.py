"""Clio on an OpenAI-compatible Chat Completions API: OpenAI itself, or a local vLLM.

Through the official ``openai`` SDK (async). vLLM serves the same API, so the
only differences are ``base_url`` and that a key is optional; tool calling on
vLLM also needs the server started with ``--enable-auto-tool-choice`` and a
``--tool-call-parser`` for the model. Web search isn't offered here.

History holds the chat messages exactly as sent (system prompt excluded: it is
added fresh each request).
"""

from __future__ import annotations

import json
import logging

import openai

from musehost.brain.providers.base import Done, ProviderError, Text, ToolCall, ToolResult

log = logging.getLogger(__name__)


class OpenAICompatProvider:
    def __init__(
        self,
        *,
        name: str,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        max_tokens: int = 4096,
        max_retries: int = 2,
    ) -> None:
        self.name = name  # "openai" or "vllm": keys history
        self.model = model
        self.max_tokens = max_tokens
        # The SDK insists on a key; a local vLLM usually doesn't check it.
        self._client = openai.AsyncOpenAI(
            api_key=api_key or "unused", base_url=base_url or None, max_retries=max_retries
        )

    async def stream_turn(self, *, system, history, user_text, tools, run_tool, max_rounds):
        user = {"role": "user", "content": user_text}
        messages = [{"role": "system", "content": system}, *history, user]
        new_messages = [user]
        tool_params = [
            {
                "type": "function",
                "function": {
                    "name": spec.name,
                    "description": spec.description,
                    "parameters": spec.input_schema,
                },
            }
            for spec in tools
        ]
        usage = {"input_tokens": 0, "output_tokens": 0}
        rounds = 0
        spoke = False
        while True:
            extra = {"tools": tool_params} if tool_params else {}
            if tool_params and rounds >= max_rounds:
                extra["tool_choice"] = "none"
            text, calls, finish = "", {}, None
            try:
                stream = await self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    max_completion_tokens=self.max_tokens,
                    stream=True,
                    stream_options={"include_usage": True},
                    **extra,
                )
                async for chunk in stream:
                    if chunk.usage:
                        usage["input_tokens"] += chunk.usage.prompt_tokens or 0
                        usage["output_tokens"] += chunk.usage.completion_tokens or 0
                    for choice in chunk.choices:
                        delta = choice.delta
                        if delta.content:
                            if spoke and not text:
                                yield Text(" ")  # between rounds
                            text += delta.content
                            yield Text(delta.content)
                        for part in delta.tool_calls or []:
                            call = calls.setdefault(
                                part.index, {"id": "", "name": "", "arguments": ""}
                            )
                            if part.id:
                                call["id"] = part.id
                            if part.function and part.function.name:
                                call["name"] = part.function.name
                            if part.function and part.function.arguments:
                                call["arguments"] += part.function.arguments
                        if choice.finish_reason:
                            finish = choice.finish_reason
            except (openai.AuthenticationError, openai.PermissionDeniedError) as exc:
                raise ProviderError("auth", f"HTTP {exc.status_code}") from None
            except openai.RateLimitError:
                raise ProviderError("unavailable", "rate limited") from None
            except openai.APIStatusError as exc:
                if exc.status_code < 500:
                    log.error(
                        "%s rejected the request: HTTP %d: %s",
                        self.name,
                        exc.status_code,
                        str(exc.message)[:300],
                    )
                raise ProviderError("unavailable", f"HTTP {exc.status_code}") from None
            except openai.APIConnectionError as exc:
                raise ProviderError("unavailable", type(exc).__name__) from None
            spoke = spoke or bool(text)

            if finish == "content_filter":
                raise ProviderError("refused", "content_filter")
            ordered = [calls[i] for i in sorted(calls)]
            if ordered and finish == "length":
                # Cut off mid tool call: keep none of this turn (append-only history).
                yield Done("max_tokens", usage, [])
                return
            assistant = {"role": "assistant", "content": text or None}
            if ordered:
                assistant["tool_calls"] = [
                    {
                        "id": c["id"],
                        "type": "function",
                        "function": {"name": c["name"], "arguments": c["arguments"]},
                    }
                    for c in ordered
                ]
            messages.append(assistant)
            new_messages.append(assistant)
            if not ordered:
                stop = {"stop": "end_turn", "length": "max_tokens"}.get(finish or "", finish or "")
                yield Done(stop, usage, new_messages)
                return
            for call in ordered:
                try:
                    arguments = json.loads(call["arguments"] or "{}")
                    result = await run_tool(ToolCall(call["id"], call["name"], arguments))
                except json.JSONDecodeError:
                    result = ToolResult(
                        json.dumps({"ok": False, "error": "invalid JSON arguments"}), is_error=True
                    )
                tool_message = {
                    "role": "tool",
                    "tool_call_id": call["id"],
                    "content": result.content,
                }
                messages.append(tool_message)
                new_messages.append(tool_message)
            rounds += 1
