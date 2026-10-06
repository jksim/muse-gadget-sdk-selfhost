"""The Transcriber with a fake engine: states, queueing, timeouts, WAV decoding."""

import asyncio
import struct
import threading
import time
import wave

import pytest

from musehost import speech


def write_wav(path, seconds=0.5, rate=16000, width=2, channels=1, value=1000):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(channels)
        w.setsampwidth(width)
        w.setframerate(rate)
        if width == 2:
            frame = struct.pack("<h", value) * channels
        else:
            frame = bytes([128 + value % 100]) * channels
        w.writeframes(frame * int(seconds * rate))
    return path


class FakeEngine:
    def __init__(self, text="hello clio", load_s=0.0, run_s=0.0, fail=False, load_fail=False):
        self.text, self.load_s, self.run_s = text, load_s, run_s
        self.fail, self.load_fail = fail, load_fail
        self.calls = []
        self.active = 0
        self.max_active = 0
        self._lock = threading.Lock()

    def load(self):
        time.sleep(self.load_s)
        if self.load_fail:
            raise FileNotFoundError("no model here")

    def transcribe(self, samples, rate):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            self.calls.append((len(samples), rate))
            time.sleep(self.run_s)
            if self.fail:
                raise RuntimeError("engine blew up")
            return self.text
        finally:
            with self._lock:
                self.active -= 1


async def started(engine, timeout_s=30.0):
    transcriber = speech.Transcriber(engine, timeout_s=timeout_s)
    transcriber.start()
    await transcriber.wait_loaded(5)
    return transcriber


def test_a_transcript_comes_back_ok(tmp_path):
    async def scenario():
        transcriber = await started(FakeEngine("  what's the weather like today?  "))
        return await transcriber.transcribe(write_wav(tmp_path / "a.wav"))

    result = asyncio.run(scenario())
    assert (result.status, result.text) == ("ok", "what's the weather like today?")
    assert result.seconds >= 0


def test_an_empty_transcript_is_reported_as_empty(tmp_path):
    async def scenario():
        transcriber = await started(FakeEngine("   "))
        return await transcriber.transcribe(write_wav(tmp_path / "a.wav"))

    result = asyncio.run(scenario())
    assert (result.status, result.text) == ("empty", "")


def test_while_the_model_loads_notes_are_answered_as_loading(tmp_path):
    async def scenario():
        transcriber = speech.Transcriber(FakeEngine(load_s=1.0))
        transcriber.start()
        return await transcriber.transcribe(write_wav(tmp_path / "a.wav"))

    assert asyncio.run(scenario()).status == "loading"


def test_with_speech_off_notes_are_not_transcribed(tmp_path):
    async def scenario():
        transcriber = speech.Transcriber(None)
        transcriber.start()
        return await transcriber.transcribe(write_wav(tmp_path / "a.wav"))

    assert asyncio.run(scenario()).status == "off"


def test_an_engine_error_fails_that_note_only(tmp_path):
    engine = FakeEngine(fail=True)

    async def scenario():
        transcriber = await started(engine)
        first = await transcriber.transcribe(write_wav(tmp_path / "a.wav"))
        engine.fail = False
        second = await transcriber.transcribe(write_wav(tmp_path / "b.wav"))
        return first, second

    first, second = asyncio.run(scenario())
    assert (first.status, second.status) == ("failed", "ok")


def test_a_slow_transcription_times_out_and_the_next_one_still_works(tmp_path):
    engine = FakeEngine(run_s=0.6)

    async def scenario():
        transcriber = await started(engine, timeout_s=0.2)
        first = await transcriber.transcribe(write_wav(tmp_path / "a.wav"))
        engine.run_s = 0.0
        await asyncio.sleep(0.6)  # let the stuck call finish on the worker
        transcriber.timeout_s = 5
        second = await transcriber.transcribe(write_wav(tmp_path / "b.wav"))
        return first, second

    first, second = asyncio.run(scenario())
    assert (first.status, second.status) == ("failed", "ok")


def test_a_model_that_fails_to_load_fails_notes_instead_of_crashing(tmp_path):
    async def scenario():
        transcriber = speech.Transcriber(FakeEngine(load_fail=True))
        transcriber.start()
        await transcriber.wait_loaded(5)
        return await transcriber.transcribe(write_wav(tmp_path / "a.wav"))

    assert asyncio.run(scenario()).status == "failed"


def test_concurrent_notes_are_transcribed_one_at_a_time(tmp_path):
    engine = FakeEngine(run_s=0.1)

    async def scenario():
        transcriber = await started(engine)
        paths = [write_wav(tmp_path / f"{i}.wav") for i in range(3)]
        return await asyncio.gather(*(transcriber.transcribe(p) for p in paths))

    results = asyncio.run(scenario())
    assert [r.status for r in results] == ["ok"] * 3
    assert engine.max_active == 1


def test_the_event_loop_stays_free_during_a_transcription(tmp_path):
    engine = FakeEngine(run_s=0.5)

    async def scenario():
        transcriber = await started(engine)
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                ticks += 1
                await asyncio.sleep(0.01)

        task = asyncio.ensure_future(ticker())
        await transcriber.transcribe(write_wav(tmp_path / "a.wav"))
        task.cancel()
        return ticks

    assert asyncio.run(scenario()) > 20


# -- WAV decoding --------------------------------------------------------------------


def test_decoding_a_16_bit_mono_note_gives_float_samples(tmp_path):
    samples, rate = speech.decode_wav(write_wav(tmp_path / "a.wav", seconds=0.25, value=16384))
    assert rate == 16000 and len(samples) == 4000
    assert samples[0] == pytest.approx(0.5)
    assert all(-1.0 <= s < 1.0 for s in samples)


def test_stereo_is_mixed_down_to_mono(tmp_path):
    samples, _ = speech.decode_wav(write_wav(tmp_path / "s.wav", seconds=0.1, channels=2))
    assert len(samples) == 1600


@pytest.mark.parametrize(
    "make", [lambda p: write_wav(p, width=1), lambda p: (p.write_bytes(b"not audio"), p)[1]]
)
def test_anything_but_16_bit_pcm_wav_is_refused(tmp_path, make):
    with pytest.raises(ValueError):
        speech.decode_wav(make(tmp_path / "x.wav"))


# -- Engines, models and the app -------------------------------------------------------


def test_with_speech_off_there_is_no_engine(tmp_path):
    from musehost.config import HostConfig

    config = HostConfig(hostnames=("h",), ips=(), speech_model="")
    assert speech.engine_for(config, tmp_path) is None


def test_the_configured_model_is_loaded_from_the_models_dir(tmp_path):
    from musehost.config import HostConfig

    engine = speech.engine_for(HostConfig(hostnames=("h",), ips=()), tmp_path)
    assert engine.model_dir == tmp_path / "base.en"


def test_a_missing_model_fails_to_load_without_touching_the_network(tmp_path):
    async def scenario():
        transcriber = speech.Transcriber(speech.WhisperEngine(tmp_path / "nope"))
        transcriber.start()
        await transcriber.wait_loaded(30)
        return await transcriber.transcribe(write_wav(tmp_path / "a.wav"))

    assert asyncio.run(scenario()).status == "failed"


def test_the_server_answers_while_the_model_is_still_loading(state, monkeypatch):
    import http.client
    import ssl
    import tomllib

    from conftest import serving

    monkeypatch.setattr(speech, "engine_for", lambda config, models: FakeEngine(load_s=3))
    port = tomllib.loads((state / "host.toml").read_text())["port"]
    started = time.monotonic()
    with serving(state):
        conn = http.client.HTTPSConnection(
            "localhost", port, context=ssl.create_default_context(cafile=str(state / "ca.pem"))
        )
        conn.request("GET", "/healthz")
        assert conn.getresponse().status == 200
        conn.close()
        assert time.monotonic() - started < 2.5
