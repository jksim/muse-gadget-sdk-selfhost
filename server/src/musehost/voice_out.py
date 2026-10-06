"""Clio's voice: text to speech on the host, streamed to gadgets as MP3.

A voice engine (Piper on the Pi) turns a reply into 16-bit PCM one sentence at
a time. Each sentence is encoded straight away into constant-bitrate mono MP3
and sent, so the gadget can start playing before the whole reply is
synthesised. CBR with no ID3/Xing header matters: the CoreS3 estimates how much
audio is left from the bitrate. One worker thread loads the voice and runs one
reply at a time; reply text is never logged.
"""

from __future__ import annotations

import asyncio
import logging
import threading
import time
from collections.abc import AsyncIterator, Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Protocol

log = logging.getLogger(__name__)

DEFAULT_VOICE = "en_US-lessac-medium"
MP3_BITRATE = 48_000
_END = object()


class VoiceEngine(Protocol):
    def load(self) -> None: ...

    def synthesize(self, text: str) -> Iterator[tuple[bytes, int]]:
        """(pcm16 mono bytes, sample rate) per sentence."""
        ...


class SynthesisFailed(Exception):
    """The voice engine failed partway through a reply."""


class Mp3Encoder:
    """Feeds PCM in, returns the MP3 frames ready so far (CBR, mono, no tags)."""

    def __init__(self, rate: int, bitrate: int = MP3_BITRATE) -> None:
        import av

        self._av = av
        self.rate = rate
        self._ctx = av.CodecContext.create("libmp3lame", "w")
        self._ctx.sample_rate = rate
        self._ctx.layout = "mono"
        self._ctx.format = "s16p"
        self._ctx.bit_rate = bitrate

    def feed(self, pcm: bytes) -> bytes:
        import numpy as np

        samples = np.frombuffer(pcm, dtype="<i2").reshape(1, -1)
        frame = self._av.AudioFrame.from_ndarray(samples, format="s16p", layout="mono")
        frame.sample_rate = self.rate
        return b"".join(bytes(p) for p in self._ctx.encode(frame))

    def close(self) -> bytes:
        return b"".join(bytes(p) for p in self._ctx.encode(None))


class Synthesizer:
    def __init__(self, engine: VoiceEngine | None) -> None:
        self._engine = engine
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="voice")
        self._loaded = threading.Event()
        self._ready = False

    @property
    def state(self) -> str:
        if self._engine is None:
            return "off"
        if not self._loaded.is_set():
            return "loading"
        return "ready" if self._ready else "failed"

    def start(self) -> None:
        if self._engine is None:
            self._loaded.set()
            return
        self._worker.submit(self._load)

    def _load(self) -> None:
        started = time.monotonic()
        try:
            self._engine.load()
            self._ready = True
            log.info("voice loaded in %.1f s", time.monotonic() - started)
        except Exception as exc:
            log.error("voice failed to load: %s: %s", type(exc).__name__, exc)
        finally:
            self._loaded.set()

    async def wait_loaded(self, timeout: float | None = None) -> bool:
        return await asyncio.to_thread(self._loaded.wait, timeout)

    async def synthesize_mp3(self, text: str) -> AsyncIterator[bytes]:
        """MP3 bytes for ``text``, a chunk per sentence; raises SynthesisFailed."""
        loop = asyncio.get_running_loop()
        queue: asyncio.Queue = asyncio.Queue()

        def put(item) -> None:
            loop.call_soon_threadsafe(queue.put_nowait, item)

        def run() -> None:
            started = time.monotonic()
            encoder = None
            audio_s = 0.0
            size = 0
            try:
                for pcm, rate in self._engine.synthesize(text):
                    encoder = encoder or Mp3Encoder(rate)
                    audio_s += len(pcm) / 2 / rate
                    chunk = encoder.feed(pcm)
                    size += len(chunk)
                    if chunk:
                        put(chunk)
                if encoder is not None:
                    tail = encoder.close()
                    size += len(tail)
                    if tail:
                        put(tail)
                log.info(
                    "spoke %d characters: %.1f s of audio in %.2f s, %d bytes",
                    len(text),
                    audio_s,
                    time.monotonic() - started,
                    size,
                )
                put(_END)
            except Exception as exc:
                log.warning("speech synthesis failed: %s", type(exc).__name__)
                put(SynthesisFailed(type(exc).__name__))

        self._worker.submit(run)
        while True:
            item = await queue.get()
            if item is _END:
                return
            if isinstance(item, SynthesisFailed):
                raise item
            yield item

    def close(self) -> None:
        self._worker.shutdown(wait=False, cancel_futures=True)


class PiperEngine:
    """Piper, CPU, loading only from a local ``<voice>.onnx`` (+ ``.onnx.json``)."""

    def __init__(self, voice_dir: Path, voice: str) -> None:
        self.model_path = voice_dir / f"{voice}.onnx"
        self._voice = None

    def load(self) -> None:
        from piper import PiperVoice

        if not self.model_path.exists():
            raise FileNotFoundError(f"no Piper voice at {self.model_path}")
        self._voice = PiperVoice.load(self.model_path)

    def synthesize(self, text: str) -> Iterator[tuple[bytes, int]]:
        for chunk in self._voice.synthesize(text):  # one chunk per sentence
            yield chunk.audio_int16_bytes, chunk.sample_rate


def engine_for(config, models_dir: Path) -> VoiceEngine | None:
    """The voice ``host.toml`` asks for, or None with speech output off."""
    if not config.tts_voice:
        return None
    return PiperEngine(models_dir / "piper", config.tts_voice)


def download_voice(voice: str, models_dir: Path) -> Path:
    """Fetch a Piper voice into ``models_dir/piper`` once; returns that directory."""
    target = models_dir / "piper"
    if (target / f"{voice}.onnx").exists() and (target / f"{voice}.onnx.json").exists():
        return target
    from piper.download_voices import download_voice as fetch

    target.mkdir(mode=0o700, parents=True, exist_ok=True)
    fetch(voice, target)
    return target
