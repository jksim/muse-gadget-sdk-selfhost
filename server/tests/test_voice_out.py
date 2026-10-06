"""Speech output with a fake voice: MP3 encoding, sentence streaming, states, queueing."""

import asyncio
import io
import math
import struct
import threading
import time

import av
import pytest

from musehost import voice_out

RATE = 22050


def tone(seconds: float, rate: int = RATE) -> bytes:
    n = int(seconds * rate)
    return b"".join(struct.pack("<h", int(8000 * math.sin(i / 8))) for i in range(n))


class FakeVoice:
    """A 0.5 s tone per sentence (split on '. ')."""

    def __init__(self, load_s=0.0, run_s=0.0, fail=False, load_fail=False):
        self.load_s, self.run_s, self.fail, self.load_fail = load_s, run_s, fail, load_fail
        self.active = self.max_active = 0
        self._lock = threading.Lock()

    def load(self):
        time.sleep(self.load_s)
        if self.load_fail:
            raise FileNotFoundError("no voice here")

    def synthesize(self, text):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            for _sentence in [s for s in text.split(". ") if s.strip()]:
                time.sleep(self.run_s)
                if self.fail:
                    raise RuntimeError("voice broke")
                yield tone(0.5), RATE
        finally:
            with self._lock:
                self.active -= 1


def decode(mp3: bytes):
    with av.open(io.BytesIO(mp3), format="mp3") as container:
        stream = container.streams.audio[0]
        frames = list(container.decode(stream))
        seconds = sum(f.samples for f in frames) / stream.rate
        return seconds, stream.channels, stream.rate


def frame_bitrates(mp3: bytes) -> set[int]:
    """Bitrates of each MPEG frame header (CBR = one value)."""
    with av.open(io.BytesIO(mp3), format="mp3") as container:
        return {p.size for p in container.demux(container.streams.audio[0]) if p.size}


async def started(engine):
    synthesizer = voice_out.Synthesizer(engine)
    synthesizer.start()
    await synthesizer.wait_loaded(5)
    return synthesizer


async def collect(synthesizer, text):
    return [chunk async for chunk in synthesizer.synthesize_mp3(text)]


def test_two_sentences_stream_as_cbr_mono_mp3():
    async def scenario():
        return await collect(await started(FakeVoice()), "One. Two.")

    chunks = asyncio.run(scenario())
    assert len(chunks) >= 2
    mp3 = b"".join(chunks)
    seconds, channels, rate = decode(mp3)
    assert seconds == pytest.approx(1.0, abs=0.12)
    assert (channels, rate) == (1, RATE)
    assert not mp3.startswith(b"ID3")  # no tag: the device estimates length from bitrate
    assert len(frame_bitrates(mp3)) <= 2  # constant frame size (padding bit may vary by one)


def test_the_first_chunk_arrives_before_the_last_sentence_is_synthesised():
    async def scenario():
        synthesizer = await started(FakeVoice(run_s=0.3))
        started_at = time.monotonic()
        stamps = []
        async for _ in synthesizer.synthesize_mp3("One. Two. Three."):
            stamps.append(time.monotonic() - started_at)
        return stamps

    stamps = asyncio.run(scenario())
    assert stamps[0] < 0.6 and stamps[-1] > 0.8


def test_with_speech_output_off_the_state_is_off():
    synthesizer = voice_out.Synthesizer(None)
    synthesizer.start()
    assert synthesizer.state == "off"


def test_before_the_voice_loads_the_state_is_loading():
    synthesizer = voice_out.Synthesizer(FakeVoice(load_s=1.0))
    synthesizer.start()
    assert synthesizer.state == "loading"


def test_a_voice_that_fails_to_load_is_failed():
    async def scenario():
        synthesizer = voice_out.Synthesizer(FakeVoice(load_fail=True))
        synthesizer.start()
        await synthesizer.wait_loaded(5)
        return synthesizer.state

    assert asyncio.run(scenario()) == "failed"


def test_an_engine_error_mid_reply_raises_and_the_next_reply_works():
    voice = FakeVoice(fail=True)

    async def scenario():
        synthesizer = await started(voice)
        with pytest.raises(voice_out.SynthesisFailed):
            await collect(synthesizer, "One. Two.")
        voice.fail = False
        return await collect(synthesizer, "Three.")

    assert asyncio.run(scenario())


def test_concurrent_replies_are_synthesised_one_at_a_time():
    voice = FakeVoice(run_s=0.05)

    async def scenario():
        synthesizer = await started(voice)
        return await asyncio.gather(*(collect(synthesizer, "A. B.") for _ in range(3)))

    results = asyncio.run(scenario())
    assert all(results) and voice.max_active == 1


def test_reply_text_is_not_logged(caplog):
    import logging

    caplog.set_level(logging.DEBUG)

    async def scenario():
        return await collect(await started(FakeVoice()), "The vault code is 4711.")

    asyncio.run(scenario())
    assert "4711" not in caplog.text and "characters" in caplog.text


def test_tts_voice_defaults_and_round_trips(tmp_path):
    import dataclasses

    from musehost.config import HostConfig

    config = HostConfig(hostnames=("muse-host.local",), ips=(), port=443)
    assert config.tts_voice == "en_US-lessac-medium"
    off = dataclasses.replace(config, tts_voice="")
    off.save(tmp_path / "host.toml")
    assert HostConfig.load(tmp_path / "host.toml") == off


def test_the_configured_voice_is_piper_from_the_models_dir(tmp_path):
    from musehost.config import HostConfig

    engine = voice_out.engine_for(HostConfig(hostnames=("h",), ips=()), tmp_path)
    assert isinstance(engine, voice_out.PiperEngine)
    assert engine.model_path == tmp_path / "piper" / "en_US-lessac-medium.onnx"


def test_with_tts_voice_empty_there_is_no_engine(tmp_path):
    from musehost.config import HostConfig

    assert (
        voice_out.engine_for(HostConfig(hostnames=("h",), ips=(), tts_voice=""), tmp_path) is None
    )


def test_a_missing_voice_fails_to_load_without_downloading(tmp_path):
    async def scenario():
        synthesizer = voice_out.Synthesizer(voice_out.PiperEngine(tmp_path, "en_US-lessac-medium"))
        synthesizer.start()
        await synthesizer.wait_loaded(30)
        return synthesizer.state

    assert asyncio.run(scenario()) == "failed"
