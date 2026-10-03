from __future__ import annotations

import asyncio
from dataclasses import replace

import numpy as np
import pytest

from telegram_talkie.app import Talkie
from telegram_talkie.config import Config, StorageConfig, TelegramConfig
from telegram_talkie.storage import Store


def message(update_id=1, kind="voice", chat_id=101, user_id=101, **media):
    return {
        "update_id": update_id,
        "message": {
            "chat": {"id": chat_id, "type": "private"},
            "from": {"id": user_id},
            kind: {"file_id": str(update_id), "duration": 1, **media},
        },
    }


class FakeTelegram:
    def __init__(self):
        self.batch = []
        self.offsets = []
        self.downloads = []
        self.sent = []
        self.notices = []
        self.failure = None
        self.blobs = {}

    async def updates(self, offset):
        self.offsets.append(offset)
        await asyncio.sleep(0)
        return self.batch

    async def download(self, file_id, max_bytes):
        self.downloads.append(file_id)
        if self.failure:
            raise self.failure
        yield self.blobs.get(file_id, b"valid-audio")

    async def send_voice(self, chat_id, path):
        if self.failure:
            raise self.failure
        self.sent.append((chat_id, path.read_bytes()))

    async def send_text(self, chat_id, text):
        if self.failure:
            raise self.failure
        self.notices.append((chat_id, text))


class FakeAudio:
    def __init__(self):
        self.enabled = True
        self.frames = []
        self.buffered = []
        self.played = []
        self.events = []
        self.on_play = None
        self.on_read = None

    async def start(self):
        self.resume()

    async def close(self):
        self.suspend()

    def suspend(self):
        self.enabled = False
        self.buffered.clear()
        self.events.append("suspend")

    def resume(self):
        self.buffered.clear()
        self.enabled = True
        self.events.append("resume")

    async def play(self, samples):
        assert not self.enabled, "Microphone must be suspended during all output"
        self.played.append(samples.copy())
        self.events.append("play")
        # Simulate queued callback data containing the wake phrase from the speaker.
        self.buffered.append(np.full(512, 9, dtype=np.float32))
        if self.on_play:
            await self.on_play()
        await asyncio.sleep(0)

    async def read(self):
        assert self.enabled
        if self.on_read:
            await self.on_read()
        await asyncio.sleep(0)
        self.events.append("read")
        if self.buffered:
            return self.buffered.pop(0)
        if not self.frames:
            raise EOFError("Fake microphone exhausted")
        return self.frames.pop(0)


class FakeDetector:
    def __init__(self):
        self.wake_calls = []
        self.speech_calls = []
        self.resets = 0

    def reset(self):
        self.resets += 1

    async def wake(self, samples):
        self.wake_calls.append(samples.copy())
        return samples[0] == 9

    async def speech(self, samples):
        self.speech_calls.append(samples.copy())
        return samples[0] == 1


class FakeCodec:
    def __init__(self):
        self.encoded = []
        self.decode_error = None

    async def encode(self, samples):
        self.encoded.append(samples.copy())
        return b"OggS-fake"

    async def decode(self, path, sample_rate, max_seconds):
        if self.decode_error:
            raise self.decode_error
        assert path.read_bytes()
        return np.full(1024, 0.1, dtype=np.float32)


def frames(value: int, seconds: float) -> list[np.ndarray]:
    count = round(seconds * 16000 / 512)
    return [np.full(512, value, dtype=np.float32) for _ in range(count)]


@pytest.fixture
def rig(tmp_path):
    config = Config(
        telegram=TelegramConfig(chat_id=101, user_id=101), storage=StorageConfig(tmp_path / "state")
    )
    config = replace(config, audio=replace(config.audio, settle_seconds=0))
    store = Store(config.storage.directory, config.storage.max_audio_bytes)
    store.bind("123:fixture-only", 101, 101)
    telegram, audio, detector, codec = FakeTelegram(), FakeAudio(), FakeDetector(), FakeCodec()
    app = Talkie(config, store, telegram, audio, detector, codec)
    yield app
    app.store.close()
