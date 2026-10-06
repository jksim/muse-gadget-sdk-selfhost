"""Chat over Noise: /chat/subscribe events and /chat/stream turns."""

import asyncio
import json

from noise_client import NoiseClient
from test_link import link_session, wait_for

NDJSON = {"content-type": "application/json", "accept": "application/x-ndjson"}
TEXT_TURN = {"message": "hello there", "output_modality": "text"}


async def subscribe(client: NoiseClient) -> int:
    sid = await client.request("POST", "/chat/subscribe", b"{}", headers=NDJSON)
    [first] = await client.read_lines(sid, 1)
    assert first == {"type": "subscribed"}
    return sid


async def post(client: NoiseClient, body: dict, parts: int = 1) -> dict:
    data = json.dumps(body).encode()
    if parts == 1:
        sid = await client.request("POST", "/chat/stream", data)
    else:
        sid = await client.request("POST", "/chat/stream", end_body=False)
        size = -(-len(data) // parts)
        for i in range(parts):
            piece = data[i * size : (i + 1) * size]
            await client.send_chunk(sid, piece, end_body=i == parts - 1)
    status, ack = await client.response(sid)
    assert status == 200
    return json.loads(ack)


async def turn_events(client: NoiseClient, sub: int) -> list[dict]:
    """Events up to and including the closing agent.status idle."""
    events = []
    while True:
        [line] = await client.read_lines(sub, 1)
        events.append(line)
        if line["event"] == "agent.status" and line["payload"]["activity_code"] == "idle":
            return events


def chat(live, scenario):
    async def run():
        client = await NoiseClient.connect(live.port, live.ca, live.vm_token())
        try:
            return await scenario(client)
        finally:
            await client.close()

    return asyncio.run(run())


def test_a_text_turn_streams_clios_reply_in_the_order_the_firmware_expects(live):
    async def scenario(client):
        sub = await subscribe(client)
        ack = await post(client, TEXT_TURN)
        return ack, await turn_events(client, sub)

    ack, events = chat(live, scenario)
    user_id = ack["result"]["message_id"]
    names = [e["event"] for e in events]
    assert names[0] == "agent.status" and names[1] == "delta.message_start"
    assert names[-2] == "delta.message_done" and names[-1] == "agent.status"
    assert set(names[2:-2]) == {"delta.text_append"} and len(names[2:-2]) >= 2
    assert all(e["type"] == "event" for e in events)
    seqs = [e["seq"] for e in events]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    start, done = events[1]["payload"], events[-2]["payload"]
    assert start["reply_to_message_id"] == user_id
    assert {e["payload"]["message_id"] for e in events[1:-1]} == {start["message_id"]}
    text = "".join(e["payload"]["text"] for e in events[2:-2])
    assert done["display_text"] == text
    assert "hello there" in text and "brain isn't connected" in text


def test_a_message_split_over_several_chunks_is_reassembled(live):
    async def scenario(client):
        sub = await subscribe(client)
        await post(client, {**TEXT_TURN, "message": "x" * 3000}, parts=7)
        return await turn_events(client, sub)

    events = chat(live, scenario)
    assert "x" * 3000 in events[-2]["payload"]["display_text"]


def test_a_custom_chat_handler_replaces_the_placeholder(live):
    async def shout(turn):
        yield "HI "
        yield turn.text.upper()

    live.app.state.loop.call_soon_threadsafe(live.app.state.hub.set_chat_handler, shout)

    async def scenario(client):
        sub = await subscribe(client)
        await post(client, TEXT_TURN)
        return await turn_events(client, sub)

    events = chat(live, scenario)
    assert events[-2]["payload"]["display_text"] == "HI HELLO THERE"


def test_the_linux_clients_send_chat_gets_acknowledged(live):
    async def scenario():
        session = link_session(live)
        stop = asyncio.Event()
        task = asyncio.ensure_future(session.run(stop))
        await wait_for(lambda: session.registered_at is not None)
        reply = await session.send_chat("hi from the pi")
        stop.set()
        await asyncio.wait_for(task, 5)
        return reply

    reply = asyncio.run(scenario())
    assert reply["ok"] and reply["response"]["result"]["message_id"]


def test_replies_go_to_every_subscription_of_the_node(live):
    async def scenario():
        token = live.vm_token()
        first = await NoiseClient.connect(live.port, live.ca, token)
        second = await NoiseClient.connect(live.port, live.ca, token)
        try:
            sub1, sub2 = await subscribe(first), await subscribe(second)
            await post(first, TEXT_TURN)
            return await turn_events(first, sub1), await turn_events(second, sub2)
        finally:
            await first.close()
            await second.close()

    events1, events2 = asyncio.run(scenario())
    assert events1 == events2


# -- Voice notes ---------------------------------------------------------------------

import base64  # noqa: E402
import math  # noqa: E402
import stat  # noqa: E402
import struct  # noqa: E402
import wave  # noqa: E402

from musehost import chat as chat_module  # noqa: E402

NOTE_HEAD = (
    b'{"message":"","output_modality":"text","items":[{"type":"file",'
    b'"mime_type":"audio/wav","filename":"voice_note.wav","data_base64":"'
)
NOTE_TAIL = b'"}]}'


def firmware_wav(seconds: float, rate: int = 16000) -> bytes:
    """A note as muse_chat_text.c builds it: sizes left at 0xFFFFFFFF ("unknown")."""
    header = (
        b"RIFF"
        + struct.pack("<I", 0xFFFFFFFF)
        + b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
        + b"data"
        + struct.pack("<I", 0xFFFFFFFF)
    )
    samples = int(seconds * rate)
    pcm = b"".join(struct.pack("<h", int(8000 * math.sin(i / 20))) for i in range(samples))
    return header + pcm


def note_body(wav: bytes) -> bytes:
    return NOTE_HEAD + base64.b64encode(wav) + NOTE_TAIL


async def post_raw(client, body: bytes, parts: int):
    sid = await client.request("POST", "/chat/stream", end_body=False)
    size = -(-len(body) // parts)
    for i in range(parts):
        await client.send_chunk(sid, body[i * size : (i + 1) * size], end_body=i == parts - 1)
    return await client.response(sid)


def use_transcriber(live, engine, timeout_s=30.0):
    """Swap the live server's transcriber for one around ``engine`` (None = off)."""
    from musehost.speech import Transcriber

    transcriber = Transcriber(engine, timeout_s=timeout_s)
    transcriber.start()
    if engine is not None:
        assert transcriber._loaded.wait(5)
    live.app.state.transcriber = transcriber
    return transcriber


def test_a_streamed_voice_note_is_saved_and_answered(live, state):
    use_transcriber(live, None)  # speech off: the pre-speech reply

    async def scenario(client):
        sub = await subscribe(client)
        status, ack = await post_raw(client, note_body(firmware_wav(3.0)), parts=25)
        assert status == 200
        return await turn_events(client, sub)

    events = chat(live, scenario)
    assert "Got your 3.0 s voice note" in events[-2]["payload"]["display_text"]
    folder = state / "voice" / "homelink-abcdef"
    [saved] = list(folder.glob("*.wav"))
    with wave.open(str(saved)) as w:
        assert (w.getframerate(), w.getnchannels(), w.getsampwidth()) == (16000, 1, 2)
        assert w.getnframes() == 48000
    assert stat.S_IMODE(saved.stat().st_mode) == 0o600
    assert stat.S_IMODE(folder.stat().st_mode) == 0o700


def test_an_oversized_chat_body_is_refused_and_nothing_is_saved(live, state):
    async def scenario(client):
        return await post_raw(client, note_body(firmware_wav(70.0)), parts=40)  # > 2 MB

    status, _ = chat(live, scenario)
    assert status == 413
    assert not list((state / "voice").rglob("*.wav")) if (state / "voice").exists() else True


def test_saving_fixes_the_unknown_sizes_and_reports_the_duration(tmp_path):
    path, seconds = chat_module.save_voice_note(tmp_path, "homelink-abcdef", firmware_wav(1.5))
    assert seconds == 1.5
    data = path.read_bytes()
    assert struct.unpack_from("<I", data, 4)[0] == len(data) - 8
    assert struct.unpack_from("<I", data, 40)[0] == len(data) - 44


def test_only_the_newest_hundred_notes_are_kept(tmp_path):
    wav = firmware_wav(0.01)
    paths = [chat_module.save_voice_note(tmp_path, "homelink-abcdef", wav)[0] for _ in range(101)]
    kept = sorted((tmp_path / "homelink-abcdef").glob("*.wav"))
    assert len(kept) == 100 and paths[0] not in kept and paths[-1] in kept


def test_something_that_isnt_a_wav_is_rejected(tmp_path):
    import pytest

    with pytest.raises(ValueError):
        chat_module.save_voice_note(tmp_path, "homelink-abcdef", b"not audio at all")


# -- Speech --------------------------------------------------------------------------

from test_speech import FakeEngine  # noqa: E402


def voice_turn(live, seconds=1.0, parts=5):
    async def scenario(client):
        sub = await subscribe(client)
        status, ack = await post_raw(client, note_body(firmware_wav(seconds)), parts=parts)
        assert status == 200
        return await turn_events(client, sub)

    return chat(live, scenario)


def reply_text(events) -> str:
    return events[-2]["payload"]["display_text"]


def test_a_transcribed_note_is_answered_with_what_clio_heard(live, state):
    use_transcriber(live, FakeEngine("What's the weather like today?"))
    seen = []

    async def handler(turn):
        seen.append(turn)
        async for piece in chat_module.placeholder(turn):
            yield piece

    live.app.state.loop.call_soon_threadsafe(live.app.state.hub.set_chat_handler, handler)
    events = voice_turn(live)
    assert reply_text(events).startswith("Clio heard: What's the weather like today?")
    [turn] = seen
    assert (turn.text, turn.transcribed, turn.speech_status) == (
        "What's the weather like today?",
        True,
        "ok",
    )
    assert turn.audio_path is not None and turn.duration_s == 1.0
    txt = turn.audio_path.with_suffix(".txt")
    assert txt.read_text() == "What's the weather like today?"
    assert stat.S_IMODE(txt.stat().st_mode) == 0o600


def test_silence_is_answered_with_didnt_catch(live):
    use_transcriber(live, FakeEngine("  "))
    assert "didn't catch anything" in reply_text(voice_turn(live))


def test_a_note_while_the_model_loads_is_answered_with_waking_up(live):
    from musehost.speech import Transcriber

    transcriber = Transcriber(FakeEngine(load_s=5))
    transcriber.start()
    live.app.state.transcriber = transcriber
    assert "still waking up my ears" in reply_text(voice_turn(live))


def test_a_failed_transcription_is_answered_with_couldnt_make_out(live):
    use_transcriber(live, FakeEngine(fail=True))
    assert "couldn't make out" in reply_text(voice_turn(live))


def test_the_ack_comes_before_the_transcription(live):
    use_transcriber(live, FakeEngine(run_s=1.0))

    async def scenario(client):
        sub = await subscribe(client)
        started = asyncio.get_running_loop().time()
        status, _ = await post_raw(client, note_body(firmware_wav(0.5)), parts=3)
        acked = asyncio.get_running_loop().time() - started
        events = await turn_events(client, sub)
        return acked, events

    acked, events = chat(live, scenario)
    assert acked < 0.8 and "Clio heard" in reply_text(events)


def test_a_slow_transcription_does_not_delay_an_invoke(live):
    from test_link import link_session, on_server, wait_for

    use_transcriber(live, FakeEngine(run_s=1.5))

    async def scenario():
        session = link_session(live, run_command=lambda *a: {"ok": True})
        stop = asyncio.Event()
        task = asyncio.ensure_future(session.run(stop))
        await wait_for(lambda: session.registered_at is not None)
        client = await NoiseClient.connect(live.port, live.ca, live.vm_token())
        try:
            sub = await subscribe(client)
            await post_raw(client, note_body(firmware_wav(0.5)), parts=2)
            loop = asyncio.get_running_loop()
            started = loop.time()
            await asyncio.to_thread(
                lambda: on_server(live, live.app.state.hub.invoke("homelink-abcdef", "x"))
            )
            took = loop.time() - started
            await turn_events(client, sub)
            return took
        finally:
            await client.close()
            stop.set()
            await asyncio.wait_for(task, 5)

    assert asyncio.run(scenario()) < 0.8


def test_transcripts_are_pruned_with_their_notes(tmp_path):
    wav = firmware_wav(0.01)
    first, _ = chat_module.save_voice_note(tmp_path, "homelink-abcdef", wav, keep=2)
    first.with_suffix(".txt").write_text("old")
    for _ in range(2):
        chat_module.save_voice_note(tmp_path, "homelink-abcdef", wav, keep=2)
    assert not first.exists() and not first.with_suffix(".txt").exists()


# -- POST /tts ---------------------------------------------------------------------------

import pytest  # noqa: E402
from test_voice_out import FakeVoice, decode  # noqa: E402

from musehost.voice_out import Synthesizer  # noqa: E402


def use_voice(live, engine):
    synthesizer = Synthesizer(engine)
    synthesizer.start()
    if engine is not None and not getattr(engine, "load_s", 0):
        assert synthesizer._loaded.wait(5)
    live.app.state.synthesizer = synthesizer
    return synthesizer


TTS_HEADERS = {"content-type": "application/json", "accept": "audio/mpeg"}


def tts(live, body: bytes):
    async def scenario(client):
        sid = await client.request("POST", "/tts", body, headers=TTS_HEADERS)
        frames = []
        while True:
            frame = await client.next_frame(sid, timeout=10)
            frames.append(frame)
            if frame.kind == "reset" or frame.value.end_body:
                return frames

    return chat(live, scenario)


def test_tts_streams_the_reply_as_mp3(live):
    use_voice(live, FakeVoice())
    frames = tts(live, json.dumps({"text": "Hello. I'm Clio."}).encode())
    response = frames[0]
    assert response.kind == "response" and response.value.status == 200
    headers = {h.key.lower(): h.value for h in response.value.headers}
    assert headers["content-type"] == "audio/mpeg"
    body = response.value.body + b"".join(f.value.data for f in frames[1:])
    assert len(frames) >= 3  # several chunks: one per sentence, then the tail/end
    seconds, channels, _ = decode(body)
    assert seconds == pytest.approx(1.0, abs=0.12) and channels == 1


@pytest.mark.parametrize(
    "engine, body, status",
    [
        (None, {"text": "hi"}, 404),
        (FakeVoice(load_s=5), {"text": "hi"}, 503),
        (FakeVoice(load_fail=True), {"text": "hi"}, 503),
        (FakeVoice(), {"text": ""}, 400),
        (FakeVoice(), {"text": "x" * 2001}, 400),
        (FakeVoice(), {"words": "hi"}, 400),
    ],
)
def test_tts_refusals(live, engine, body, status):
    synthesizer = use_voice(live, engine)
    if engine is not None and engine.load_fail:
        assert synthesizer._loaded.wait(5)
    frames = tts(live, json.dumps(body).encode())
    assert frames[0].value.status == status


def test_bad_json_is_400(live):
    use_voice(live, FakeVoice())
    assert tts(live, b"{not json")[0].value.status == 400


def test_a_synthesis_failure_mid_reply_resets_the_stream_and_the_session_lives_on(live):
    use_voice(live, FakeVoice(fail=True))

    async def scenario(client):
        sid = await client.request("POST", "/tts", json.dumps({"text": "One. Two."}).encode())
        kinds = []
        while True:
            frame = await client.next_frame(sid, timeout=10)
            kinds.append(frame.kind)
            if frame.kind == "reset" or frame.value.end_body:
                break
        status, _ = await client.response(await client.request("GET", "/identity"))
        return kinds, status

    kinds, status = chat(live, scenario)
    assert kinds[-1] == "reset" and status == 200


def test_synthesis_does_not_block_other_work(live):
    use_voice(live, FakeVoice(run_s=1.0))

    async def scenario(client):
        sid = await client.request("POST", "/tts", json.dumps({"text": "Slow. Reply."}).encode())
        loop = asyncio.get_running_loop()
        started = loop.time()
        status, _ = await client.response(await client.request("GET", "/identity"))
        took = loop.time() - started
        while True:
            frame = await client.next_frame(sid, timeout=10)
            if frame.kind == "reset" or frame.value.end_body:
                break
        return status, took

    status, took = chat(live, scenario)
    assert status == 200 and took < 0.5
