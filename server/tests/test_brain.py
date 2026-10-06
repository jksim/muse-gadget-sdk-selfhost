"""The brain core with a fake provider: streaming, speech states, prompt, failures."""

import asyncio
from pathlib import Path

import pytest

from musehost.brain import Brain, prompt
from musehost.brain.providers.base import Done, ProviderError, Status, Text
from musehost.config import HostConfig
from musehost.hub import ChatTurn, Hub

CONFIG = HostConfig(hostnames=("muse-host.local",), ips=(), port=443)


class FakeProvider:
    """Plays back a script of events; records what it was asked."""

    name, model = "fake", "fake-1"

    def __init__(self, *script, error=None):
        self.script = list(script) or [Text("Hello "), Text("there."), Done("end_turn", {})]
        self.error = error
        self.calls = []

    async def stream_turn(self, *, system, history, user_text, tools, run_tool, max_rounds):
        self.calls.append(
            {"system": system, "history": list(history), "user_text": user_text, "tools": tools}
        )
        for event in self.script:
            if isinstance(event, Done) and not event.messages:
                event = Done(
                    event.stop_reason,
                    event.usage,
                    [
                        {"role": "user", "content": user_text},
                        {"role": "assistant", "content": "x"},
                    ],
                )
            yield event
        if self.error is not None:
            raise self.error


def make_brain(provider, hub=None, tmp_path=None):
    return Brain(hub or Hub(), provider, CONFIG)


def collect(brain, turn) -> str:
    async def run():
        return [piece async for piece in brain(turn)]

    return "".join(asyncio.run(run()))


def text_turn(text="who are you?"):
    return ChatTurn(node_id="homelink-4d6734", message_id="u-1", text=text)


def voice_turn(status, text=None):
    return ChatTurn(
        node_id="homelink-4d6734",
        message_id="u-2",
        text=text,
        audio_path=Path("voice/n.wav"),  # never opened
        duration_s=1.5,
        transcribed=status in ("ok", "empty"),
        speech_status=status,
    )


def test_the_providers_text_streams_through_in_order():
    provider = FakeProvider(Text("I'm "), Text("Clio."), Done("end_turn", {}))
    assert collect(make_brain(provider), text_turn()) == "I'm Clio."


@pytest.mark.parametrize("status", ["empty", "loading", "failed", "off"])
def test_speech_problems_are_answered_without_the_model(status):
    provider = FakeProvider()
    reply = collect(make_brain(provider), voice_turn(status))
    assert provider.calls == [] and reply


def test_a_transcribed_note_goes_to_the_model():
    provider = FakeProvider()
    collect(make_brain(provider), voice_turn("ok", "What's the weather like today?"))
    assert "What's the weather like today?" in provider.calls[0]["user_text"]


def test_the_system_prompt_is_fixed_and_context_goes_in_the_user_message():
    provider = FakeProvider()
    brain = make_brain(provider)
    collect(brain, text_turn("first"))
    collect(brain, text_turn("second"))
    first, second = provider.calls
    assert first["system"] == second["system"] == prompt.SYSTEM_PROMPT
    assert "Clio" in prompt.SYSTEM_PROMPT
    assert first["user_text"].endswith("first") and "local time" in first["user_text"].lower()


def test_the_device_is_named_in_the_turn_context():
    hub = Hub()
    hub.register(
        "homelink-4d6734", {"display_name": "Kitchen CoreS3", "platform": "esp32"}, object()
    )
    provider = FakeProvider()
    collect(make_brain(provider, hub), text_turn())
    assert "Kitchen CoreS3" in provider.calls[0]["user_text"]


@pytest.mark.parametrize(
    "kind, words",
    [
        ("auth", "credentials aren't working"),
        ("unavailable", "couldn't reach my brain"),
        ("refused", "can't help with that"),
    ],
)
def test_provider_failures_get_the_specs_replies(kind, words):
    provider = FakeProvider(error=ProviderError(kind, "boom"))
    provider.script = []
    assert words in collect(make_brain(provider), text_turn())


def test_a_failure_after_some_text_keeps_the_text():
    provider = FakeProvider(Text("Let me think"), error=ProviderError("unavailable", "x"))
    reply = collect(make_brain(provider), text_turn())
    assert reply.startswith("Let me think") and "couldn't reach my brain" in reply


def test_a_max_tokens_stop_keeps_the_text_so_far():
    provider = FakeProvider(Text("A very long"), Done("max_tokens", {}))
    assert collect(make_brain(provider), text_turn()) == "A very long"


def test_status_events_are_published_to_the_device():
    hub = Hub()
    published = []

    async def publish(node, event, payload):
        published.append((event, payload))

    hub.publish = publish
    provider = FakeProvider(Status("working"), Text("Done."), Done("end_turn", {}))
    collect(make_brain(provider, hub), text_turn())
    assert ("agent.status", {"activity_code": "working"}) in published


def test_brain_settings_default_per_the_spec_and_round_trip(tmp_path):
    import dataclasses

    assert (CONFIG.brain_provider, CONFIG.brain_model, CONFIG.brain_effort) == ("claude", "", "low")
    assert CONFIG.brain_web_search is True and CONFIG.brain_idle_minutes == 30
    assert CONFIG.brain_tools == (
        "device.health",
        "display.draw_url",
        "display.show_animation",
    )
    custom = dataclasses.replace(
        CONFIG,
        brain_provider="vllm",
        brain_model="qwen",
        brain_base_url="http://gpu:8000/v1",
        brain_web_search=False,
        brain_tools=("device.health",),
        brain_max_tokens=1024,
    )
    custom.save(tmp_path / "host.toml")
    assert HostConfig.load(tmp_path / "host.toml") == custom


# -- Wiring: which provider answers ---------------------------------------------------


def test_with_a_claude_key_the_brain_answers(monkeypatch):
    from musehost.app import build_brain
    from musehost.brain.providers.claude import ClaudeProvider

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    brain = build_brain(CONFIG, Hub(), store=None)
    assert isinstance(brain.provider, ClaudeProvider)
    assert brain.provider.model == "claude-opus-5-5" and brain.provider.effort == "low"


def test_without_a_key_the_placeholder_keeps_answering(monkeypatch, caplog):
    from musehost.app import build_brain

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    assert build_brain(CONFIG, Hub(), store=None) is None
    assert "ANTHROPIC_API_KEY" in caplog.text


def test_with_the_brain_off_the_placeholder_answers(monkeypatch):
    import dataclasses

    from musehost.app import build_brain

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    off = dataclasses.replace(CONFIG, brain_provider="")
    assert build_brain(off, Hub(), store=None) is None


def test_the_app_installs_the_brain_as_the_chat_handler(state, monkeypatch):
    from musehost.app import create_app

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    app = create_app(state)
    assert isinstance(app.state.hub.chat_handler, Brain)
    assert app.state.hub.chat_handler.history is not None


@pytest.mark.parametrize(
    "settings, env, expected",
    [
        ({"brain_provider": "openai", "brain_model": "gpt-x"}, {"OPENAI_API_KEY": "k"}, "openai"),
        ({"brain_provider": "openai"}, {"OPENAI_API_KEY": "k"}, None),  # no model
        ({"brain_provider": "openai", "brain_model": "gpt-x"}, {}, None),  # no key
        (
            {
                "brain_provider": "vllm",
                "brain_model": "qwen",
                "brain_base_url": "http://gpu:8000/v1",
            },
            {},
            "vllm",
        ),
        ({"brain_provider": "vllm", "brain_model": "qwen"}, {}, None),  # no base URL
    ],
)
def test_openai_and_vllm_need_their_settings(monkeypatch, settings, env, expected):
    import dataclasses

    from musehost.app import build_brain

    for name, value in env.items():
        monkeypatch.setenv(name, value)
    brain = build_brain(dataclasses.replace(CONFIG, **settings), Hub(), store=None)
    assert (brain.provider.name if brain else None) == expected
