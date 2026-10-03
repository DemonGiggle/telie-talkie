"""Opt in after setup-models: TELIE_TEST_MODELS=1 pytest tests/test_models.py."""

import os
from dataclasses import replace
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


@pytest.mark.parametrize("phrase", ["HELLO KITTY", "HEY BUDDY", "WAKE UP"])
def test_real_bpe_keyword_uses_configured_phrase(config, phrase):
    config = replace(config, detection=replace(config.detection, wake_phrase=phrase))
    text = keyword_text(config)
    assert text.endswith("@" + phrase.replace(" ", "_") + "\n")
    assert "<unk>" not in text


def test_real_bpe_emits_each_unique_keyword(config):
    phrases = (" Hello   Kitty ", "HEY BUDDY", "hello kitty", "WAKE UP")
    config = replace(config, detection=replace(config.detection, wake_phrase=phrases))
    lines = keyword_text(config).splitlines()
    assert [line.split(" @")[1] for line in lines] == ["HELLO_KITTY", "HEY_BUDDY", "WAKE_UP"]
    assert all("<unk>" not in line for line in lines)


@pytest.mark.parametrize("block_size", [128, 512, 1024])
@pytest.mark.parametrize("phrases", ["HEY BUDDY", ("HELLO KITTY", "HEY BUDDY", "WAKE UP")])
async def test_real_models_threaded_silence_and_reset(config, tmp_path, block_size, phrases):
    config = replace(
        config,
        detection=replace(config.detection, wake_phrase=phrases),
        storage=replace(config.storage, directory=tmp_path),
    )
    detector = LocalDetector(config)
    for _ in range(40):
        assert not await detector.wake(np.zeros(block_size, dtype=np.float32))
        assert not await detector.speech(np.zeros(block_size, dtype=np.float32))
    detector.reset()
    assert not await detector.wake(np.zeros(512, dtype=np.float32))
    assert not await detector.speech(np.zeros(512, dtype=np.float32))
