from __future__ import annotations

import asyncio

import numpy as np

from .config import AudioConfig
from .models import BLOCK_SIZE, SAMPLE_RATE


class AudioError(Exception):
    pass


def tone(sample_rate: int, volume: float, error: bool = False) -> np.ndarray:
    time = np.arange(int(sample_rate * 0.12), dtype=np.float32) / sample_rate
    # Fade both ends to avoid loud clicks.
    beep = np.sin(2 * np.pi * (440 if error else 1000) * time) * np.sin(np.pi * time / 0.12) ** 2
    beep = (beep * volume).astype(np.float32)
    return np.concatenate([beep, np.zeros(int(sample_rate * 0.1)), beep]) if error else beep


class SoundDeviceAudio:
    def __init__(self, config: AudioConfig):
        self.config = config
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=64)
        self.enabled = False
        self.epoch = 0
        self.input = None
        self.sd = None

    async def start(self) -> None:
        import sounddevice as sd

        self.sd = sd
        self.loop = asyncio.get_running_loop()
        self.input = sd.InputStream(
            device=self.config.input_device,
            channels=1,
            samplerate=SAMPLE_RATE,
            dtype="float32",
            blocksize=BLOCK_SIZE,
            callback=self._callback,
        )
        self.input.start()
        self.resume()

    def _callback(self, data, frames, timing, status) -> None:
        if self.enabled:
            self.loop.call_soon_threadsafe(self._push, self.epoch, data[:, 0].copy(), bool(status))

    def _push(self, epoch: int, samples: np.ndarray, overflow: bool) -> None:
        if self.enabled and epoch == self.epoch:
            if self.queue.full():
                self.clear()
                overflow = True
            self.queue.put_nowait((samples, overflow))

    def clear(self) -> None:
        while not self.queue.empty():
            self.queue.get_nowait()

    def suspend(self) -> None:
        self.enabled = False
        self.epoch += 1
        self.clear()

    def resume(self) -> None:
        self.epoch += 1
        self.clear()
        self.enabled = True

    async def read(self, timeout: float = 1) -> np.ndarray:
        try:
            samples, overflow = await asyncio.wait_for(self.queue.get(), timeout)
        except TimeoutError:
            raise AudioError("Microphone stopped producing audio") from None
        if overflow:
            raise AudioError("Microphone overflow; check CPU load and audio configuration")
        return samples

    async def play(self, samples: np.ndarray) -> None:
        self.sd.play(
            samples,
            samplerate=self.config.output_sample_rate,
            device=self.config.output_device,
            blocking=False,
        )
        try:
            status = await asyncio.to_thread(self.sd.wait)
            if status:
                raise AudioError("Speaker output underflow")
        finally:
            self.sd.stop()

    async def close(self) -> None:
        self.suspend()
        if self.sd:
            self.sd.stop()
        if self.input:
            self.input.abort()
            self.input.close()
