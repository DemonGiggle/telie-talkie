import asyncio

import numpy as np
import pytest

from telegram_talkie.audio import AudioError, SoundDeviceAudio
from telegram_talkie.config import AudioConfig


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
