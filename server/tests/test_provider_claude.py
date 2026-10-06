"""The Claude adapter, using the real anthropic SDK against a local fake Messages API."""

import asyncio

import pytest
from fake_llm import claude_error, claude_message, fake_server
from test_brain import CONFIG, text_turn

from musehost.brain import Brain
from musehost.brain.providers.base import Done, ProviderError, Text
from musehost.brain.providers.claude import ClaudeProvider, history_content
from musehost.hub import Hub


def provider_for(base_url, **kwargs):
    return ClaudeProvider(api_key="test-key", base_url=base_url, max_retries=0, **kwargs)


async def no_tool(call):
    raise AssertionError("no tools in this test")


def run_turn(provider, history=None, text="Who are you?"):
    async def scenario():
        return [
            event
            async for event in provider.stream_turn(
                system="You are Clio.",
                history=history or [],
                user_text=text,
                tools=[],
                run_tool=no_tool,
                max_rounds=5,
            )
        ]

    return asyncio.run(scenario())


def test_text_streams_and_the_request_has_every_setting_the_spec_asks_for():
    reply = claude_message(
        [("thinking", "sig-abc=="), ("text", "I'm Clio.", ["I'm ", "Clio."])],
        usage={"cache_read_input_tokens": 0},
    )
    with fake_server([reply]) as (url, requests):
        events = run_turn(provider_for(url))
    texts = [e.text for e in events if isinstance(e, Text)]
    assert texts == ["I'm ", "Clio."]
    [request] = requests
    body, headers = request["body"], {k.lower(): v for k, v in request["headers"].items()}
    assert request["path"].startswith("/v1/messages")
    assert body["model"] == "claude-opus-5-5"
    assert body["stream"] is True
    assert body["output_config"] == {"effort": "low"}
    assert body["cache_control"] == {"type": "ephemeral"}
    assert body["fallbacks"] == "default"
    assert "server-side-fallback-2026-07-01" in headers["anthropic-beta"]
    assert headers["x-api-key"] == "test-key"
    assert body["system"] == "You are Clio."
    assert body["messages"] == [{"role": "user", "content": "Who are you?"}]
    assert "thinking" not in body  # Opus 5.5: thinking is always on; don't configure it
    done = events[-1]
    assert isinstance(done, Done) and done.stop_reason == "end_turn"
    assert done.usage["output_tokens"] == 42


def test_the_assistant_turn_is_kept_exactly_for_history():
    reply = claude_message([("thinking", "sig-abc=="), ("text", "Hi.", ["Hi."])])
    with fake_server([reply]) as (url, _):
        done = run_turn(provider_for(url))[-1]
    user, assistant = done.messages
    assert user == {"role": "user", "content": "Who are you?"}
    assert assistant["role"] == "assistant"
    assert assistant["content"] == [
        {"type": "thinking", "thinking": "", "signature": "sig-abc=="},
        {"type": "text", "text": "Hi."},
    ]


def test_history_is_sent_before_the_new_message():
    history = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": [{"type": "text", "text": "one"}]},
    ]
    with fake_server([claude_message([("text", "two", ["two"])])]) as (url, requests):
        run_turn(provider_for(url), history=history, text="second")
    assert requests[0]["body"]["messages"] == [*history, {"role": "user", "content": "second"}]


def test_a_configured_model_and_effort_are_used():
    with fake_server([claude_message([("text", "ok", ["ok"])])]) as (url, requests):
        run_turn(provider_for(url, model="claude-sonnet-5-5", effort="medium"))
    assert requests[0]["body"]["model"] == "claude-sonnet-5-5"
    assert requests[0]["body"]["output_config"] == {"effort": "medium"}


@pytest.mark.parametrize(
    "reply, kind",
    [
        (claude_error(401, "authentication_error"), "auth"),
        (claude_error(403, "permission_error"), "auth"),
        (claude_error(429, "rate_limit_error"), "unavailable"),
        (claude_error(529, "overloaded_error"), "unavailable"),
        (claude_error(500, "api_error"), "unavailable"),
        (claude_error(404, "not_found_error"), "unavailable"),
        (claude_message([("text", "I", ["I"])], stop_reason="refusal"), "refused"),
    ],
)
def test_failures_are_classified(reply, kind):
    with fake_server([reply]) as (url, _):
        with pytest.raises(ProviderError) as caught:
            run_turn(provider_for(url))
    assert caught.value.kind == kind


def test_an_unreachable_api_is_unavailable():
    import socket

    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    with pytest.raises(ProviderError) as caught:
        run_turn(provider_for(f"http://127.0.0.1:{port}"))
    assert caught.value.kind == "unavailable"


def test_the_brain_answers_through_the_claude_adapter():
    reply = claude_message([("text", "Hello from Clio.", ["Hello ", "from Clio."])])
    with fake_server([reply]) as (url, _):
        brain = Brain(Hub(), provider_for(url), CONFIG)

        async def turn():
            return "".join([p async for p in brain(text_turn())])

        assert asyncio.run(turn()) == "Hello from Clio."


# -- Replaying content after a mid-output fallback ----------------------------------------


def test_content_without_a_fallback_is_kept_as_is():
    content = [
        {"type": "thinking", "thinking": "", "signature": "s"},
        {"type": "text", "text": "a"},
    ]
    assert history_content(content) == content


def test_blocks_before_a_mid_output_fallback_are_cleaned_for_replay():
    content = [
        {"type": "thinking", "thinking": "", "signature": "s1"},
        {"type": "text", "text": "Partial "},
        {"type": "tool_use", "id": "t1", "name": "x", "input": {}},
        {"type": "server_tool_use", "id": "s1", "name": "web_search", "input": {}},
        {"type": "server_tool_use", "id": "s2", "name": "web_search", "input": {}},
        {"type": "web_search_tool_result", "tool_use_id": "s2", "content": []},
        {"type": "fallback", "from": {"model": "a"}, "to": {"model": "b"}},
        {"type": "thinking", "thinking": "", "signature": "s3"},
        {"type": "text", "text": "rest"},
    ]
    assert history_content(content) == [
        {"type": "text", "text": "Partial "},
        {"type": "server_tool_use", "id": "s2", "name": "web_search", "input": {}},
        {"type": "web_search_tool_result", "tool_use_id": "s2", "content": []},
        {"type": "thinking", "thinking": "", "signature": "s3"},
        {"type": "text", "text": "rest"},
    ]


# -- Tools and web search (B5) --------------------------------------------------------------

import json  # noqa: E402

from musehost.brain.providers.base import Status, ToolCall, ToolResult, ToolSpec  # noqa: E402

HEALTH = ToolSpec(
    "device_health",
    "Report battery.",
    {"type": "object", "properties": {}, "required": [], "additionalProperties": False},
)
DRAW = ToolSpec(
    "display_draw_url",
    "Draw an image.",
    {
        "type": "object",
        "properties": {"url": {"type": "string"}},
        "required": ["url"],
        "additionalProperties": False,
    },
)


def tool_turn(provider, *, tools=(HEALTH,), result=None, max_rounds=5):
    calls = []

    async def run_tool(call):
        calls.append(call)
        return result or ToolResult(json.dumps({"ok": True, "payload": {"battery_pct": 82}}))

    async def scenario():
        return [
            e
            async for e in provider.stream_turn(
                system="You are Clio.",
                history=[],
                user_text="How's your battery?",
                tools=list(tools),
                run_tool=run_tool,
                max_rounds=max_rounds,
            )
        ]

    return asyncio.run(scenario()), calls


def test_a_tool_call_runs_and_its_result_goes_back_in_one_user_message():
    first = claude_message(
        [
            ("text", "Let me check.", ["Let me check."]),
            ("tool_use", "toolu_1", "device_health", ["{", "}"]),
        ],
        stop_reason="tool_use",
    )
    second = claude_message([("text", "Your battery is at 82%.", ["Your battery is at 82%."])])
    with fake_server([first, second]) as (url, requests):
        events, calls = tool_turn(provider_for(url))
    assert calls == [ToolCall("toolu_1", "device_health", {})]
    texts = "".join(e.text for e in events if isinstance(e, Text))
    assert texts == "Let me check. Your battery is at 82%."
    tools = requests[0]["body"]["tools"]
    assert tools[0]["name"] == "device_health" and tools[0]["eager_input_streaming"] is True
    sent = requests[1]["body"]["messages"]
    assert sent[1]["role"] == "assistant"
    assert sent[1]["content"][1] == {
        "type": "tool_use",
        "id": "toolu_1",
        "name": "device_health",
        "input": {},
    }
    assert sent[2] == {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": "toolu_1",
                "content": json.dumps({"ok": True, "payload": {"battery_pct": 82}}),
                "is_error": False,
            }
        ],
    }
    done = events[-1]
    assert [m["role"] for m in done.messages] == ["user", "assistant", "user", "assistant"]
    assert done.messages[:3] == sent


def test_a_tool_input_split_over_deltas_is_assembled():
    first = claude_message(
        [
            (
                "tool_use",
                "toolu_2",
                "display_draw_url",
                ['{"url": "https://exa', 'mple.com/cat.jpg"}'],
            )
        ],
        stop_reason="tool_use",
    )
    second = claude_message([("text", "There's a cat.", ["There's a cat."])])
    with fake_server([first, second]) as (url, _):
        _, calls = tool_turn(provider_for(url), tools=(DRAW,))
    assert calls[0].input == {"url": "https://example.com/cat.jpg"}


def test_a_tool_error_is_marked_for_the_model():
    first = claude_message(
        [("tool_use", "toolu_3", "device_health", ["{}"])], stop_reason="tool_use"
    )
    second = claude_message([("text", "I couldn't check.", ["I couldn't check."])])
    with fake_server([first, second]) as (url, requests):
        tool_turn(provider_for(url), result=ToolResult('{"error": "offline"}', is_error=True))
    assert requests[1]["body"]["messages"][2]["content"][0]["is_error"] is True


def test_no_tool_runs_when_the_reply_hit_max_tokens():
    first = claude_message(
        [("tool_use", "toolu_4", "display_draw_url", ['{"url": "https://ex'])],
        stop_reason="max_tokens",
    )
    with fake_server([first]) as (url, requests):
        events, calls = tool_turn(provider_for(url), tools=(DRAW,))
    assert calls == [] and len(requests) == 1 and events[-1].stop_reason == "max_tokens"


def test_pause_turn_is_resumed_and_web_search_shows_as_working():
    search = (
        "raw",
        {
            "type": "server_tool_use",
            "id": "srvtoolu_1",
            "name": "web_search",
            "input": {"query": "weather"},
        },
    )
    found = (
        "raw",
        {"type": "web_search_tool_result", "tool_use_id": "srvtoolu_1", "content": []},
    )
    first = claude_message([search, found], stop_reason="pause_turn")
    second = claude_message([("text", "Sunny, 21 degrees.", ["Sunny, 21 degrees."])])
    with fake_server([first, second]) as (url, requests):
        events, _ = tool_turn(provider_for(url, web_search=True))
    assert Status("working") in events
    assert requests[1]["body"]["messages"][1]["role"] == "assistant"
    assert requests[1]["body"]["messages"][1]["content"][0]["type"] == "server_tool_use"
    assert "".join(e.text for e in events if isinstance(e, Text)) == "Sunny, 21 degrees."


def test_web_search_is_offered_only_when_enabled():
    with fake_server([claude_message([("text", "ok", ["ok"])])] * 2) as (url, requests):
        tool_turn(provider_for(url, web_search=True))
        tool_turn(provider_for(url, web_search=False))
    on, off = (r["body"].get("tools", []) for r in requests)
    assert {"type": "web_search_20260209", "name": "web_search", "max_uses": 2} in on
    assert all(t.get("name") != "web_search" for t in off)


def test_after_the_last_allowed_round_claude_must_answer():
    first = claude_message(
        [("tool_use", "toolu_5", "device_health", ["{}"])], stop_reason="tool_use"
    )
    second = claude_message([("text", "82%.", ["82%."])])
    with fake_server([first, second]) as (url, requests):
        tool_turn(provider_for(url), max_rounds=1)
    assert "tool_choice" not in requests[0]["body"]
    assert requests[1]["body"]["tool_choice"] == {"type": "none"}


def test_with_no_tools_and_no_web_search_none_are_sent():
    with fake_server([claude_message([("text", "hi", ["hi"])])]) as (url, requests):
        tool_turn(provider_for(url), tools=())
    assert "tools" not in requests[0]["body"]


def test_a_turn_cut_off_mid_tool_call_is_not_kept_in_history():
    # Its tool_use would have no tool_result, which the API rejects next turn.
    first = claude_message(
        [("tool_use", "toolu_6", "display_draw_url", ['{"url": "https://ex'])],
        stop_reason="max_tokens",
    )
    with fake_server([first]) as (url, _):
        events, _ = tool_turn(provider_for(url), tools=(DRAW,))
    assert events[-1].stop_reason == "max_tokens" and events[-1].messages == []


def test_history_bound_to_another_conversation_is_reported_as_stale(caplog):
    message = (
        "messages.1.content.0: Invalid `signature` in `thinking` block. The block is bound "
        "to a different conversation. Remove the block, or set "
        '`thinking.block_binding.prefix_mismatch_behavior` to "drop_block".'
    )
    with fake_server([claude_error(400, "invalid_request_error", message)]) as (url, _):
        with pytest.raises(ProviderError) as caught:
            run_turn(provider_for(url))
    assert caught.value.kind == "stale_history"


def test_the_api_error_message_is_logged_for_a_rejected_request(caplog):
    import logging

    caplog.set_level(logging.ERROR)
    with fake_server([claude_error(400, "invalid_request_error", "tools.0: bad schema")]) as (
        url,
        _,
    ):
        with pytest.raises(ProviderError):
            run_turn(provider_for(url))
    assert "tools.0: bad schema" in caplog.text
