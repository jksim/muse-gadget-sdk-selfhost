"""Hermes Agent as Clio's brain: its Responses API, against a local fake Hermes."""

import asyncio
import socket

import pytest
from fake_llm import Reply, fake_server

from musehost.brain.providers.base import Done, ProviderError, Status, Text
from musehost.brain.providers.hermes import HermesProvider


def provider_for(url, **kwargs):
    kwargs.setdefault("timeout_s", 10)
    return HermesProvider(api_key="hermes-key", base_url=url + "/v1", **kwargs)


def run_turn(provider, *, conversation="musehost-7", node_id="homelink-4d6734"):
    async def scenario():
        return [
            e
            async for e in provider.stream_turn(
                system="You are Clio.",
                history=[{"role": "user", "content": "ignored: Hermes keeps history"}],
                user_text="How's my gadget?",
                tools=[],
                run_tool=None,
                max_rounds=5,
                conversation=conversation,
                node_id=node_id,
            )
        ]

    return asyncio.run(scenario())


def text_item(item_id, phase=None):
    item = {"id": item_id, "type": "message", "role": "assistant", "content": []}
    if phase:
        item["phase"] = phase
    return ("response.output_item.added", {"type": "response.output_item.added", "item": item})


def delta(item_id, text):
    return (
        "response.output_text.delta",
        {"type": "response.output_text.delta", "item_id": item_id, "delta": text},
    )


COMPLETED = (
    "response.completed",
    {
        "type": "response.completed",
        "response": {
            "id": "resp_1",
            "status": "completed",
            "usage": {"input_tokens": 40, "output_tokens": 9},
        },
    },
)


def answer(*parts):
    return Reply(
        events=[
            ("response.created", {"type": "response.created", "response": {"id": "resp_1"}}),
            text_item("msg_1"),
            *(delta("msg_1", p) for p in parts),
            COMPLETED,
        ]
    )


def test_the_request_is_a_streamed_responses_call_in_a_named_conversation():
    with fake_server([answer("Hi.")]) as (url, requests):
        run_turn(provider_for(url))
    [request] = requests
    assert request["path"] == "/v1/responses"
    assert request["headers"]["Authorization"] == "Bearer hermes-key"
    body = request["body"]
    assert body["model"] == "hermes-agent"
    assert body["input"] == "How's my gadget?"
    assert body["conversation"] == "musehost-7"
    assert body["stream"] is True and body["store"] is True
    assert body["instructions"].startswith("You are Clio.")
    assert 'gadget="homelink-4d6734"' in body["instructions"]
    assert "tools" not in body  # Hermes runs its own tools (the gadgets come via MCP)


def test_text_streams_and_history_records_the_exchange():
    with fake_server([answer("Hello ", "there.")]) as (url, _):
        events = run_turn(provider_for(url))
    assert [e.text for e in events if isinstance(e, Text)] == ["Hello ", "there."]
    [done] = [e for e in events if isinstance(e, Done)]
    assert done.stop_reason == "completed"
    assert done.usage == {"input_tokens": 40, "output_tokens": 9}
    assert done.messages == [
        {"role": "user", "content": "How's my gadget?"},
        {"role": "assistant", "content": "Hello there."},
    ]


def test_commentary_tool_progress_and_keepalives_are_not_spoken():
    events = [
        text_item("c1", phase="commentary"),
        delta("c1", "Let me check the gadget..."),
        (":", "keepalive"),
        (
            "response.output_item.added",
            {
                "type": "response.output_item.added",
                "item": {
                    "id": "fc1",
                    "type": "function_call",
                    "name": "mcp_musehost_device_health",
                },
            },
        ),
        ("hermes.tool.progress", {"tool": "mcp_musehost_device_health", "status": "running"}),
        ("response.some_future_event", {"type": "response.some_future_event"}),
        text_item("m1"),
        delta("m1", "Battery is at 80 percent."),
        COMPLETED,
    ]
    with fake_server([Reply(events=events)]) as (url, _):
        out = run_turn(provider_for(url))
    assert [e.text for e in out if isinstance(e, Text)] == ["Battery is at 80 percent."]
    assert Status("working") in out


@pytest.mark.parametrize(
    "reply, kind",
    [
        (Reply(status=401, body={"error": "bad key"}), "auth"),
        (Reply(status=403, body={"error": "forbidden"}), "auth"),
        (Reply(status=500, body={"error": "boom"}), "unavailable"),
        (
            Reply(
                events=[
                    (
                        "response.failed",
                        {"type": "response.failed", "response": {"error": {"message": "x"}}},
                    )
                ]
            ),
            "unavailable",
        ),
        (Reply(events=[text_item("m1")]), "unavailable"),  # ended without completing
    ],
)
def test_failures_become_provider_errors(reply, kind):
    with fake_server([reply]) as (url, _):
        with pytest.raises(ProviderError) as caught:
            run_turn(provider_for(url))
    assert caught.value.kind == kind


def test_hermes_not_running_is_unavailable():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    with pytest.raises(ProviderError) as caught:
        run_turn(provider_for(f"http://127.0.0.1:{port}"))
    assert caught.value.kind == "unavailable"


def test_a_turn_slower_than_the_timeout_is_unavailable():
    with fake_server([Reply(events=[COMPLETED], delay=2.0)]) as (url, _):
        with pytest.raises(ProviderError) as caught:
            run_turn(provider_for(url, timeout_s=0.5))
    assert caught.value.kind == "unavailable"


def test_nothing_secret_is_logged(caplog):
    import logging

    caplog.set_level(logging.DEBUG)
    with fake_server([answer("The vault code is 4711.")]) as (url, _):
        run_turn(provider_for(url))
    with fake_server([Reply(status=401, body={"error": "bad key hermes-key"})]) as (url, _):
        with pytest.raises(ProviderError):
            run_turn(provider_for(url))
    assert "hermes-key" not in caplog.text and "4711" not in caplog.text
    assert "How's my gadget" not in caplog.text


# -- the brain gives each musehost conversation its own Hermes conversation -------------------


def test_the_brain_names_the_conversation_and_the_gadget(tmp_path):
    from test_brain import CONFIG, text_turn

    from musehost.brain import Brain
    from musehost.brain.history import History
    from musehost.hub import Hub
    from musehost.store import Store

    with fake_server([answer("One."), answer("Two."), answer("Three.")]) as (url, requests):
        history = History(Store.open(tmp_path / "musehost.db"))
        brain = Brain(Hub(), provider_for(url), CONFIG, history)

        async def say(text):
            return "".join([p async for p in brain(text_turn(text))])

        assert asyncio.run(say("first")) == "One."
        asyncio.run(say("second"))
        history.start_new("homelink-4d6734")
        asyncio.run(say("third"))
    names = [r["body"]["conversation"] for r in requests]
    assert names[0] == names[1] and names[0].startswith("musehost-")
    assert names[2] != names[0]
    assert all('gadget="homelink-4d6734"' in r["body"]["instructions"] for r in requests)
