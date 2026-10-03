"""Opt in after setup-models: TELIE_TEST_MODELS=1 pytest tests/test_models.py."""

import os
from pathlib import Path

import numpy as np
import pytest

from telegram_talkie.config import load_config
from telegram_talkie.models import LocalDetector, keyword_text

pytestmark = pytest.mark.skipif(
    os.environ.get("TELIE_TEST_MODELS") != "1",
    reason="Opt-in test requires downloaded keyword/VAD models",
)


@pytest.fixture
def config():
    return load_config(Path(__file__).parents[1] / "config.example.toml")


def test_real_bpe_keyword_is_hello_kitty(config):
    text = keyword_text(config)
    assert text.endswith("@HELLO_KITTY\n")
    assert "<unk>" not in text


@pytest.mark.parametrize("block_size", [128, 512, 1024])
async def test_real_models_threaded_silence_and_reset(config, block_size):
    detector = LocalDetector(config)
    for _ in range(40):
        assert not await detector.wake(np.zeros(block_size, dtype=np.float32))
        assert not await detector.speech(np.zeros(block_size, dtype=np.float32))
    detector.reset()
    assert not await detector.wake(np.zeros(512, dtype=np.float32))
    assert not await detector.speech(np.zeros(512, dtype=np.float32))
