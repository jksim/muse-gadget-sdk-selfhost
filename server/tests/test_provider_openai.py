"""The OpenAI / vLLM adapter, using the real openai SDK against a local fake server."""

import asyncio
import json

import pytest
from fake_llm import chat_stream, claude_message, fake_server, openai_error
from test_brain import CONFIG, text_turn
from test_brain_tools import hub_with_cores3

from musehost.brain import Brain
from musehost.brain.providers.base import ProviderError, Text, ToolCall, ToolResult, ToolSpec
from musehost.brain.providers.claude import ClaudeProvider
from musehost.brain.providers.openai_compat import OpenAICompatProvider

HEALTH = ToolSpec(
    "device_health",
    "Report battery.",
    {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
)


def provider_for(url, **kwargs):
    kwargs.setdefault("name", "openai")
    kwargs.setdefault("model", "gpt-test")
    return OpenAICompatProvider(api_key="test-key", base_url=url + "/v1", max_retries=0, **kwargs)


def run_turn(provider, *, tools=(), result=None, max_rounds=5, history=None):
    calls = []

    async def run_tool(call):
        calls.append(call)
        return result or ToolResult(json.dumps({"ok": True, "payload": {"battery_pct": 82}}))

    async def scenario():
        return [
            e
            async for e in provider.stream_turn(
                system="You are Clio.",
                history=history or [],
                user_text="How's your battery?",
                tools=list(tools),
                run_tool=run_tool,
                max_rounds=max_rounds,
            )
        ]

    return asyncio.run(scenario()), calls


def test_text_streams_and_the_request_is_a_streamed_chat_completion():
    with fake_server([chat_stream(["Hi ", "there."])]) as (url, requests):
        events, _ = run_turn(provider_for(url))
    assert [e.text for e in events if isinstance(e, Text)] == ["Hi ", "there."]
    [request] = requests
    body = request["body"]
    assert request["path"] == "/v1/chat/completions"
    assert body["model"] == "gpt-test" and body["stream"] is True
    assert body["stream_options"] == {"include_usage": True}
    assert body["max_completion_tokens"] == 4096
    assert body["messages"] == [
        {"role": "system", "content": "You are Clio."},
        {"role": "user", "content": "How's your battery?"},
    ]
    assert "tools" not in body
    done = events[-1]
    assert done.stop_reason == "end_turn"
    assert done.usage["input_tokens"] == 80 and done.usage["output_tokens"] == 12
    assert done.messages == [
        {"role": "user", "content": "How's your battery?"},
        {"role": "assistant", "content": "Hi there."},
    ]


def test_tool_calls_split_across_chunks_are_assembled_and_answered():
    first = chat_stream(
        tool_calls=[(0, "call_1", "device_health", ["{", "}"])], finish="tool_calls"
    )
    second = chat_stream(["Your battery is 82%."])
    with fake_server([first, second]) as (url, requests):
        events, calls = run_turn(provider_for(url), tools=(HEALTH,))
    assert calls == [ToolCall("call_1", "device_health", {})]
    tools = requests[0]["body"]["tools"]
    assert tools == [
        {
            "type": "function",
            "function": {
                "name": "device_health",
                "description": "Report battery.",
                "parameters": HEALTH.input_schema,
            },
        }
    ]
    sent = requests[1]["body"]["messages"]
    assert sent[2] == {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "device_health", "arguments": "{}"},
            }
        ],
    }
    assert sent[3] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": json.dumps({"ok": True, "payload": {"battery_pct": 82}}),
    }
    assert [m["role"] for m in events[-1].messages] == ["user", "assistant", "tool", "assistant"]


def test_unparseable_arguments_go_back_as_an_error_without_running_the_tool():
    first = chat_stream(
        tool_calls=[(0, "call_2", "device_health", ['{"broken'])], finish="tool_calls"
    )
    second = chat_stream(["Sorry."])
    with fake_server([first, second]) as (url, requests):
        _, calls = run_turn(provider_for(url), tools=(HEALTH,))
    assert calls == []
    assert "invalid" in requests[1]["body"]["messages"][3]["content"].lower()


def test_after_the_last_round_tool_choice_is_none():
    first = chat_stream(tool_calls=[(0, "call_3", "device_health", ["{}"])], finish="tool_calls")
    second = chat_stream(["82%."])
    with fake_server([first, second]) as (url, requests):
        run_turn(provider_for(url), tools=(HEALTH,), max_rounds=1)
    assert "tool_choice" not in requests[0]["body"]
    assert requests[1]["body"]["tool_choice"] == "none"


def test_length_with_a_pending_tool_call_keeps_nothing():
    first = chat_stream(tool_calls=[(0, "call_4", "device_health", ['{"a'])], finish="length")
    with fake_server([first]) as (url, _):
        events, calls = run_turn(provider_for(url), tools=(HEALTH,))
    assert calls == [] and events[-1].stop_reason == "max_tokens" and events[-1].messages == []


def test_a_content_filter_stop_is_a_refusal():
    with fake_server([chat_stream(["x"], finish="content_filter")]) as (url, _):
        with pytest.raises(ProviderError) as caught:
            run_turn(provider_for(url))
    assert caught.value.kind == "refused"


@pytest.mark.parametrize(
    "status, kind",
    [
        (401, "auth"),
        (403, "auth"),
        (429, "unavailable"),
        (500, "unavailable"),
        (404, "unavailable"),
    ],
)
def test_errors_are_classified(status, kind):
    with fake_server([openai_error(status)]) as (url, _):
        with pytest.raises(ProviderError) as caught:
            run_turn(provider_for(url))
    assert caught.value.kind == kind


def test_vllm_uses_its_base_url_and_needs_no_key():
    with fake_server([chat_stream(["ok"])]) as (url, requests):
        provider = OpenAICompatProvider(
            name="vllm", model="qwen", base_url=url + "/v1", api_key=None, max_retries=0
        )
        run_turn(provider)
    assert provider.name == "vllm" and requests[0]["body"]["model"] == "qwen"


# -- The same brain tool turn through either adapter -----------------------------------------


def claude_tool_script():
    return [
        claude_message([("tool_use", "toolu_1", "device_health", ["{}"])], stop_reason="tool_use"),
        claude_message([("text", "Your battery is at 82%.", ["Your battery is at 82%."])]),
    ]


def openai_tool_script():
    return [
        chat_stream(tool_calls=[(0, "call_1", "device_health", ["{}"])], finish="tool_calls"),
        chat_stream(["Your battery is at 82%."]),
    ]


@pytest.mark.parametrize(
    "make, script",
    [
        (lambda url: ClaudeProvider(api_key="k", base_url=url, max_retries=0), claude_tool_script),
        (lambda url: provider_for(url), openai_tool_script),
    ],
    ids=["claude", "openai"],
)
def test_the_brain_checks_the_battery_through_either_adapter(make, script):
    hub = hub_with_cores3()
    with fake_server(script()) as (url, _):
        brain = Brain(hub, make(url), CONFIG)

        async def turn():
            return "".join([p async for p in brain(text_turn("How's your battery?"))])

        reply = asyncio.run(turn())
    assert reply == "Your battery is at 82%."
    assert hub.invoked == [("homelink-4d6734", "device.health", {}, 30.0)]
