"""The dashboard's chat with Clio (as the operator) and the conversation history."""

import asyncio
import contextlib
import html
import logging
import re

import httpx
from test_brain import FakeProvider
from test_brain_tools import CORES3_COMMANDS, ToolCallingProvider

from musehost.brain import Brain
from musehost.brain.history import History
from musehost.brain.providers.base import Done, ProviderError, Text, ToolCall
from musehost.dashboard import auth
from musehost.hub import InvokeResult

GOOD = "correct horse battery"
NODE = "homelink-4d6734"
SECRET_TEXT = "PLANTED-chat-words"


class FakeLink:
    async def send(self, message):
        pass

    async def invoke(self, command, params, timeout_s):
        return InvokeResult(ok=True, payload={"battery_pct": 77})


@contextlib.asynccontextmanager
async def chat_dashboard(state, provider):
    from musehost.app import create_app

    app = create_app(state)
    app.state.hub.chat_handler = Brain(
        app.state.hub, provider, app.state.config, History(app.state.tokens.store)
    )
    auth.set_password(state, GOOD)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://muse-host.local") as c:
        await c.post("/dashboard/login", data={"password": GOOD})
        page = (await c.get("/dashboard/chat")).text
        csrf = re.search(r'name="csrf-token" content="([^"]+)"', page).group(1)
        yield app, c, csrf


async def say(app, c, csrf, text="hello", device=""):
    """Send one message; returns the finished turn's SSE stream as text."""
    sent = await c.post(
        "/dashboard/chat/send", data={"csrf": csrf, "message": text, "device": device}
    )
    assert sent.status_code == 200, sent.text
    events_url = re.search(r'sse-connect="([^"]+)"', sent.text).group(1)
    job = app.state.jobs.get(events_url.rstrip("/").split("/")[-2])
    await asyncio.wait_for(job.done.wait(), 10)
    return (await c.get(events_url)).text


def pieces(stream):
    return [m for m in re.findall(r"event: piece\ndata: (.*)\n", stream)]


def test_a_reply_streams_in_order(state):
    provider = FakeProvider(Text("Hello "), Text("operator."), Done("end_turn", {}))

    async def scenario():
        async with chat_dashboard(state, provider) as (app, c, csrf):
            stream = await say(app, c, csrf, "hi Clio")
            assert pieces(stream) == ["Hello ", "operator."]
            assert stream.rstrip().endswith("data: done") or "event: done" in stream
            assert provider.calls[0]["tools"] == []  # no gadget: no tools
            assert "hi Clio" in provider.calls[0]["user_text"]

    asyncio.run(scenario())


def test_with_a_gadgets_tools_clio_can_use_them_in_the_operator_conversation(state):
    provider = ToolCallingProvider(ToolCall("t1", "device_health", {}))

    async def scenario():
        async with chat_dashboard(state, provider) as (app, c, csrf):
            app.state.hub.register(
                NODE, {"display_name": "CoreS3", "commands_v2": CORES3_COMMANDS}, FakeLink()
            )
            page = (await c.get("/dashboard/chat")).text
            assert f'value="{NODE}"' in page
            await say(app, c, csrf, "how's the battery?", device=NODE)
            offered = {t.name for t in provider.tools_offered}
            assert "device_health" in offered and not any("ota" in n for n in offered)
            assert '"battery_pct": 77' in provider.results[0].content
            owners = {
                r[0] for r in app.state.tokens.store.db.execute("SELECT node_id FROM conversations")
            }
            assert owners == {"operator"}

    asyncio.run(scenario())


def test_new_conversation_starts_afresh(state):
    provider = FakeProvider()

    async def scenario():
        async with chat_dashboard(state, provider) as (app, c, csrf):
            await say(app, c, csrf)
            await say(app, c, csrf)
            assert (await c.post("/dashboard/chat/new", data={"csrf": csrf})).status_code == 303
            await say(app, c, csrf)
            assert [len(call["history"]) for call in provider.calls] == [0, 2, 0]

    asyncio.run(scenario())


def test_a_second_send_while_clio_is_answering_is_refused(state):
    gate = asyncio.Event()

    class Slow(FakeProvider):
        async def stream_turn(self, **kwargs):
            await gate.wait()
            async for event in super().stream_turn(**kwargs):
                yield event

    async def scenario():
        async with chat_dashboard(state, Slow()) as (app, c, csrf):
            first = await c.post("/dashboard/chat/send", data={"csrf": csrf, "message": "a"})
            assert first.status_code == 200
            second = await c.post("/dashboard/chat/send", data={"csrf": csrf, "message": "b"})
            assert second.status_code == 409
            gate.set()
            job = app.state.jobs.running("chat")
            await asyncio.wait_for(job.done.wait(), 10)

    asyncio.run(scenario())


def test_a_provider_failure_shows_clios_failure_reply(state):
    provider = FakeProvider(error=ProviderError("unavailable", "boom"))
    provider.script = []

    async def scenario():
        async with chat_dashboard(state, provider) as (app, c, csrf):
            stream = await say(app, c, csrf)
            assert "couldn't reach my brain" in html.unescape("".join(pieces(stream)))

    asyncio.run(scenario())


def test_the_reply_is_escaped(state):
    provider = FakeProvider(Text("<script>alert(1)</script>"), Done("end_turn", {}))

    async def scenario():
        async with chat_dashboard(state, provider) as (app, c, csrf):
            stream = await say(app, c, csrf)
            assert "<script>" not in stream and "&lt;script&gt;" in stream

    asyncio.run(scenario())


def test_the_page_runs_no_inline_script(state):
    async def scenario():
        async with chat_dashboard(state, FakeProvider()) as (app, c, csrf):
            for text in (
                (await c.get("/dashboard/chat")).text,
                (await c.post("/dashboard/chat/send", data={"csrf": csrf, "message": "hi"})).text,
            ):
                assert "hx-on" not in text and "<script>" not in text
                assert not re.search(r"\son\w+=", text)
            job_id = re.search(r"/chat/turns/([^/]+)/events", text).group(1)
            await asyncio.wait_for(app.state.jobs.get(job_id).done.wait(), 10)

    asyncio.run(scenario())


def test_sending_needs_the_csrf_token_and_a_message(state):
    async def scenario():
        async with chat_dashboard(state, FakeProvider()) as (app, c, csrf):
            assert (await c.post("/dashboard/chat/send", data={"message": "x"})).status_code == 403
            empty = await c.post("/dashboard/chat/send", data={"csrf": csrf, "message": "  "})
            assert empty.status_code == 400
            assert (await c.post("/dashboard/chat/new", data={})).status_code == 403

    asyncio.run(scenario())


def test_chat_text_never_reaches_the_logs(state, caplog):
    from musehost.dashboard import logring

    caplog.set_level(logging.DEBUG)
    ring = logring.install()
    ring.clear()
    provider = FakeProvider(Text(SECRET_TEXT + " reply"), Done("end_turn", {}))

    async def scenario():
        async with chat_dashboard(state, provider) as (app, c, csrf):
            await say(app, c, csrf, SECRET_TEXT + " question")

    asyncio.run(scenario())
    assert SECRET_TEXT not in caplog.text
    assert SECRET_TEXT not in "\n".join(ring.lines())


# -- T10: history ------------------------------------------------------------------------------

from starlette.testclient import TestClient  # noqa: E402

CLAUDE_TURN = [
    {
        "role": "user",
        "content": "[Context: local time Tuesday 07 October 2026, 20:15 BST; "
        "speaking through Kitchen CoreS3 (esp32).]\nhow's the battery?",
    },
    {
        "role": "assistant",
        "content": [
            {"type": "thinking", "thinking": "PLANTED-THINKING", "signature": "sig"},
            {"type": "redacted_thinking", "data": "PLANTED-REDACTED"},
            {"type": "tool_use", "id": "t1", "name": "device_health", "input": {}},
        ],
    },
    {
        "role": "user",
        "content": [
            {"type": "tool_result", "tool_use_id": "t1", "content": '{"battery_pct": 77}'},
        ],
    },
    {"role": "assistant", "content": [{"type": "text", "text": "It's at 77%. <script>x</script>"}]},
]
OPENAI_TURN = [
    {"role": "user", "content": "draw a cat"},
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "display_draw_url", "arguments": "{}"},
            },
        ],
    },
    {"role": "tool", "tool_call_id": "c1", "content": '{"ok": false, "error": "no image"}'},
    {"role": "assistant", "content": "I couldn't draw it."},
]


def history_client(state, conversations):
    from musehost.app import create_app

    app = create_app(state)
    history = History(app.state.tokens.store)
    app.state.tokens.enroll(NODE, "Kitchen CoreS3")
    ids = []
    for node, provider, messages in conversations:
        conv = history.current(node, provider, "m")
        history.append(conv.id, messages)
        history.start_new(node)
        ids.append(conv.id)
    auth.set_password(state, GOOD)
    client = TestClient(app, base_url="https://muse-host.local")
    client.post("/dashboard/login", data={"password": GOOD})
    return client, ids


def test_the_history_lists_conversations_newest_first(state):
    client, ids = history_client(
        state,
        [
            (NODE, "claude", CLAUDE_TURN),
            ("operator", "openai", OPENAI_TURN),
        ],
    )
    text = client.get("/dashboard/history").text
    assert "Kitchen CoreS3" in text and "operator" in text.lower()
    assert text.index(f"/dashboard/history/{ids[1]}") < text.index(f"/dashboard/history/{ids[0]}")
    assert "claude" in text and "openai" in text
    assert "how's the battery" not in text  # the list shows no content


def test_a_claude_conversation_shows_text_and_tool_use_but_not_thinking(state):
    client, ids = history_client(state, [(NODE, "claude", CLAUDE_TURN)])
    text = client.get(f"/dashboard/history/{ids[0]}").text
    assert "how&#39;s the battery?" in text or "how's the battery?" in text
    assert "[Context:" not in text and "20:15" in text  # the turn's time, not the raw context
    assert "used device_health: ok" in text
    assert "PLANTED" not in text
    assert "<script>x" not in text and "&lt;script&gt;x" in text


def test_an_openai_conversation_shows_its_tool_calls(state):
    client, ids = history_client(state, [("operator", "openai", OPENAI_TURN)])
    text = client.get(f"/dashboard/history/{ids[0]}").text
    assert "draw a cat" in text and "used display_draw_url: failed" in text
    assert "couldn" in text


def test_the_history_pages_by_25(state):
    client, ids = history_client(
        state, [("operator", "claude", [{"role": "user", "content": f"m{i}"}]) for i in range(30)]
    )
    first = client.get("/dashboard/history").text
    second = client.get("/dashboard/history?page=2").text
    assert first.count('href="/dashboard/history/') == 25
    assert f"/dashboard/history/{ids[0]}" in second and "?page=2" in first
    assert client.get("/dashboard/history?page=x").status_code == 200


def test_an_unknown_conversation_is_a_404(state):
    client, _ = history_client(state, [])
    assert client.get("/dashboard/history/999").status_code == 404
    assert client.get("/dashboard/history/abc").status_code == 404


def test_reading_history_logs_no_content(state, caplog):
    caplog.set_level(logging.DEBUG)
    client, ids = history_client(state, [(NODE, "claude", CLAUDE_TURN)])
    client.get(f"/dashboard/history/{ids[0]}")
    assert "battery" not in caplog.text
