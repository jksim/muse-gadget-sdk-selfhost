"""The real faster-whisper engine (slow: downloads tiny.en once, ~75 MB)."""

import asyncio
import os
from pathlib import Path

import pytest
from test_speech import write_wav

from musehost import speech

pytestmark = pytest.mark.slow
MODELS = Path(os.environ.get("MUSEHOST_TEST_MODELS", Path.home() / ".cache/musehost-test-models"))


def test_tiny_en_transcribes_silence_to_nothing(tmp_path):
    model_dir = speech.download_model("tiny.en", MODELS)
    assert (model_dir / "model.bin").exists()
    assert speech.download_model("tiny.en", MODELS) == model_dir  # second call: no download

    async def scenario():
        transcriber = speech.Transcriber(speech.WhisperEngine(model_dir))
        transcriber.start()
        assert await transcriber.wait_loaded(120)
        return await transcriber.transcribe(write_wav(tmp_path / "s.wav", seconds=2.0, value=0))

    result = asyncio.run(scenario())
    assert (result.status, result.text) == ("empty", "")
