"""Chat over Noise: what a gadget says to Clio and how her reply streams back.

A gadget keeps ``POST /chat/subscribe`` open and reads newline-delimited JSON
events from it. Each message goes up as ``POST /chat/stream``; the host acks it
with a ``message_id`` and then streams the reply on every subscription of that
node, as the firmware expects (``muse_chat_session.cpp``):

    agent.status thinking -> delta.message_start -> delta.text_append ...
    -> delta.message_done -> agent.status idle

``message_start`` names the user message it answers in
``reply_to_message_id``; the firmware ignores replies to other messages.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import dataclasses
import json
import logging
import os
import struct
import tempfile
import time
import uuid
from pathlib import Path

from musehost.hub import ChatTurn, Hub
from musehost.noise_server import Stream
from musehost.voice_out import SynthesisFailed

log = logging.getLogger(__name__)

# A 20 s voice note is ~850 KB of base64; anything far beyond is refused.
MAX_CHAT_BODY = 2 * 1024 * 1024
KEEP_VOICE_NOTES = 100


NOT_CONNECTED = "My brain isn't connected yet."


async def placeholder(turn: ChatTurn):
    """Clio's answer until ``brain`` is connected."""
    status = turn.speech_status
    if turn.audio_path is not None and status != "ok":
        if status == "empty":
            yield "I didn't catch anything in that voice note."
        elif status == "loading":
            yield "I'm still waking up my ears; try again in a moment."
        elif status == "failed":
            yield "Sorry, I couldn't make out that voice note."
        else:  # speech off
            yield f"Got your {turn.duration_s or 0:.1f} s voice note. "
            yield NOT_CONNECTED
        return
    text = (turn.text or "").strip() or "(nothing)"
    yield "Clio heard: "
    yield text
    yield (" " if text[-1] in ".!?" else ". ") + NOT_CONNECTED


async def chat_subscribe(stream: Stream) -> None:
    hub: Hub = stream.session.ws.app.state.hub
    node_id = stream.session.node_id
    await stream.respond(
        200, b'{"type":"subscribed"}\n', end=False, headers={"content-type": "application/x-ndjson"}
    )
    hub.subscribe(node_id, stream)
    try:
        await stream.gone.wait()
    finally:
        hub.unsubscribe(node_id, stream)


async def chat_stream(stream: Stream) -> None:
    state = stream.session.ws.app.state
    hub: Hub = state.hub
    node_id = stream.session.node_id
    # Voice notes stream in while the owner talks; spill big bodies to disk.
    with tempfile.SpooledTemporaryFile(max_size=256 * 1024) as spool:
        size = 0
        async for data in stream.chunks():
            size += len(data)
            if size > MAX_CHAT_BODY:
                log.warning("chat body from %s over %d bytes; refused", node_id, MAX_CHAT_BODY)
                await stream.respond_json(413, {"ok": False, "error": "message too large"})
                return
            spool.write(data)
        spool.seek(0)
        try:
            body = json.load(spool)
        except (ValueError, UnicodeDecodeError):
            body = None
    if not isinstance(body, dict):
        await stream.respond_json(400, {"ok": False, "error": "invalid chat body"})
        return

    audio_path = duration = None
    for item in body.get("items") or []:
        if (
            isinstance(item, dict)
            and item.get("type") == "file"
            and (str(item.get("mime_type", "")).startswith("audio/"))
        ):
            try:
                wav = base64.b64decode(item.get("data_base64") or "", validate=True)
                audio_path, duration = save_voice_note(state.voice_dir, node_id, wav)
            except (ValueError, binascii.Error) as exc:
                await stream.respond_json(400, {"ok": False, "error": f"bad voice note: {exc}"})
                return
            log.info("voice note from %s: %.1f s", node_id, duration)
            break

    message_id = f"u-{uuid.uuid4()}"
    session_id = body.get("session_id") if isinstance(body.get("session_id"), str) else None
    text = body.get("message") if isinstance(body.get("message"), str) else None
    turn = ChatTurn(
        node_id=node_id,
        message_id=message_id,
        text=text or None,
        audio_path=audio_path,
        duration_s=duration,
        session_id=session_id,
    )
    await stream.respond_json(200, {"result": {"message_id": message_id}})
    log.info("chat turn %s from %s", message_id, node_id)
    task = asyncio.ensure_future(reply(hub, turn, getattr(state, "transcriber", None)))
    _turns.add(task)
    task.add_done_callback(_turns.discard)


_turns: set[asyncio.Task] = set()

MAX_TTS_TEXT = 2000


async def tts(stream: Stream) -> None:
    """``POST /tts``: speak ``{"text"}`` as MP3, streamed a sentence at a time."""
    synthesizer = getattr(stream.session.ws.app.state, "synthesizer", None)
    if synthesizer is None or synthesizer.state == "off":
        await stream.respond(404)
        return
    try:
        body = json.loads(await stream.read_body(16 * 1024))
    except (ValueError, UnicodeDecodeError):
        body = None
    text = body.get("text") if isinstance(body, dict) else None
    if not isinstance(text, str) or not text.strip() or len(text) > MAX_TTS_TEXT:
        await stream.respond_json(400, {"ok": False, "error": 'expected {"text": ...}'})
        return
    if synthesizer.state != "ready":
        await stream.respond(503)
        return
    await stream.respond(200, end=False, headers={"content-type": "audio/mpeg"})
    try:
        async for chunk in synthesizer.synthesize_mp3(text.strip()):
            await stream.send(chunk)
    except SynthesisFailed:
        await stream.reset("speech synthesis failed")
        return
    await stream.send(b"", end=True)


async def _with_transcript(turn: ChatTurn, transcriber) -> ChatTurn:
    """``turn`` with the voice note's transcript as its text (saved beside the WAV)."""
    if transcriber is None:
        return dataclasses.replace(turn, speech_status="off")
    result = await transcriber.transcribe(turn.audio_path)
    transcribed = result.status in ("ok", "empty")
    if transcribed:
        path = turn.audio_path.with_suffix(".txt")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(result.text)
    return dataclasses.replace(
        turn,
        text=result.text if transcribed else None,
        transcribed=transcribed,
        speech_status=result.status,
    )


def save_voice_note(root: Path, node_id: str, wav: bytes, keep: int = KEEP_VOICE_NOTES):
    """Store a WAV with its sizes filled in; returns ``(path, seconds)``.

    The firmware streams the note before it knows its length, so the RIFF and
    data sizes arrive as 0xFFFFFFFF. Only the newest ``keep`` notes per node
    are kept.
    """
    rate, channels, width, data_at = _parse_wav(wav)
    fixed = bytearray(wav)
    struct.pack_into("<I", fixed, 4, len(wav) - 8)
    struct.pack_into("<I", fixed, data_at - 4, len(wav) - data_at)
    seconds = round((len(wav) - data_at) / (rate * channels * width), 2)

    folder = root / node_id
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(folder, 0o700)
    os.chmod(root, 0o700)
    ns = time.time_ns()
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime(ns // 1_000_000_000))
    path = folder / f"{stamp}.{ns % 1_000_000_000:09d}Z.wav"
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(fixed)
    for old in sorted(folder.glob("*.wav"))[:-keep]:
        old.unlink(missing_ok=True)
        old.with_suffix(".txt").unlink(missing_ok=True)
    return path, seconds


def _parse_wav(wav: bytes) -> tuple[int, int, int, int]:
    """``(rate, channels, bytes per sample, offset of the samples)`` of a PCM WAV."""
    if len(wav) < 44 or wav[:4] != b"RIFF" or wav[8:12] != b"WAVE":
        raise ValueError("not a RIFF/WAVE file")
    offset, fmt = 12, None
    while offset + 8 <= len(wav):
        chunk, size = wav[offset : offset + 4], struct.unpack_from("<I", wav, offset + 4)[0]
        if chunk == b"fmt ":
            fmt = struct.unpack_from("<HHIIHH", wav, offset + 8)
        elif chunk == b"data":
            if fmt is None or fmt[0] != 1 or not fmt[1] or not fmt[2] or fmt[5] % 8:
                raise ValueError("not 8/16/24/32-bit PCM")
            return fmt[2], fmt[1], fmt[5] // 8, offset + 8
        offset += 8 + size + (size & 1)
    raise ValueError("no data chunk")


OPERATOR = "operator"  # the conversation `musehost chat` and the dashboard share


async def operator_turn(hub: Hub, message: str, device: str | None = None, new: bool = False):
    """One operator turn, as pieces of Clio's reply.

    ``device`` lends that gadget's tools; the turn stays in the operator's
    conversation either way. ``new`` starts a fresh conversation first.
    """
    handler = hub.chat_handler or placeholder
    history = getattr(handler, "history", None)
    if new and history is not None:
        history.start_new(OPERATOR)
    turn = ChatTurn(
        node_id=OPERATOR,
        message_id=f"op-{uuid.uuid4()}",
        text=message,
        tool_node_id=device or None,
    )
    async for piece in handler(turn):
        yield piece


async def reply(hub: Hub, turn: ChatTurn, transcriber=None) -> None:
    """Transcribe a voice note if there is one, run the chat handler, stream its reply."""
    handler = hub.chat_handler or placeholder
    reply_id = f"a-{uuid.uuid4()}"
    node = turn.node_id
    await hub.publish(node, "agent.status", {"activity_code": "thinking"})
    if turn.audio_path is not None:
        turn = await _with_transcript(turn, transcriber)
    await hub.publish(
        node,
        "delta.message_start",
        {"message_id": reply_id, "reply_to_message_id": turn.message_id},
    )
    text = ""
    try:
        async for piece in handler(turn):
            if piece:
                text += piece
                await hub.publish(
                    node, "delta.text_append", {"message_id": reply_id, "text": piece}
                )
    except Exception:
        log.exception("chat handler failed for %s", turn.message_id)
        piece = " (Something went wrong on my side.)"
        text += piece
        await hub.publish(node, "delta.text_append", {"message_id": reply_id, "text": piece})
    await hub.publish(node, "delta.message_done", {"message_id": reply_id, "display_text": text})
    await hub.publish(node, "agent.status", {"activity_code": "idle"})
