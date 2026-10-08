"""Speech-to-text for voice notes, locally on the host.

One worker thread owns the speech engine: it loads the model once (after the
server has started, so devices aren't kept waiting) and then transcribes notes
one at a time, so two notes never fight over the CPU and the event loop stays
free. Callers get a :class:`Result` whose ``status`` says what happened:

- ``ok`` / ``empty``: transcribed (``empty`` = only silence or noise)
- ``loading``: the model isn't ready yet
- ``off``: speech is turned off (``speech_model = ""``)
- ``failed``: the model didn't load, the engine errored, or it took too long

Transcripts are as private as the audio: this module never logs their text.
"""

from __future__ import annotations

import array
import asyncio
import logging
import sys
import threading
import time
import wave
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from musehost.config import HostConfig

log = logging.getLogger(__name__)

DEFAULT_MODEL = "base.en"
DEFAULT_TIMEOUT_S = 30.0


class SpeechEngine(Protocol):
    def load(self) -> None: ...

    def transcribe(self, samples: array.array, rate: int) -> str: ...


@dataclass(frozen=True)
class Result:
    status: str  # ok | empty | loading | off | failed
    text: str = ""
    seconds: float = 0.0


def decode_wav(path: Path) -> tuple[array.array, int]:
    """16-bit PCM WAV → mono float32 samples in [-1, 1) and the sample rate."""
    try:
        with wave.open(str(path), "rb") as w:
            if w.getsampwidth() != 2 or w.getcomptype() != "NONE":
                raise ValueError("voice notes must be 16-bit PCM")
            channels, rate = w.getnchannels(), w.getframerate()
            pcm = array.array("h", w.readframes(w.getnframes()))
    except (wave.Error, EOFError) as exc:
        raise ValueError(f"not a WAV file: {exc}") from None
    if sys.byteorder == "big":
        pcm.byteswap()
    if channels > 1:
        pcm = array.array(
            "h",
            (sum(pcm[i : i + channels]) // channels for i in range(0, len(pcm), channels)),
        )
    return array.array("f", (s / 32768.0 for s in pcm)), rate


class WhisperEngine:
    """faster-whisper on the CPU, loading only from a local model directory."""

    RATE = 16000

    def __init__(self, model_dir: Path, threads: int = 4) -> None:
        self.model_dir = model_dir
        self._threads = threads
        self._model = None

    def load(self) -> None:
        from faster_whisper import WhisperModel

        if not (self.model_dir / "model.bin").exists():
            raise FileNotFoundError(f"no speech model in {self.model_dir}")
        self._model = WhisperModel(
            str(self.model_dir),
            device="cpu",
            compute_type="int8",
            cpu_threads=self._threads,
            local_files_only=True,
        )

    def transcribe(self, samples: array.array, rate: int) -> str:
        import numpy as np

        if rate != self.RATE:
            raise ValueError(f"voice notes must be {self.RATE} Hz, not {rate}")
        segments, _info = self._model.transcribe(
            np.frombuffer(samples, dtype=np.float32),
            language="en",
            beam_size=1,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        return " ".join(segment.text.strip() for segment in segments)


def engine_for(config: HostConfig, models_dir: Path) -> SpeechEngine | None:
    """The engine ``host.toml`` asks for, or None with speech turned off."""
    if not config.speech_model:
        return None
    return WhisperEngine(models_dir / config.speech_model)


def download_model(name: str, models_dir: Path) -> Path:
    """Fetch a faster-whisper model into ``models_dir/name`` once; returns that dir."""
    target = models_dir / name
    if (target / "model.bin").exists():
        return target
    from faster_whisper import download_model as fetch

    models_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    fetch(name, output_dir=str(target))
    return target


class Transcriber:
    def __init__(self, engine: SpeechEngine | None, timeout_s: float = DEFAULT_TIMEOUT_S) -> None:
        self._engine = engine
        self.timeout_s = timeout_s
        self._worker = ThreadPoolExecutor(max_workers=1, thread_name_prefix="speech")
        self._loaded = threading.Event()  # set once loading finished, either way
        self._ready = False

    def start(self) -> None:
        """Begin loading the model in the background (no-op with speech off)."""
        if self._engine is None:
            self._loaded.set()
            return
        self._worker.submit(self._load)

    def _load(self) -> None:
        started = time.monotonic()
        try:
            self._engine.load()
            self._ready = True
            log.info("speech model loaded in %.1f s", time.monotonic() - started)
        except Exception as exc:
            log.error("speech model failed to load: %s: %s", type(exc).__name__, exc)
        finally:
            self._loaded.set()

    @property
    def state(self) -> str:
        """off | loading | ready | failed, as the dashboard shows it."""
        if self._engine is None:
            return "off"
        if not self._loaded.is_set():
            return "loading"
        return "ready" if self._ready else "failed"

    async def wait_loaded(self, timeout: float | None = None) -> bool:
        return await asyncio.to_thread(self._loaded.wait, timeout)

    async def transcribe(self, path: Path) -> Result:
        if self._engine is None:
            return Result("off")
        if not self._loaded.is_set():
            return Result("loading")
        if not self._ready:
            return Result("failed")
        started = time.monotonic()
        future = asyncio.get_running_loop().run_in_executor(self._worker, self._run, path)
        try:
            text = await asyncio.wait_for(asyncio.shield(future), self.timeout_s)
        except TimeoutError:
            log.warning("transcription took over %.0f s; giving up on this note", self.timeout_s)
            return Result("failed", seconds=time.monotonic() - started)
        except Exception as exc:
            log.warning("transcription failed: %s", type(exc).__name__)
            return Result("failed", seconds=time.monotonic() - started)
        seconds = time.monotonic() - started
        text = " ".join(text.split())
        log.info("transcribed a note in %.2f s (%d characters)", seconds, len(text))
        return Result("ok" if text else "empty", text, seconds)

    def _run(self, path: Path) -> str:
        samples, rate = decode_wav(path)
        return self._engine.transcribe(samples, rate)

    def close(self) -> None:
        self._worker.shutdown(wait=False, cancel_futures=True)
