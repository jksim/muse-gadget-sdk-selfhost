"""Tools: the allowlist, name mapping, input validation, and the brain's tool loop."""

import asyncio
import json

import pytest
from test_brain import CONFIG, text_turn

from musehost.brain import MAX_TOOL_CALLS, Brain
from musehost.brain.providers.base import Done, Text, ToolCall
from musehost.brain.tools import build_toolset, validate
from musehost.hub import DeviceOffline, Hub, InvokeResult, InvokeTimeout

# What the CoreS3 actually advertises (esp32/main/noise_control.cpp).
CORES3_COMMANDS = {
    "device.health": {
        "description": "Report basic Link health, including battery level (percent)...",
        "required": {},
        "optional": {},
    },
    "display.draw_url": {
        "description": "Fetch an image and draw it on the screen.",
        "required": {
            "url": {"type": "string", "description": "http:// or https:// URL of the image."}
        },
        "optional": {
            "row": {"type": "integer", "description": "Row to draw the top of the image at."}
        },
        "timeout_ms": 60000,
    },
    "display.show_animation": {
        "description": "Clear the image and bring back the animation.",
        "required": {},
        "optional": {},
    },
    "device.ota": {
        "description": "Download a firmware image from `url` and apply it, then reboot.",
        "required": {"url": {"type": "string", "description": "Firmware URL."}},
        "optional": {"force": {"type": "boolean", "description": "Install even if older."}},
    },
}


# -- Building tools -------------------------------------------------------------------


def test_only_allowlisted_commands_become_tools():
    toolset = build_toolset(CORES3_COMMANDS, CONFIG.brain_tools)
    names = sorted(spec.name for spec in toolset.specs)
    assert names == ["device_health", "display_draw_url", "display_show_animation"]
    assert "device.ota" not in json.dumps([s.__dict__ for s in toolset.specs])


def test_tool_names_map_back_to_commands():
    toolset = build_toolset(CORES3_COMMANDS, CONFIG.brain_tools)
    assert toolset.command("display_draw_url").name == "display.draw_url"
    assert toolset.command("device_ota") is None
    assert toolset.command("device.health") is None  # only the mapped name is accepted


def test_a_tool_schema_comes_from_required_and_optional():
    toolset = build_toolset(CORES3_COMMANDS, CONFIG.brain_tools)
    spec = next(s for s in toolset.specs if s.name == "display_draw_url")
    assert spec.input_schema == {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "http:// or https:// URL of the image."},
            "row": {"type": "integer", "description": "Row to draw the top of the image at."},
        },
        "required": ["url"],
        "additionalProperties": False,
    }
    assert spec.description.startswith("Fetch an image")
    assert toolset.command("display_draw_url").timeout_s == 60.0


def test_a_device_without_commands_has_no_tools():
    assert build_toolset({}, CONFIG.brain_tools).specs == []


@pytest.mark.parametrize(
    "value, ok",
    [
        ({"url": "https://example.com/cat.jpg"}, True),
        ({"url": "https://example.com/cat.jpg", "row": 10}, True),
        ({}, False),  # missing required
        ({"url": 7}, False),  # wrong type
        ({"url": "https://x", "row": True}, False),  # bool is not an integer
        ({"url": "https://x", "extra": 1}, False),  # unknown field
        ("not an object", False),
    ],
)
def test_inputs_are_validated_against_the_schema(value, ok):
    schema = build_toolset(CORES3_COMMANDS, CONFIG.brain_tools).command("display_draw_url").schema
    assert (validate(value, schema) is None) == ok


# -- The loop ---------------------------------------------------------------------------


class ToolCallingProvider:
    """Asks for the scripted tool calls, then says what it got back."""

    name, model = "fake", "fake-1"

    def __init__(self, *calls):
        self.calls = list(calls)
        self.tools_offered = None
        self.results = []

    async def stream_turn(self, *, system, history, user_text, tools, run_tool, max_rounds):
        self.tools_offered = tools
        for call in self.calls:
            self.results.append(await run_tool(call))
        yield Text("Done.")
        yield Done("end_turn", {}, [])


def hub_with_cores3(invoke=None):
    hub = Hub()
    hub.register(
        "homelink-4d6734",
        {"display_name": "CoreS3", "platform": "esp32", "commands_v2": CORES3_COMMANDS},
        object(),
    )
    hub.invoked = []

    async def fake_invoke(node_id, command, params=None, timeout_s=30):
        hub.invoked.append((node_id, command, params, timeout_s))
        if invoke:
            return await invoke(command, params)
        return InvokeResult(ok=True, payload={"battery_pct": 82})

    hub.invoke = fake_invoke
    published = []

    async def publish(node, event, payload):
        published.append((event, payload))

    hub.publish = publish
    hub.published = published
    return hub


def run(brain):
    async def scenario():
        return "".join([p async for p in brain(text_turn("How's your battery?"))])

    return asyncio.run(scenario())


def test_an_allowlisted_tool_is_invoked_and_its_payload_returned():
    hub = hub_with_cores3()
    provider = ToolCallingProvider(ToolCall("t1", "device_health", {}))
    run(Brain(hub, provider, CONFIG))
    assert hub.invoked == [("homelink-4d6734", "device.health", {}, 30.0)]
    [result] = provider.results
    assert not result.is_error and json.loads(result.content)["payload"] == {"battery_pct": 82}
    assert ("agent.status", {"activity_code": "working"}) in hub.published
    assert {s.name for s in provider.tools_offered} == {
        "device_health",
        "display_draw_url",
        "display_show_animation",
    }


@pytest.mark.parametrize("name", ["device_ota", "device.ota", "system_run", "nope"])
def test_ota_and_unknown_tools_never_reach_the_device(name):
    hub = hub_with_cores3()
    provider = ToolCallingProvider(ToolCall("t1", name, {"url": "https://evil/fw.bin"}))
    run(Brain(hub, provider, CONFIG))
    assert hub.invoked == []
    assert provider.results[0].is_error


def test_bad_input_is_an_error_without_invoking():
    hub = hub_with_cores3()
    provider = ToolCallingProvider(ToolCall("t1", "display_draw_url", {"row": "top"}))
    run(Brain(hub, provider, CONFIG))
    assert hub.invoked == [] and provider.results[0].is_error


@pytest.mark.parametrize("error", [DeviceOffline("x"), InvokeTimeout("x")])
def test_offline_and_timeouts_come_back_as_errors(error):
    async def fail(command, params):
        raise error

    hub = hub_with_cores3(invoke=fail)
    provider = ToolCallingProvider(ToolCall("t1", "device_health", {}))
    run(Brain(hub, provider, CONFIG))
    assert provider.results[0].is_error


def test_a_failed_command_is_reported_to_the_model_as_an_error():
    async def failed(command, params):
        return InvokeResult(ok=False, error="no image")

    hub = hub_with_cores3(invoke=failed)
    provider = ToolCallingProvider(ToolCall("t1", "display_draw_url", {"url": "https://x/y.png"}))
    run(Brain(hub, provider, CONFIG))
    assert provider.results[0].is_error and "no image" in provider.results[0].content


def test_too_many_tool_calls_in_one_turn_are_refused():
    hub = hub_with_cores3()
    calls = [ToolCall(f"t{i}", "device_health", {}) for i in range(MAX_TOOL_CALLS + 2)]
    provider = ToolCallingProvider(*calls)
    run(Brain(hub, provider, CONFIG))
    assert len(hub.invoked) == MAX_TOOL_CALLS
    assert all(r.is_error for r in provider.results[MAX_TOOL_CALLS:])


def test_a_turn_from_an_unknown_device_has_no_tools():
    provider = ToolCallingProvider()
    run(Brain(Hub(), provider, CONFIG))
    assert provider.tools_offered == []


def test_tool_payloads_are_not_logged(caplog):
    import logging

    caplog.set_level(logging.DEBUG)

    async def secret(command, params):
        return InvokeResult(ok=True, payload={"wifi_password": "hunter2"})

    hub = hub_with_cores3(invoke=secret)
    provider = ToolCallingProvider(ToolCall("t1", "device_health", {}))
    run(Brain(hub, provider, CONFIG))
    assert "device.health" in caplog.text and "hunter2" not in caplog.text
