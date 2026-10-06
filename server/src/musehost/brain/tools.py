"""The device commands Clio may use, as tools a language model can call.

A gadget advertises ``commands_v2``: per command a description plus
``required`` and ``optional`` maps of JSON-schema properties. Only commands in
``host.toml``'s ``brain_tools`` allowlist become tools, so firmware updates and
the like are never offered. Command names (``display.draw_url``) aren't valid
tool names, so each is mapped to one (``display_draw_url``) and back; a call
for any name outside this turn's mapping is refused before reaching a device.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from musehost.brain.providers.base import ToolSpec

DEFAULT_TIMEOUT_S = 30.0
_UNSAFE = re.compile(r"[^A-Za-z0-9_-]")


@dataclass(frozen=True)
class Command:
    name: str
    schema: dict
    timeout_s: float


@dataclass
class Toolset:
    specs: list[ToolSpec] = field(default_factory=list)
    _commands: dict[str, Command] = field(default_factory=dict)

    def command(self, tool_name: str) -> Command | None:
        return self._commands.get(tool_name)


def tool_name(command: str) -> str:
    return _UNSAFE.sub("_", command)[:64]


def build_toolset(commands_v2: dict, allowlist) -> Toolset:
    toolset = Toolset()
    for command in sorted(commands_v2):  # stable order keeps the prompt cacheable
        if command not in allowlist:
            continue
        spec = commands_v2[command] if isinstance(commands_v2[command], dict) else {}
        required = spec.get("required") if isinstance(spec.get("required"), dict) else {}
        optional = spec.get("optional") if isinstance(spec.get("optional"), dict) else {}
        schema = {
            "type": "object",
            "properties": {**required, **optional},
            "required": list(required),
            "additionalProperties": False,
        }
        name = tool_name(command)
        if name in toolset._commands:
            continue  # two commands collapsing to one name: keep the first
        timeout_ms = spec.get("timeout_ms")
        timeout_s = timeout_ms / 1000 if isinstance(timeout_ms, (int, float)) else DEFAULT_TIMEOUT_S
        toolset._commands[name] = Command(command, schema, float(timeout_s))
        toolset.specs.append(ToolSpec(name, str(spec.get("description") or command), schema))
    return toolset


_TYPES = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "object": dict,
    "array": list,
}


def validate(value, schema: dict) -> str | None:
    """Why ``value`` doesn't fit ``schema`` (a flat object schema), or None if it does."""
    if not isinstance(value, dict):
        return "input must be an object"
    properties = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in value:
            return f"missing required field {key!r}"
    for key, item in value.items():
        if key not in properties:
            return f"unknown field {key!r}"
        expected = properties[key].get("type") if isinstance(properties[key], dict) else None
        python_type = _TYPES.get(expected)
        if python_type is None:
            continue
        if isinstance(item, bool) and expected in ("integer", "number"):
            return f"field {key!r} must be {expected}"
        if not isinstance(item, python_type):
            return f"field {key!r} must be {expected}"
    return None
