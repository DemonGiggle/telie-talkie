from __future__ import annotations

from collections import deque

import numpy as np

from .config import RecordingConfig
from .models import SAMPLE_RATE


class Recording:
    """A sample-clock recorder, fed only with microphone frames after the ready beep."""

    def __init__(self, config: RecordingConfig, sample_rate: int = SAMPLE_RATE):
        self.config = config
        self.sample_rate = sample_rate
        self.elapsed = 0
        self.silence = 0
        self.spoken = False
        self.done = False
        self.frames: list[np.ndarray] = []
        self.pre_roll: deque[np.ndarray] = deque()
        self.pre_roll_size = 0

    def feed(self, samples: np.ndarray, speech: bool) -> bool:
        if self.done:
            return True
        remaining = int(self.config.max_seconds * self.sample_rate) - self.elapsed
        samples = samples[:remaining]
        self.elapsed += len(samples)
        if not self.spoken:
            self.pre_roll.append(samples)
            self.pre_roll_size += len(samples)
            keep = max(len(samples), int(self.config.pre_roll_seconds * self.sample_rate))
            while self.pre_roll_size > keep and len(self.pre_roll) > 1:
                extra = self.pre_roll_size - keep
                first = self.pre_roll.popleft()
                if len(first) > extra:
                    self.pre_roll.appendleft(first[extra:])
                    self.pre_roll_size -= extra
                    break
                self.pre_roll_size -= len(first)
            if speech:
                self.spoken = True
                self.frames.extend(self.pre_roll)
                self.pre_roll.clear()
        else:
            self.frames.append(samples)
        self.silence = 0 if speech else self.silence + len(samples)
        self.done = (
            self.elapsed >= int(self.config.max_seconds * self.sample_rate)
            or (
                not self.spoken
                and self.elapsed >= int(self.config.speech_wait_seconds * self.sample_rate)
            )
            or (self.spoken and self.silence >= int(self.config.silence_seconds * self.sample_rate))
        )
        return self.done

    def result(self) -> np.ndarray | None:
        if not self.spoken:
            return None
        samples = np.concatenate(self.frames)
        trim = max(0, self.silence - int(self.config.tail_seconds * self.sample_rate))
        return samples[: len(samples) - trim] if trim else samples
