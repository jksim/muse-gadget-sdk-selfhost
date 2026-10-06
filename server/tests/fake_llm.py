"""Local fake LLM HTTP servers that speak the real streaming wire formats.

The real SDKs talk to these through ``base_url``, so adapter tests exercise the
SDKs' own request building and stream parsing without network or cost.
"""

from __future__ import annotations

import http.server
import json
import threading
from contextlib import contextmanager


class Reply:
    """One scripted HTTP response: SSE events, or an error status with a JSON body."""

    def __init__(self, events=None, status=200, body=None):
        self.events = events or []
        self.status = status
        self.body = body


@contextmanager
def fake_server(replies: list[Reply]):
    """Serve ``replies`` in order to POST requests; yields (base_url, requests)."""
    requests: list[dict] = []
    queue = list(replies)

    class Handler(http.server.BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_POST(self):
            length = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(length) or b"{}")
            requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
            reply = queue.pop(0) if queue else Reply(status=500, body={"error": "no reply"})
            if reply.status != 200:
                data = json.dumps(reply.body or {}).encode()
                self.send_response(reply.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            payload = "".join(
                (f"event: {name}\n" if name else "")
                + f"data: {data if isinstance(data, str) else json.dumps(data)}\n\n"
                for name, data in reply.events
            ).encode()
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}", requests
    finally:
        server.shutdown()
        server.server_close()


# -- Anthropic Messages SSE ---------------------------------------------------------------


def claude_message(blocks, stop_reason="end_turn", model="claude-opus-5-5", usage=None):
    """SSE events for one Messages response.

    ``blocks`` items: ("text", "full text", [chunks...]) | ("thinking", signature)
    | ("tool_use", id, name, [json chunks]) | ("raw", block_dict).
    """
    events = [
        (
            "message_start",
            {
                "type": "message_start",
                "message": {
                    "id": "msg_test",
                    "type": "message",
                    "role": "assistant",
                    "model": model,
                    "content": [],
                    "stop_reason": None,
                    "stop_sequence": None,
                    "usage": {"input_tokens": 120, "output_tokens": 1},
                },
            },
        )
    ]
    for index, block in enumerate(blocks):
        kind = block[0]
        if kind == "text":
            start = {"type": "text", "text": ""}
            deltas = [{"type": "text_delta", "text": t} for t in block[2]]
        elif kind == "thinking":
            start = {"type": "thinking", "thinking": "", "signature": ""}
            deltas = [{"type": "signature_delta", "signature": block[1]}]
        elif kind == "tool_use":
            start = {"type": "tool_use", "id": block[1], "name": block[2], "input": {}}
            deltas = [{"type": "input_json_delta", "partial_json": j} for j in block[3]]
        else:  # raw block, delivered whole
            start, deltas = block[1], []
        events.append(
            (
                "content_block_start",
                {"type": "content_block_start", "index": index, "content_block": start},
            )
        )
        for delta in deltas:
            events.append(
                (
                    "content_block_delta",
                    {"type": "content_block_delta", "index": index, "delta": delta},
                )
            )
        events.append(("content_block_stop", {"type": "content_block_stop", "index": index}))
    final_usage = {"output_tokens": 42, **(usage or {})}
    events.append(
        (
            "message_delta",
            {
                "type": "message_delta",
                "delta": {"stop_reason": stop_reason, "stop_sequence": None},
                "usage": final_usage,
            },
        )
    )
    events.append(("message_stop", {"type": "message_stop"}))
    return Reply(events)


def claude_error(status, kind, message="nope"):
    return Reply(status=status, body={"type": "error", "error": {"type": kind, "message": message}})


# -- OpenAI Chat Completions SSE (also what vLLM serves) ------------------------------------


def _chunk(delta=None, finish=None, usage=None, choices=True):
    body = {"id": "chatcmpl-test", "object": "chat.completion.chunk", "created": 1, "model": "m"}
    body["choices"] = (
        [{"index": 0, "delta": delta or {}, "finish_reason": finish}] if choices else []
    )
    if usage is not None:
        body["usage"] = usage
    return (None, body)


def chat_stream(text_chunks=(), tool_calls=(), finish="stop"):
    """SSE chunks for one streamed chat completion.

    ``tool_calls`` items: (index, id, name, [argument fragments]).
    """
    events = [_chunk({"role": "assistant", "content": ""})]
    events += [_chunk({"content": t}) for t in text_chunks]
    for index, call_id, name, fragments in tool_calls:
        events.append(
            _chunk(
                {
                    "tool_calls": [
                        {
                            "index": index,
                            "id": call_id,
                            "type": "function",
                            "function": {"name": name, "arguments": ""},
                        }
                    ]
                }
            )
        )
        events += [
            _chunk({"tool_calls": [{"index": index, "function": {"arguments": f}}]})
            for f in fragments
        ]
    events.append(_chunk({}, finish=finish))
    events.append(
        _chunk(
            usage={"prompt_tokens": 80, "completion_tokens": 12, "total_tokens": 92}, choices=False
        )
    )
    events.append((None, "[DONE]"))
    return Reply(events)


def openai_error(status, message="nope"):
    return Reply(status=status, body={"error": {"message": message, "type": "error", "code": None}})
