from dataclasses import fields
from pathlib import Path

import pytest

from telegram_talkie.config import Config, load_config, tomllib


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
        "[recording]\nmax_seconds=0",
        "[recording]\nsilence_seconds=0",
        "[audio]\nvolume=2",
        "[telegram]\nmax_download_bytes=20000001",
        "[telegram]\nuser_id='101'",
        "[telegram]\ntoken_file_env=''",
        "[telegram]\ntoken_env='INVALID NAME'",
        "[telegram]\ntoken_file_env='TELEGRAM_BOT_TOKEN'",
        "[telegram]\ntoken_credential='../token'",
        "[detection]\nv ad_threshold=0.5",
        "[audio]\nvolume=nan",
        "[recording]\nmax_second=3",
        "[unknown]\nvalue=1",
        "[storage]\ndirectory=1",
        "[audio]\ninput_sample_rate=0",
        "[audio]\ninput_channels=2\ninput_channel=2",
        "[audio]\noutput_channels=0",
        "[audio]\nprocessing_block_size=0",
        "[audio]\nbuffer_blocks=0",
        "[audio]\ninput_latency='fast'",
        "[audio]\noutput_latency=nan",
        "[audio]\ninput_gain=-1",
        "[audio]\nresample_quality='INVALID'",
        "[network]\nread_timeout_seconds=30",
        "[network]\nmax_connections=2\nmax_keepalive_connections=3",
        "[network]\nchunk_bytes=0",
        "[retry]\ninitial_seconds=10\nmax_seconds=5",
        "[retry]\nmultiplier=0.5",
        "[retry]\njitter_ratio=-0.1",
        "[runtime]\nqueue_interval_seconds=0",
        "[pairing]\ncode_bytes=8",
        "[logging]\nlevel='VERBOSE'",
        "[tones]\nready_frequency_hz=48000",
        "[tones]\nerror_repeats=0",
        "[codec]\nbitrate_bps=100",
        "[codec]\nsample_rate=44100",
        "[codec]\ncomplexity=11",
        "[codec]\ntimeout_seconds=0",
        "[codec]\nallowed_input_formats=[]",
        "[models]\nencoder=''",
        "[models]\nkws_archive_root='../outside'",
        "[models]\nmax_archive_bytes=-1",
        "[telegram]\napi_base_url='https://user:password@example.invalid'",
    ],
)
def test_invalid_configuration_fails_early(tmp_path, text):
    path = tmp_path / "config.toml"
    path.write_text(text)
    with pytest.raises(ValueError):
        load_config(path)


def test_old_configuration_keeps_defaults_and_longer_recording_is_allowed(tmp_path):
    path = tmp_path / "config.toml"
    path.write_text("[recording]\nmax_seconds=120\n[telegram]\nmax_incoming_seconds=7200")
    config = load_config(path)
    assert config.recording.max_seconds == 120
    assert config.telegram.max_incoming_seconds == 7200
    assert config.audio.buffer_blocks == 64
    assert config.codec.bitrate_bps == 24000
    assert config.network.connect_timeout_seconds == 15
    assert config.models.encoder.endswith(".int8.onnx")


def test_every_configuration_field_is_documented_in_example():
    path = Path(__file__).parents[1] / "config.example.toml"
    raw = tomllib.loads(path.read_text())
    for table in fields(Config):
        defaults = getattr(Config(), table.name)
        listed = set(raw[table.name])
        if table.name == "audio":
            listed |= {"input_device", "output_device"}  # Commented out to select defaults.
        assert listed == {field.name for field in fields(defaults)}, table.name
