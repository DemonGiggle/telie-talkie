from __future__ import annotations

import asyncio
import logging
import re
from contextlib import suppress

import numpy as np
from anyio import CancelScope, fail_after

from .config import MODEL_SAMPLE_RATE, AudioConfig, TonesConfig

log = logging.getLogger(__name__)


class AudioError(Exception):
    pass


def _resolve_device(sd, device: str, direction: str) -> int | str:
    try:
        sd.query_devices(device, kind=direction)
        return device
    except ValueError:
        # Only the ALSA card number is dynamic. Keep the device name, PCM number,
        # direction and host API, and require exactly one matching endpoint.
        pattern = r"(.+?)\s*\(hw:\d+,(\d+)\)(?:,\s*ALSA)?"
        match = re.fullmatch(pattern, device.strip(), flags=re.IGNORECASE)
        if not match or not match[1].strip():
            raise
        candidates = []
        for index, info in enumerate(sd.query_devices()):
            current = re.fullmatch(pattern, info["name"], flags=re.IGNORECASE)
            if (
                current
                and current[1].strip().casefold() == match[1].strip().casefold()
                and current[2] == match[2]
                and info[f"max_{direction}_channels"] > 0
                and sd.query_hostapis(info["hostapi"])["name"] == "ALSA"
            ):
                candidates.append((index, info["name"]))
        if len(candidates) != 1:
            raise ValueError(
                f"Expected one {direction} ALSA device matching {match[1].strip()!r} "
                f"and PCM {match[2]}; found {len(candidates)}"
            ) from None
        index, name = candidates[0]
        log.warning("Resolved stale %s audio device %r as %r", direction, device, name)
        return index


def check_device(sd, config: AudioConfig, direction: str) -> int | str | None:
    """Resolve and validate an endpoint, including an obsolete ALSA card suffix."""
    configured = device = getattr(config, f"{direction}_device")
    try:
        if isinstance(device, str):
            device = _resolve_device(sd, device, direction)
        getattr(sd, f"check_{direction}_settings")(
            device=device,
            channels=getattr(config, f"{direction}_channels"),
            dtype="float32",
            samplerate=getattr(config, f"{direction}_sample_rate"),
        )
    except (sd.PortAudioError, ValueError) as error:
        raise AudioError(
            f"Configured {direction} audio device {configured!r} is unavailable or "
            f"incompatible: {error}"
        ) from error
    return device


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
        await self.close()
        self.loop = asyncio.get_running_loop()
        # Device discovery may block. Keep the Telegram workers running while it probes.
        opening = asyncio.create_task(asyncio.to_thread(self._open))
        try:
            await asyncio.shield(opening)
        except AudioError:
            await self.close()
            raise
        except asyncio.CancelledError:
            # Finish native setup before closing it; never leave an opening thread behind.
            with CancelScope(shield=True):
                with suppress(Exception):
                    await opening
                await self.close()
            raise
        self.resume()

    def _open(self) -> None:
        try:
            import sounddevice as sd
        except (ImportError, OSError) as error:
            raise AudioError(f"Cannot load PortAudio: {error}") from error
        refresh = self.sd is not None
        self.sd = sd
        try:
            if refresh:
                # PortAudio caches devices at initialization. The pinned sounddevice
                # version exposes these hooks; all of this app's streams are closed first.
                if sd._initialized:
                    sd._terminate()
                sd._initialize()
            self.input_device = check_device(sd, self.config, "input")
            self.output_device = check_device(sd, self.config, "output")
            self.input = sd.InputStream(
                device=self.input_device,
                channels=self.config.input_channels,
                samplerate=self.config.input_sample_rate,
                dtype="float32",
                blocksize=self.config.block_size,
                latency=self.config.input_latency,
                callback=self._callback,
            )
            self.input.start()
        except (sd.PortAudioError, ValueError) as error:
            raise AudioError(f"Cannot open configured audio devices: {error}") from error

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
        samples = np.repeat(samples[:, None], self.config.output_channels, axis=1)
        finished = asyncio.Event()
        loop = asyncio.get_running_loop()
        cursor = 0
        underflow = False

        def callback(data, frames, timing, status):
            nonlocal cursor, underflow
            underflow = underflow or bool(status)
            end = min(cursor + frames, len(samples))
            data.fill(0)
            data[: end - cursor] = samples[cursor:end]
            cursor = end
            if cursor == len(samples):
                raise self.sd.CallbackStop

        def finished_callback():
            loop.call_soon_threadsafe(finished.set)

        stream = None
        try:
            stream = self.sd.OutputStream(
                samplerate=self.config.output_sample_rate,
                device=self.output_device,
                channels=self.config.output_channels,
                dtype="float32",
                latency=self.config.output_latency,
                callback=callback,
                finished_callback=finished_callback,
            )
            stream.start()
            latency = self.config.output_latency
            latency_seconds = latency if isinstance(latency, (int, float)) else 0
            timeout = (
                len(samples) / self.config.output_sample_rate
                + latency_seconds
                + self.config.playback_grace_seconds
            )
            with fail_after(timeout):
                await finished.wait()
            if underflow:
                raise AudioError("Speaker output underflow")
        except TimeoutError:
            raise AudioError("Speaker stopped completing playback") from None
        except (self.sd.PortAudioError, ValueError) as error:
            raise AudioError(f"Speaker playback failed: {error}") from error
        finally:
            # This worker owns stream cleanup; no blocking wait thread races with close.
            if stream is not None:
                try:
                    stream.abort()
                finally:
                    stream.close()

    async def close(self) -> None:
        self.suspend()
        if self.input:
            stream, self.input = self.input, None
            try:
                stream.abort()
            finally:
                stream.close()
