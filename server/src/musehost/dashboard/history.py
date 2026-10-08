"""Past conversations, read straight from the brain's history tables.

Messages are summarised for reading: text as text, tool use as one line
("used device_health: ok"), provider-internal blocks (thinking, redacted or
encrypted content) left out. Nothing here is logged.
"""

from __future__ import annotations

import json
import re
import time
from pathlib import Path

from starlette.requests import Request
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from musehost.dashboard.web import page

PAGE_SIZE = 25
CONTEXT = re.compile(r"^\[Context: local time (?P<when>.+?); speaking through (?P<via>.+?)\.\]\n")


def _when(epoch) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(epoch)) if epoch else "-"


def _failed(content) -> bool:
    if isinstance(content, list):
        content = " ".join(b.get("text", "") for b in content if isinstance(b, dict))
    try:
        data = json.loads(content) if isinstance(content, str) else None
    except ValueError:
        return False
    return isinstance(data, dict) and (data.get("ok") is False or "error" in data)


def readable(messages: list[dict]) -> list[dict]:
    """Stored messages (Claude or OpenAI shaped) as turns to show."""
    names: dict[str, str] = {}
    turns: list[dict] = []

    def add(role: str, part: tuple[str, str], when: str = "", via: str = "") -> None:
        if turns and turns[-1]["role"] == role and not when:
            turns[-1]["parts"].append(part)
        else:
            turns.append({"role": role, "when": when, "via": via, "parts": [part]})

    def text_of(role: str, text: str) -> None:
        when = via = ""
        if role == "user":
            match = CONTEXT.match(text)
            if match:
                when, via, text = match["when"], match["via"], text[match.end() :]
        if text.strip():
            add(role, ("text", text), when, via)

    for message in messages:
        role = message.get("role", "")
        content = message.get("content")
        if role == "system":
            continue
        for call in message.get("tool_calls") or []:  # OpenAI-style assistant tool calls
            names[call.get("id", "")] = (call.get("function") or {}).get("name", "a tool")
        if role == "tool":  # OpenAI-style tool result
            name = names.get(message.get("tool_call_id", ""), "a tool")
            add("assistant", ("tool", f"used {name}: {'failed' if _failed(content) else 'ok'}"))
            continue
        if isinstance(content, str):
            text_of(role, content)
            continue
        for block in content or []:
            if not isinstance(block, dict):
                continue
            kind = block.get("type")
            if kind == "text":
                text_of(role, block.get("text", ""))
            elif kind in ("tool_use", "server_tool_use"):
                names[block.get("id", "")] = block.get("name", "a tool")
            elif kind == "tool_result":
                name = names.get(block.get("tool_use_id", ""), "a tool")
                failed = block.get("is_error") or _failed(block.get("content"))
                add("assistant", ("tool", f"used {name}: {'failed' if failed else 'ok'}"))
            elif kind == "web_search_tool_result":
                add("assistant", ("tool", "used web_search: ok"))
            # thinking, redacted_thinking and anything unknown: not shown
    return turns


def routes(state: Path, parent) -> list[Route]:
    def db():
        return parent.state.tokens.store.db

    async def history_list(request: Request):
        raw = request.query_params.get("page", "1")
        number = int(raw) if raw.isdigit() and int(raw) > 0 else 1
        rows = (
            db()
            .execute(
                "SELECT c.id, c.node_id, c.provider, c.model, c.started_at, c.last_at,"
                " c.message_count, d.display_name FROM conversations c"
                " LEFT JOIN devices d ON d.node_id = c.node_id"
                " ORDER BY c.last_at DESC, c.id DESC LIMIT ? OFFSET ?",
                (PAGE_SIZE + 1, (number - 1) * PAGE_SIZE),
            )
            .fetchall()
        )
        items = [
            {
                "id": r["id"],
                "who": "You (operator)"
                if r["node_id"] == "operator"
                else (r["display_name"] or r["node_id"]),
                "provider": r["provider"],
                "model": r["model"],
                "started": _when(r["started_at"]),
                "last": _when(r["last_at"]),
                "count": r["message_count"],
            }
            for r in rows[:PAGE_SIZE]
        ]
        return page(
            request,
            "history.html",
            {
                "title": "History",
                "items": items,
                "page": number,
                "more": len(rows) > PAGE_SIZE,
            },
        )

    async def conversation(request: Request):
        raw = request.path_params["conversation_id"]
        row = (
            db()
            .execute(
                "SELECT c.*, d.display_name FROM conversations c"
                " LEFT JOIN devices d ON d.node_id = c.node_id WHERE c.id = ?",
                (int(raw),),
            )
            .fetchone()
            if raw.isdigit()
            else None
        )
        if row is None:
            return PlainTextResponse("No such conversation", status_code=404)
        messages = [
            json.loads(r["content_json"])
            for r in db().execute(
                "SELECT content_json FROM conversation_messages WHERE conversation_id = ?"
                " ORDER BY seq",
                (row["id"],),
            )
        ]
        who = (
            "You (operator)"
            if row["node_id"] == "operator"
            else (row["display_name"] or row["node_id"])
        )
        return page(
            request,
            "conversation.html",
            {
                "title": f"Conversation with {who}",
                "who": who,
                "row": row,
                "started": _when(row["started_at"]),
                "last": _when(row["last_at"]),
                "turns": readable(messages),
            },
        )

    return [
        Route("/history", history_list),
        Route("/history/{conversation_id}", conversation),
    ]
