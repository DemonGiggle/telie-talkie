from __future__ import annotations

import math
import tomllib
from dataclasses import dataclass, fields
from pathlib import Path


@dataclass(frozen=True)
class TelegramConfig:
    chat_id: int = 0
    user_id: int = 0
    token_env: str = "TELEGRAM_BOT_TOKEN"
    poll_timeout: int = 30
    max_download_bytes: int = 20_000_000
    max_incoming_seconds: float = 300.0


@dataclass(frozen=True)
class AudioConfig:
    input_device: int | str | None = None
    output_device: int | str | None = None
    output_sample_rate: int = 48000
    volume: float = 0.8
    beep_volume: float = 0.2
    settle_seconds: float = 0.2


@dataclass(frozen=True)
class DetectionConfig:
    wake_phrase: str = "HELLO KITTY"
    keywords_score: float = 1.5
    keywords_threshold: float = 0.25
    num_trailing_blanks: int = 1
    vad_threshold: float = 0.5
    vad_min_speech_seconds: float = 0.128
    num_threads: int = 2


@dataclass(frozen=True)
class RecordingConfig:
    speech_wait_seconds: float = 5.0
    silence_seconds: float = 1.5
    max_seconds: float = 60.0
    pre_roll_seconds: float = 0.256
    tail_seconds: float = 0.2


@dataclass(frozen=True)
class StorageConfig:
    directory: Path = Path("state")
    max_audio_bytes: int = 512 * 1024 * 1024


@dataclass(frozen=True)
class ModelsConfig:
    directory: Path = Path("models")


@dataclass(frozen=True)
class Config:
    telegram: TelegramConfig = TelegramConfig()
    audio: AudioConfig = AudioConfig()
    detection: DetectionConfig = DetectionConfig()
    recording: RecordingConfig = RecordingConfig()
    storage: StorageConfig = StorageConfig()
    models: ModelsConfig = ModelsConfig()

    def require_pairing(self) -> None:
        if self.telegram.chat_id <= 0 or self.telegram.user_id <= 0:
            raise ValueError("Set positive private chat_id and user_id using the pair command")


def _table(cls, values: dict, base: Path):
    if not isinstance(values, dict):
        raise ValueError(f"{cls.__name__} must be a TOML table")
    unknown = set(values) - {f.name for f in fields(cls)}
    if unknown:
        raise ValueError(f"Unknown {cls.__name__} settings: {', '.join(sorted(unknown))}")
    defaults = cls()
    converted = dict(values)
    for key, value in values.items():
        default = getattr(defaults, key)
        if key.endswith("_device"):
            valid = type(value) in (int, str) and (not isinstance(value, str) or bool(value))
        elif isinstance(default, Path):
            valid = isinstance(value, str) and bool(value)
        elif type(default) is float:
            valid = type(value) in (int, float) and math.isfinite(value)
        else:
            valid = type(value) is type(default)
        if not valid:
            raise ValueError(f"Invalid type or value for {cls.__name__}.{key}")
    if hasattr(defaults, "directory"):
        path = Path(converted.get("directory", defaults.directory)).expanduser()
        converted["directory"] = (base / path).resolve()
    return cls(**converted)


def load_config(path: Path) -> Config:
    with path.open("rb") as file:
        raw = tomllib.load(file)
    tables = {
        "telegram": TelegramConfig,
        "audio": AudioConfig,
        "detection": DetectionConfig,
        "recording": RecordingConfig,
        "storage": StorageConfig,
        "models": ModelsConfig,
    }
    if set(raw) - tables.keys():
        raise ValueError("Unknown configuration table")
    config = Config(
        **{k: _table(cls, raw.get(k, {}), path.resolve().parent) for k, cls in tables.items()}
    )
    t, a, d, r, s = (
        config.telegram,
        config.audio,
        config.detection,
        config.recording,
        config.storage,
    )
    if not (0 <= t.chat_id and 0 <= t.user_id and t.token_env):
        raise ValueError("Use private chat/user IDs and a nonempty token_env")
    if not 1 <= t.poll_timeout <= 50:
        raise ValueError("poll_timeout must be between 1 and 50")
    if not 1 <= t.max_download_bytes <= 20_000_000 or s.max_audio_bytes < 1:
        raise ValueError("Storage must be positive; downloads cannot exceed 20,000,000 bytes")
    if not 0 < t.max_incoming_seconds <= 3600:
        raise ValueError("max_incoming_seconds must be between 0 and 3600")
    if not 8000 <= a.output_sample_rate <= 96000:
        raise ValueError("Unsupported output_sample_rate")
    if not all(0 <= x <= 1 for x in (a.volume, a.beep_volume, d.vad_threshold)):
        raise ValueError("Volume and VAD threshold must be between 0 and 1")
    if not (
        0 < d.keywords_threshold <= 1
        and d.keywords_score > 0
        and 1 <= d.num_threads <= 16
        and d.num_trailing_blanks >= 1
    ):
        raise ValueError("Invalid keyword detection settings")
    if not (
        d.wake_phrase.strip()
        and all(c.isascii() and (c.isalpha() or c == " ") for c in d.wake_phrase)
    ):
        raise ValueError("wake_phrase must contain English letters and spaces")
    if not (0 <= a.settle_seconds <= 5 and 0 < d.vad_min_speech_seconds <= 1):
        raise ValueError("Invalid settle or VAD speech duration")
    if not (
        0 < r.speech_wait_seconds <= r.max_seconds <= 60
        and 0 < r.silence_seconds <= r.max_seconds
        and d.vad_min_speech_seconds <= r.pre_roll_seconds <= 1
        and 0 <= r.tail_seconds <= r.silence_seconds
    ):
        raise ValueError("Invalid recording timing; maximum recording is 60 seconds")
    return config
