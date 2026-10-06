"""The real Piper voice (slow: downloads en_US-lessac-medium, ~60 MB, once)."""

import asyncio
import os
from pathlib import Path

import pytest
from test_voice_out import decode

from musehost import voice_out

pytestmark = pytest.mark.slow
MODELS = Path(os.environ.get("MUSEHOST_TEST_MODELS", Path.home() / ".cache/musehost-test-models"))


def test_lessac_says_hello():
    voice_dir = voice_out.download_voice("en_US-lessac-medium", MODELS)
    assert (voice_dir / "en_US-lessac-medium.onnx").exists()
    assert voice_out.download_voice("en_US-lessac-medium", MODELS) == voice_dir  # no re-download

    async def scenario():
        synthesizer = voice_out.Synthesizer(voice_out.PiperEngine(voice_dir, "en_US-lessac-medium"))
        synthesizer.start()
        assert await synthesizer.wait_loaded(120)
        return b"".join([c async for c in synthesizer.synthesize_mp3("Hello.")])

    seconds, channels, _ = decode(asyncio.run(scenario()))
    assert 0.4 <= seconds <= 3.0 and channels == 1
