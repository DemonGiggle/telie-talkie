from pathlib import Path

import pytest

from telegram_talkie.config import load_config


def test_example_config_and_relative_paths():
    config = load_config(Path(__file__).parents[1] / "config.example.toml")
    assert config.detection.wake_phrase == "HELLO KITTY"
    assert config.recording.speech_wait_seconds == 5
    assert config.recording.silence_seconds == 1.5
    assert config.recording.max_seconds == 60
    assert config.storage.max_audio_bytes == 512 * 1024 * 1024
    assert config.storage.directory.is_absolute()


@pytest.mark.parametrize(
    "text",
    [
        "[recording]\nmax_seconds=61",
        "[recording]\nsilence_seconds=0",
        "[audio]\nvolume=2",
        "[telegram]\nmax_download_bytes=20000001",
        "[telegram]\nuser_id='101'",
        "[detection]\nv ad_threshold=0.5",
        "[audio]\nvolume=nan",
        "[recording]\nmax_second=3",
        "[unknown]\nvalue=1",
        "[storage]\ndirectory=1",
    ],
)
def test_invalid_configuration_fails_early(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    with pytest.raises(ValueError):
        load_config(path)
