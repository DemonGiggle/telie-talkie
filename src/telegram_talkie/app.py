from __future__ import annotations

import asyncio
import logging
import math
import random
import time

import numpy as np
from anyio import create_task_group

from .audio import tone
from .codec import CodecError
from .config import Config, RetryConfig
from .recording import Recording
from .storage import StorageFull, Store
from .telegram import IncomingRejected, TelegramError

log = logging.getLogger(__name__)


def backoff(attempts: int, retry_after: float = 0, settings: RetryConfig | None = None) -> float:
    settings = settings or RetryConfig()
    log_delay = min(
        math.log(settings.max_seconds),
        math.log(settings.initial_seconds) + max(0, attempts) * math.log(settings.multiplier),
    )
    delay = min(
        settings.max_seconds, math.exp(log_delay) * random.uniform(1, 1 + settings.jitter_ratio)
    )
    return max(retry_after, delay)


class Talkie:
    def __init__(self, config: Config, store: Store, telegram, audio, detector, codec):
        self.config = config
        self.store = store
        self.telegram = telegram
        self.audio = audio
        self.detector = detector
        self.codec = codec

    async def run(self) -> None:
        try:
            await self.audio.start()
            async with create_task_group() as group:
                group.start_soon(self.poll_loop)
                group.start_soon(self.prepare_loop)
                group.start_soon(self.send_loop)
                group.start_soon(self.notice_loop)
                group.start_soon(self.audio_loop)
        finally:
            await self.audio.close()

    async def poll_once(self) -> None:
        updates = await self.telegram.updates(self.store.offset)
        t = self.config.telegram
        self.store.ingest(
            updates, t.resolved_chat_id, t.user_id, t.max_download_bytes, t.max_incoming_seconds
        )

    async def poll_loop(self) -> None:
        attempts = 0
        while True:
            try:
                await self.poll_once()
                attempts = 0
            except TelegramError as error:
                log.warning("Polling will retry: %s", error)
                await asyncio.sleep(backoff(attempts, error.retry_after, self.config.retry))
                attempts += 1

    async def prepare_once(self) -> bool:
        row = self.store.head("inbox", ("pending", "downloading"))
        if row is None or row["next_attempt"] > time.time():
            return False
        self.store.state("inbox", row["id"], "downloading")
        path = None
        try:
            path = self.store.stage()
            async for chunk in self.telegram.download(
                row["file_id"], self.config.telegram.max_download_bytes
            ):
                self.store.append(path, chunk)
            # Validate the entire bounded decode before placing it on the playback queue.
            await self.codec.decode(
                path,
                self.config.audio.output_sample_rate,
                self.config.telegram.max_incoming_seconds,
            )
            name = self.store.commit_file(path, ".media")
            self.store.ready(row["id"], name)
            log.info("Incoming audio queued (update %s)", row["update_id"])
        except (StorageFull, CodecError, IncomingRejected) as error:
            self.store.finish("inbox", row, str(error))
            log.warning("Incoming audio rejected: %s", error)
        except TelegramError as error:
            if error.retryable:
                self.store.retry(
                    "inbox",
                    row,
                    backoff(row["attempts"], error.retry_after, self.config.retry),
                    str(error),
                )
            else:
                self.store.finish("inbox", row, "Telegram cannot retrieve this file")
            log.warning("Download failed: %s", error)
        finally:
            if path is not None:
                path.unlink(missing_ok=True)
        return True

    async def prepare_loop(self) -> None:
        while True:
            if not await self.prepare_once():
                await asyncio.sleep(self.config.runtime.queue_interval_seconds)

    async def send_once(self) -> bool:
        row = self.store.head("outbox", ("pending", "sending"))
        if row is None or row["next_attempt"] > time.time():
            return False
        self.store.state("outbox", row["id"], "sending")
        try:
            await self.telegram.send_voice(row["chat_id"], self.store.path(row["path"]))
        except TelegramError as error:
            # Even permanent send errors preserve audio for a configuration fix and retry.
            delay = (
                backoff(row["attempts"], error.retry_after, self.config.retry)
                if error.retryable
                else self.config.retry.permanent_error_seconds
            )
            self.store.retry("outbox", row, delay, str(error))
            log.warning("Voice upload retained for retry: %s", error)
        else:
            self.store.finish("outbox", row)
            log.info("Outgoing voice delivered (queue %s)", row["id"])
        return True

    async def send_loop(self) -> None:
        while True:
            if not await self.send_once():
                await asyncio.sleep(self.config.runtime.queue_interval_seconds)

    async def notice_once(self) -> bool:
        row = self.store.head_notice()
        if row is None or row["next_attempt"] > time.time():
            return False
        try:
            await self.telegram.send_text(row["chat_id"], row["text"])
        except TelegramError as error:
            self.store.retry(
                "notices", row, backoff(row["attempts"], error.retry_after, self.config.retry), ""
            )
            log.warning("Error notification will retry: %s", error)
        else:
            self.store.finish("notices", row)
        return True

    async def notice_loop(self) -> None:
        while True:
            if not await self.notice_once():
                await asyncio.sleep(self.config.runtime.notice_interval_seconds)

    async def _resume(self) -> None:
        # Keep callbacks gated through speaker reverberation and detector reset.
        await asyncio.sleep(self.config.audio.settle_seconds)
        self.detector.reset()
        self.audio.resume()

    async def playback_once(self) -> bool:
        # Pending older audio blocks newer audio from overtaking it, including on retry.
        row = self.store.head("inbox", ("pending", "downloading", "ready", "playing"))
        if row is None or row["state"] != "ready":
            return False
        self.audio.suspend()
        self.store.state("inbox", row["id"], "playing")
        try:
            samples = await self.codec.decode(
                self.store.path(row["path"]),
                self.config.audio.output_sample_rate,
                self.config.telegram.max_incoming_seconds,
            )
            samples = np.clip(samples * self.config.audio.volume, -1, 1).astype(np.float32)
            await self.audio.play(samples)
        except CodecError as error:
            self.store.finish("inbox", row, str(error))
            log.warning("Playback audio rejected: %s", error)
        else:
            self.store.finish("inbox", row)
            log.info("Incoming audio played (update %s)", row["update_id"])
        finally:
            await self._resume()
        return True

    async def record_once(self) -> None:
        self.audio.suspend()
        try:
            await self.audio.play(
                tone(
                    self.config.audio.output_sample_rate,
                    self.config.audio.beep_volume,
                    settings=self.config.tones,
                )
            )
        finally:
            await self._resume()
        recording = Recording(self.config.recording)
        # Wall-clock limit also protects against a very slow microphone or detector.
        clock = asyncio.get_running_loop()
        started = clock.time()
        deadline = started + self.config.recording.max_seconds
        while not recording.done:
            now = clock.time()
            if now >= deadline or (
                not recording.spoken and now - started >= self.config.recording.speech_wait_seconds
            ):
                break
            samples = await self.audio.read()
            recording.feed(samples, await self.detector.speech(samples))
        self.audio.suspend()
        try:
            samples = recording.result()
            if samples is None:
                log.info("Recording discarded: no speech")
                return
            try:
                encoded = await self.codec.encode(samples)
                row_id = self.store.enqueue_voice(self.config.telegram.resolved_chat_id, encoded)
                log.info("Recording queued (queue %s)", row_id)
            except (StorageFull, CodecError) as error:
                log.warning("Recording could not be queued: %s", error)
                self.store.notice(
                    self.config.telegram.resolved_chat_id,
                    f"The device could not store a new recording: {error}.",
                )
                await self.audio.play(
                    tone(
                        self.config.audio.output_sample_rate,
                        self.config.audio.beep_volume,
                        error=True,
                        settings=self.config.tones,
                    )
                )
        finally:
            await self._resume()

    async def audio_loop(self) -> None:
        log.info("Listening for wake phrase")
        while True:
            if await self.playback_once():
                continue
            samples = await self.audio.read()
            if await self.detector.wake(samples):
                log.info("Wake phrase detected")
                await self.record_once()
