import asyncio
from types import SimpleNamespace

import numpy as np
import pytest

from telegram_talkie.audio import AudioError, SoundDeviceAudio, tone
from telegram_talkie.config import AudioConfig, TonesConfig


async def test_delayed_callbacks_from_before_playback_are_discarded():
    audio = SoundDeviceAudio(AudioConfig())
    audio.resume()
    old_epoch = audio.epoch
    audio.suspend()
    audio._push(old_epoch, np.ones(512), False)
    assert audio.queue.empty()
    audio.resume()
    audio._push(old_epoch, np.ones(512), False)
    assert audio.queue.empty()
    audio._push(audio.epoch, np.zeros(512), False)
    assert np.all(await audio.read() == 0)


@pytest.mark.parametrize("sample_rate", [8000, 44100, 48000])
async def test_native_input_is_resampled_and_reblocked_without_changing_pitch(sample_rate):
    config = AudioConfig(
        input_sample_rate=sample_rate, block_size=1024, processing_block_size=256, buffer_blocks=200
    )
    audio = SoundDeviceAudio(config)
    audio.resume()
    time = np.arange(sample_rate, dtype=np.float32) / sample_rate
    source = (0.2 * np.sin(2 * np.pi * 440 * time)).astype(np.float32)
    for offset in range(0, len(source), config.block_size):
        audio._push(audio.epoch, source[offset : offset + config.block_size], False)
    output = []
    while not audio.queue.empty() or len(audio.pending) >= config.processing_block_size:
        try:
            output.append(await audio.read(timeout=0.01))
        except AudioError:
            break  # Filter lookahead may retain a short tail until more hardware input arrives.
    result = np.concatenate(output)
    assert all(len(frame) == 256 for frame in output)
    assert 0.8 * 16000 < len(result) <= 16000
    peak_hz = np.argmax(abs(np.fft.rfft(result))) * 16000 / len(result)
    assert peak_hz == pytest.approx(440, abs=2)
    assert np.sqrt(np.mean(result**2)) == pytest.approx(0.2 / np.sqrt(2), abs=0.01)
    audio.suspend()
    audio.resume()
    assert not len(audio.pending) and audio.queue.empty()
    assert audio.resampler.delay() == 0


@pytest.mark.parametrize("channel,expected", [(0, 0.1), (1, 0.4), (-1, 0.25)])
async def test_stereo_selection_downmix_gain_and_buffer_settings(channel, expected):
    audio = SoundDeviceAudio(
        AudioConfig(
            input_channels=2,
            input_channel=channel,
            input_gain=0.5,
            buffer_blocks=3,
            processing_block_size=128,
        )
    )
    audio.loop = asyncio.get_running_loop()
    audio.resume()
    data = np.tile(np.array([0.2, 0.8], dtype=np.float32), (128, 1))
    audio._callback(data, 128, None, False)
    data[:] = 0
    assert np.allclose(await audio.read(), expected)
    assert audio.queue.maxsize == 3


async def test_configured_device_settings_and_stereo_output_reach_portaudio(monkeypatch):
    calls = {}

    class Stream:
        def __init__(self, **kwargs):
            calls["input"] = kwargs

        def start(self):
            pass

        def abort(self):
            pass

        def close(self):
            calls["closed"] = True

    def play(samples, **kwargs):
        calls["output"] = kwargs
        calls["samples"] = samples

    monkeypatch.setitem(
        __import__("sys").modules,
        "sounddevice",
        SimpleNamespace(InputStream=Stream, play=play, wait=lambda: None, stop=lambda: None),
    )
    config = AudioConfig(
        input_sample_rate=48000,
        input_channels=2,
        output_channels=2,
        block_size=1536,
        input_latency=0.05,
        output_latency="low",
    )
    audio = SoundDeviceAudio(config)
    await audio.start()
    audio.suspend()
    await audio.play(np.ones(256, dtype=np.float32))
    await audio.close()
    assert calls["input"]["samplerate"] == 48000
    assert calls["input"]["channels"] == 2
    assert calls["input"]["blocksize"] == 1536
    assert calls["input"]["latency"] == 0.05
    assert calls["output"]["latency"] == "low"
    assert calls["samples"].shape == (256, 2)
    assert np.all(calls["samples"][:, 0] == calls["samples"][:, 1])
    assert calls["closed"]


async def test_microphone_timeout_uses_configuration():
    audio = SoundDeviceAudio(AudioConfig(read_timeout_seconds=0.001))
    audio.resume()
    with pytest.raises(AudioError, match="stopped"):
        await audio.read()


def test_custom_tone_pitch_duration_and_repeats():
    settings = TonesConfig(
        ready_frequency_hz=750,
        error_frequency_hz=300,
        duration_seconds=0.2,
        ready_repeats=2,
        error_repeats=3,
        gap_seconds=0.05,
    )
    for error, repeats, frequency in [(False, 2, 750), (True, 3, 300)]:
        samples = tone(16000, 0.3, error=error, settings=settings)
        assert len(samples) == repeats * 3200 + (repeats - 1) * 800
        assert np.all(samples[3200:4000] == 0)
        peak_hz = np.argmax(abs(np.fft.rfft(samples[:3200]))) * 16000 / 3200
        assert peak_hz == frequency
        assert abs(samples).max() <= 0.3


async def test_callback_overflow_is_reported_instead_of_using_stale_audio():
    audio = SoundDeviceAudio(AudioConfig())
    audio.resume()
    for _ in range(65):
        audio._push(audio.epoch, np.zeros(512), False)
    with pytest.raises(AudioError, match="overflow"):
        await audio.read()


async def test_callback_thread_data_is_copied_and_epoch_guarded():
    audio = SoundDeviceAudio(AudioConfig())
    audio.loop = asyncio.get_running_loop()
    audio.resume()
    data = np.ones((512, 1), dtype=np.float32)
    audio._callback(data, 512, None, False)
    data[:] = 0
    assert np.all(await audio.read() == 1)
    audio._callback(data, 512, None, False)
    audio.suspend()
    audio.resume()
    await asyncio.sleep(0)
    assert audio.queue.empty()
