"""``link.error_report``: one crash report from a gadget (protocols/ERROR_REPORT.md).

``validate`` returns only the contract's fields, flattened for the
``crash_reports`` table, or None when the report breaks the contract. Logs and
the dashboard see nothing else from the message.
"""

from __future__ import annotations

import json
import re

MAX_MESSAGE_BYTES = 512
REASONS = frozenset(
    {
        "panic",
        "interrupt_watchdog",
        "task_watchdog",
        "watchdog",
        "brownout",
        "power_glitch",
        "cpu_lockup",
        "efuse",
        "unhandled_exception",
        "unclean_exit",
    }
)
_ADDRESS = re.compile(r"0x[0-9a-fA-F]{8}")
_BUILD_SHA = re.compile(r"[0-9a-f]{8,64}")


class _Invalid(Exception):
    pass


def _int(value, minimum: int) -> int:
    if type(value) is not int or value < minimum:
        raise _Invalid
    return value


def _text(value, longest: int) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= longest:
        raise _Invalid
    if any(not " " <= ch <= "~" for ch in value):
        raise _Invalid
    return value


def _match(value, pattern: re.Pattern) -> str:
    if not isinstance(value, str) or not pattern.fullmatch(value):
        raise _Invalid
    return value


def _optional(source: dict, key: str, check, *args):
    return check(source[key], *args) if key in source else None


def validate(message: dict) -> dict | None:
    try:
        size = len(json.dumps(message, separators=(",", ":")).encode())
        report = message.get("report")
        if size >= MAX_MESSAGE_BYTES or not isinstance(report, dict):
            raise _Invalid
        if type(report.get("schema_version")) is not int or report["schema_version"] != 1:
            raise _Invalid
        if report.get("kind") != "crash" or report.get("reason") not in REASONS:
            raise _Invalid
        fault = report.get("fault", {})
        if not isinstance(fault, dict):
            raise _Invalid
        return {
            "reason": report["reason"],
            "consecutive_count": _int(report.get("consecutive_count"), 1),
            "version": _optional(report, "version", _text, 31),
            "build_sha": _optional(report, "build_sha", _match, _BUILD_SHA),
            "uptime_seconds": _optional(report, "uptime_seconds", _int, 0),
            "fault_address": _optional(fault, "address", _match, _ADDRESS),
            "fault_return_address": _optional(fault, "return_address", _match, _ADDRESS),
            "fault_cause": _optional(fault, "cause", _text, 47),
            "fault_detail": _optional(fault, "detail", _text, 95),
        }
    except (_Invalid, TypeError, ValueError):
        return None
