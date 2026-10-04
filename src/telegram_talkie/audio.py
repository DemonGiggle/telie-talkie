from __future__ import annotations

import asyncio

import numpy as np
from anyio import fail_after

from .config import MODEL_SAMPLE_RATE, AudioConfig, TonesConfig


class AudioError(Exception):
    pass


def tone(
    sample_rate: int,
    volume: float,
    error: bool = False,
    settings: TonesConfig | None = None,
    *,
    duration_seconds: float | None = None,
    repeats: int | None = None,
) -> np.ndarray:
    settings = settings or TonesConfig()
    duration = settings.duration_seconds if duration_seconds is None else duration_seconds
    time = np.arange(int(sample_rate * duration), dtype=np.float32) / sample_rate
    # Fade both ends to avoid loud clicks.
    frequency = settings.error_frequency_hz if error else settings.ready_frequency_hz
    beep = np.sin(2 * np.pi * frequency * time) * np.sin(np.pi * time / duration) ** 2
    beep = (beep * volume).astype(np.float32)
    repeats = (
        repeats
        if repeats is not None
        else (settings.error_repeats if error else settings.ready_repeats)
    )
    gap = np.zeros(int(sample_rate * settings.gap_seconds), dtype=np.float32)
    parts = [part for _ in range(repeats - 1) for part in (beep, gap)] + [beep]
    return np.concatenate(parts)


class SoundDeviceAudio:
    def __init__(self, config: AudioConfig):
        self.config = config
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=config.buffer_blocks)
        self.pending = np.empty(0, dtype=np.float32)
        self.resampler = None
        if config.input_sample_rate != MODEL_SAMPLE_RATE:
            import soxr

            self.resampler = soxr.ResampleStream(
                config.input_sample_rate,
                MODEL_SAMPLE_RATE,
                1,
                dtype="float32",
                quality=config.resample_quality,
            )
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
            channels=self.config.input_channels,
            samplerate=self.config.input_sample_rate,
            dtype="float32",
            blocksize=self.config.block_size,
            latency=self.config.input_latency,
            callback=self._callback,
        )
        self.input.start()
        self.resume()

    def _callback(self, data, frames, timing, status) -> None:
        if self.enabled:
            epoch = self.epoch
            samples = (
                data.mean(axis=1)
                if self.config.input_channel == -1
                else data[:, self.config.input_channel].copy()
            )
            samples = np.clip(samples * self.config.input_gain, -1, 1).astype(np.float32)
            self.loop.call_soon_threadsafe(self._push, epoch, samples, bool(status))

    def _push(self, epoch: int, samples: np.ndarray, overflow: bool) -> None:
        if self.enabled and epoch == self.epoch:
            if self.queue.full():
                self.clear()
                overflow = True
            self.queue.put_nowait((samples, overflow))

    def clear(self) -> None:
        while not self.queue.empty():
            self.queue.get_nowait()
        self.pending = np.empty(0, dtype=np.float32)
        if self.resampler is not None:
            self.resampler.clear()

    def suspend(self) -> None:
        self.enabled = False
        self.epoch += 1
        self.clear()

    def resume(self) -> None:
        self.epoch += 1
        self.clear()
        self.enabled = True

    async def read(self, timeout: float | None = None) -> np.ndarray:
        try:
            with fail_after(timeout if timeout is not None else self.config.read_timeout_seconds):
                while len(self.pending) < self.config.processing_block_size:
                    samples, overflow = await self.queue.get()
                    if overflow:
                        raise AudioError(
                            "Microphone overflow; check CPU load and audio configuration"
                        )
                    if self.resampler is not None:
                        samples = np.clip(self.resampler.resample_chunk(samples), -1, 1)
                    self.pending = np.concatenate((self.pending, samples))
        except TimeoutError:
            raise AudioError("Microphone stopped producing audio") from None
        samples = self.pending[: self.config.processing_block_size]
        self.pending = self.pending[self.config.processing_block_size :]
        return samples

    async def play(self, samples: np.ndarray) -> None:
        if self.config.output_channels > 1:
            samples = np.repeat(samples[:, None], self.config.output_channels, axis=1)
        self.sd.play(
            samples,
            samplerate=self.config.output_sample_rate,
            device=self.config.output_device,
            latency=self.config.output_latency,
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
