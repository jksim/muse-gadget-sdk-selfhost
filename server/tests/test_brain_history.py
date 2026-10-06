"""Conversation history: per node, append-only, with rollover and retention."""

import asyncio
import json

import pytest
from test_brain import CONFIG, FakeProvider, text_turn

from musehost.brain import Brain
from musehost.brain.history import History
from musehost.brain.providers.base import Done, Text
from musehost.hub import Hub
from musehost.store import Store


class Clock:
    def __init__(self, now=1_000_000.0):
        self.now = now

    def __call__(self):
        return self.now


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def history(tmp_path, clock):
    return History(Store.open(tmp_path / "musehost.db"), clock=clock)


def msgs(*texts):
    return [
        {"role": "user" if i % 2 == 0 else "assistant", "content": t} for i, t in enumerate(texts)
    ]


def test_a_conversation_continues_within_the_idle_window(history, clock):
    first = history.current("homelink-4d6734", "claude", "claude-opus-5-5")
    history.append(first.id, msgs("hi", "hello"))
    clock.now += 29 * 60
    again = history.current("homelink-4d6734", "claude", "claude-opus-5-5")
    assert again.id == first.id and again.messages == msgs("hi", "hello")


def test_thirty_idle_minutes_start_a_new_conversation(history, clock):
    first = history.current("homelink-4d6734", "claude", "claude-opus-5-5")
    history.append(first.id, msgs("hi", "hello"))
    clock.now += 31 * 60
    later = history.current("homelink-4d6734", "claude", "claude-opus-5-5")
    assert later.id != first.id and later.messages == []


def test_forty_messages_roll_over(history):
    conv = history.current("homelink-4d6734", "claude", "m")
    history.append(conv.id, msgs(*[str(i) for i in range(40)]))
    assert history.current("homelink-4d6734", "claude", "m").id != conv.id


@pytest.mark.parametrize("provider, model", [("openai", "m"), ("claude", "other-model")])
def test_a_provider_or_model_change_starts_fresh(history, provider, model):
    conv = history.current("homelink-4d6734", "claude", "m")
    history.append(conv.id, msgs("hi", "hello"))
    assert history.current("homelink-4d6734", provider, model).messages == []


def test_nodes_have_separate_conversations(history):
    a = history.current("homelink-4d6734", "claude", "m")
    history.append(a.id, msgs("hi", "hello"))
    assert history.current("operator", "claude", "m").messages == []


def test_starting_new_on_request(history):
    conv = history.current("operator", "claude", "m")
    history.append(conv.id, msgs("hi", "hello"))
    history.start_new("operator")
    assert history.current("operator", "claude", "m").messages == []


def test_native_content_round_trips_exactly(history):
    claude_turn = [
        {"role": "user", "content": "Who are you?"},
        {
            "role": "assistant",
            "content": [
                {"type": "thinking", "thinking": "", "signature": "EqQBCkYIBxgCKkB+/=="},
                {"type": "text", "text": "I'm Clio — “nice” to meet you. 🙂"},
            ],
        },
    ]
    conv = history.current("homelink-4d6734", "claude", "m")
    history.append(conv.id, claude_turn)
    loaded = history.current("homelink-4d6734", "claude", "m").messages
    assert json.dumps(loaded) == json.dumps(claude_turn)


def test_stored_rows_are_only_ever_inserted(history, clock):
    conv = history.current("homelink-4d6734", "claude", "m")
    history.append(conv.id, msgs("one", "two"))
    before = history.store.db.execute(
        "SELECT seq, content_json FROM conversation_messages ORDER BY seq"
    ).fetchall()
    history.append(conv.id, msgs("three", "four"))
    after = history.store.db.execute(
        "SELECT seq, content_json FROM conversation_messages ORDER BY seq"
    ).fetchall()
    assert [tuple(r) for r in after[: len(before)]] == [tuple(r) for r in before]
    assert len(after) == 4


def test_conversations_older_than_thirty_days_are_deleted(history, clock):
    old = history.current("homelink-4d6734", "claude", "m")
    history.append(old.id, msgs("hi", "hello"))
    clock.now += 31 * 24 * 3600
    history.current("homelink-4d6734", "claude", "m")  # triggers retention
    count = history.store.db.execute(
        "SELECT count(*) FROM conversation_messages WHERE conversation_id = ?", (old.id,)
    ).fetchone()[0]
    assert count == 0


def test_the_brain_passes_earlier_turns_to_the_provider_and_saves_new_ones(tmp_path):
    history = History(Store.open(tmp_path / "musehost.db"))
    provider = FakeProvider(Text("Hi!"), Done("end_turn", {}))
    brain = Brain(Hub(), provider, CONFIG, history=history)

    async def turn(text):
        return "".join([p async for p in brain(text_turn(text))])

    asyncio.run(turn("first"))
    asyncio.run(turn("second"))
    first_call, second_call = provider.calls
    assert first_call["history"] == []
    assert [m["role"] for m in second_call["history"]] == ["user", "assistant"]


def test_a_failed_turn_saves_nothing(tmp_path):
    from musehost.brain.providers.base import ProviderError

    history = History(Store.open(tmp_path / "musehost.db"))
    provider = FakeProvider(error=ProviderError("unavailable"))
    provider.script = []
    brain = Brain(Hub(), provider, CONFIG, history=history)

    async def turn():
        return [p async for p in brain(text_turn("hi"))]

    asyncio.run(turn())
    assert history.current("homelink-4d6734", "fake", "fake-1").messages == []


# -- Tool / prompt changes start a fresh conversation (Claude binds thinking to them) -------


def test_a_different_tools_fingerprint_starts_a_fresh_conversation(history):
    conv = history.current("homelink-4d6734", "claude", "m", fingerprint="tools-a")
    history.append(conv.id, msgs("hi", "hello"))
    assert history.current("homelink-4d6734", "claude", "m", fingerprint="tools-a").id == conv.id
    assert history.current("homelink-4d6734", "claude", "m", fingerprint="tools-b").messages == []


def test_conversations_from_before_fingerprints_are_not_continued(history):
    conv = history.current("homelink-4d6734", "claude", "m")  # no fingerprint (older rows)
    history.append(conv.id, msgs("hi", "hello"))
    assert history.current("homelink-4d6734", "claude", "m", fingerprint="x").messages == []


def test_the_brain_starts_fresh_when_the_devices_tools_change(tmp_path):
    from test_brain_tools import CORES3_COMMANDS, hub_with_cores3

    history = History(Store.open(tmp_path / "musehost.db"))
    hub = hub_with_cores3()
    provider = FakeProvider()
    brain = Brain(hub, provider, CONFIG, history=history)

    async def turn():
        return [p async for p in brain(text_turn("hi"))]

    asyncio.run(turn())
    asyncio.run(turn())
    # The gadget now advertises one command fewer (e.g. after a firmware change).
    smaller = {k: v for k, v in CORES3_COMMANDS.items() if k != "display.show_animation"}
    hub.register(
        "homelink-4d6734",
        {"display_name": "CoreS3", "platform": "esp32", "commands_v2": smaller},
        object(),
    )
    asyncio.run(turn())
    assert [len(call["history"]) for call in provider.calls] == [0, 2, 0]


def test_a_stale_history_rejection_retries_once_with_a_fresh_conversation(tmp_path):
    from musehost.brain.providers.base import ProviderError

    history = History(Store.open(tmp_path / "musehost.db"))
    attempts = []

    class StaleOnce:
        name, model = "claude", "m"

        async def stream_turn(self, *, history, user_text, **kwargs):
            attempts.append(len(history))
            if len(attempts) == 1:
                raise ProviderError("stale_history", "bound to a different conversation")
            yield Text("Fresh start.")
            yield Done(
                "end_turn",
                {},
                [{"role": "user", "content": user_text}, {"role": "assistant", "content": "x"}],
            )

    conv = history.current("homelink-4d6734", "claude", "m", fingerprint=None)
    history.append(conv.id, msgs("old", "turn"))
    brain = Brain(Hub(), StaleOnce(), CONFIG, history=history)

    async def turn():
        return "".join([p async for p in brain(text_turn("hi"))])

    assert asyncio.run(turn()) == "Fresh start."
    assert attempts[-1] == 0
