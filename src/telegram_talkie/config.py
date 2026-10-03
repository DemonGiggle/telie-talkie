from __future__ import annotations

import math
import re
from dataclasses import dataclass, fields
from pathlib import Path
from urllib.parse import urlsplit

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10 uses the maintained TOML parser backport.
    import tomli as tomllib

MODEL_SAMPLE_RATE = 16000
DEFAULT_MODEL_NAME = "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"


@dataclass(frozen=True)
class TelegramConfig:
    chat_id: int = 0
    user_id: int = 0
    token_env: str = "TELEGRAM_BOT_TOKEN"
    token_file_env: str = "TELEGRAM_BOT_TOKEN_FILE"
    token_credential: str = "telegram_bot_token"
    poll_timeout: int = 30
    max_download_bytes: int = 20_000_000
    max_incoming_seconds: float = 300.0
    api_base_url: str = "https://api.telegram.org"


@dataclass(frozen=True)
class NetworkConfig:
    connect_timeout_seconds: float = 15.0
    read_timeout_seconds: float = 90.0
    write_timeout_seconds: float = 90.0
    pool_timeout_seconds: float = 90.0
    max_connections: int = 20
    max_keepalive_connections: int = 10
    chunk_bytes: int = 65536


@dataclass(frozen=True)
class RetryConfig:
    initial_seconds: float = 1.0
    multiplier: float = 2.0
    max_seconds: float = 300.0
    jitter_ratio: float = 0.25
    permanent_error_seconds: float = 300.0


@dataclass(frozen=True)
class RuntimeConfig:
    queue_interval_seconds: float = 0.25
    notice_interval_seconds: float = 0.5
    doctor_record_seconds: float = 3.0


@dataclass(frozen=True)
class PairingConfig:
    timeout_seconds: float = 300.0
    code_bytes: int = 12


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"


@dataclass(frozen=True)
class AudioConfig:
    input_device: int | str | None = None
    output_device: int | str | None = None
    output_sample_rate: int = 48000
    input_sample_rate: int = 16000
    input_channels: int = 1
    input_channel: int = 0
    output_channels: int = 1
    block_size: int = 512
    processing_block_size: int = 512
    buffer_blocks: int = 64
    read_timeout_seconds: float = 1.0
    input_latency: str | float = "high"
    output_latency: str | float = "high"
    resample_quality: str = "HQ"
    input_gain: float = 1.0
    volume: float = 0.8
    beep_volume: float = 0.2
    settle_seconds: float = 0.2


@dataclass(frozen=True)
class TonesConfig:
    ready_frequency_hz: float = 1000.0
    error_frequency_hz: float = 440.0
    duration_seconds: float = 0.12
    ready_repeats: int = 1
    error_repeats: int = 2
    gap_seconds: float = 0.1


@dataclass(frozen=True)
class CodecConfig:
    executable: str = "ffmpeg"
    bitrate_bps: int = 24000
    sample_rate: int = 16000
    application: str = "voip"
    frame_duration_ms: float = 20.0
    complexity: int = 10
    threads: int = 0
    timeout_seconds: float = 45.0
    max_encoded_bytes: int = 2_000_000
    chunk_bytes: int = 65536
    allowed_input_formats: tuple[str, ...] = (
        "ogg",
        "mp3",
        "mov",
        "matroska",
        "webm",
        "wav",
        "flac",
        "aac",
        "amr",
    )


@dataclass(frozen=True)
class DetectionConfig:
    wake_phrase: str = "HELLO KITTY"
    keywords_score: float = 1.5
    keywords_threshold: float = 0.25
    num_trailing_blanks: int = 1
    vad_threshold: float = 0.5
    vad_min_speech_seconds: float = 0.128
    num_threads: int = 2
    vad_num_threads: int = 0
    provider: str = "cpu"
    device: int = 0
    feature_dim: int = 80
    max_active_paths: int = 4
    vad_min_silence_seconds: float = 0.032
    vad_max_speech_seconds: float = 0.0
    vad_buffer_seconds: float = 65.0


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
    encoder: str = "encoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
    decoder: str = "decoder-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
    joiner: str = "joiner-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
    tokens: str = "tokens.txt"
    bpe_model: str = "bpe.model"
    vad: str = "silero_vad.onnx"
    kws_archive_url: str = (
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/"
        f"{DEFAULT_MODEL_NAME}.tar.bz2"
    )
    kws_archive_root: str = DEFAULT_MODEL_NAME
    vad_url: str = (
        "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx"
    )
    download_timeout_seconds: float = 120.0
    max_archive_bytes: int = 100_000_000
    max_vad_bytes: int = 20_000_000
    max_model_file_bytes: int = 30_000_000

    def path(self, field: str) -> Path:
        return self.directory / Path(getattr(self, field)).expanduser()


@dataclass(frozen=True)
class Config:
    telegram: TelegramConfig = TelegramConfig()
    audio: AudioConfig = AudioConfig()
    detection: DetectionConfig = DetectionConfig()
    recording: RecordingConfig = RecordingConfig()
    storage: StorageConfig = StorageConfig()
    models: ModelsConfig = ModelsConfig()
    network: NetworkConfig = NetworkConfig()
    retry: RetryConfig = RetryConfig()
    runtime: RuntimeConfig = RuntimeConfig()
    pairing: PairingConfig = PairingConfig()
    logging: LoggingConfig = LoggingConfig()
    tones: TonesConfig = TonesConfig()
    codec: CodecConfig = CodecConfig()

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
        elif key.endswith("_latency"):
            valid = (
                value in ("low", "high")
                if isinstance(value, str)
                else type(value) in (int, float) and math.isfinite(value) and value > 0
            )
        elif isinstance(default, tuple):
            valid = (
                isinstance(value, list)
                and bool(value)
                and all(
                    isinstance(item, str) and bool(re.fullmatch(r"[a-z0-9_]+", item))
                    for item in value
                )
            )
            if valid:
                converted[key] = tuple(value)
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
        "network": NetworkConfig,
        "retry": RetryConfig,
        "runtime": RuntimeConfig,
        "pairing": PairingConfig,
        "logging": LoggingConfig,
        "tones": TonesConfig,
        "codec": CodecConfig,
    }
    if set(raw) - tables.keys():
        raise ValueError("Unknown configuration table")
    config = Config(
        **{k: _table(cls, raw.get(k, {}), path.resolve().parent) for k, cls in tables.items()}
    )
    validate_config(config)
    return config


def _positive(**values) -> None:
    for name, value in values.items():
        if value <= 0 or not math.isfinite(value):
            raise ValueError(f"{name} must be positive and finite")


def _url(value: str, name: str) -> None:
    parsed = urlsplit(value)
    if (
        parsed.scheme not in ("http", "https")
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(f"{name} must be an HTTP(S) URL without credentials, query, or fragment")


def validate_config(config: Config) -> None:
    t, a, d, r, s = (
        config.telegram,
        config.audio,
        config.detection,
        config.recording,
        config.storage,
    )
    n, retry, runtime, pairing, tones, codec, models = (
        config.network,
        config.retry,
        config.runtime,
        config.pairing,
        config.tones,
        config.codec,
        config.models,
    )
    if not (0 <= t.chat_id and 0 <= t.user_id):
        raise ValueError("Use nonnegative private chat/user IDs")
    if (
        not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", t.token_env)
        or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", t.token_file_env)
        or t.token_env == t.token_file_env
    ):
        raise ValueError("token_env and token_file_env must be distinct environment variable names")
    if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.-]*", t.token_credential):
        raise ValueError("token_credential must be a credential filename without path separators")
    if not 1 <= t.poll_timeout <= 50:
        raise ValueError("poll_timeout must be between 1 and 50")
    _url(t.api_base_url, "telegram.api_base_url")
    if (
        urlsplit(t.api_base_url).hostname == "api.telegram.org"
        and t.max_download_bytes > 20_000_000
    ):
        raise ValueError("The hosted Telegram API limits downloads to 20,000,000 bytes")
    _positive(
        max_download_bytes=t.max_download_bytes,
        max_audio_bytes=s.max_audio_bytes,
        max_incoming_seconds=t.max_incoming_seconds,
        input_sample_rate=a.input_sample_rate,
        output_sample_rate=a.output_sample_rate,
        block_size=a.block_size,
        buffer_blocks=a.buffer_blocks,
        processing_block_size=a.processing_block_size,
        read_timeout_seconds=a.read_timeout_seconds,
    )
    if (
        a.input_channels < 1
        or a.output_channels < 1
        or not -1 <= a.input_channel < a.input_channels
        or a.input_gain < 0
    ):
        raise ValueError("Invalid audio channel selection or input gain")
    if a.resample_quality not in ("QQ", "LQ", "MQ", "HQ", "VHQ"):
        raise ValueError("resample_quality must be QQ, LQ, MQ, HQ, or VHQ")
    if not all(0 <= x <= 1 for x in (a.volume, a.beep_volume, d.vad_threshold)):
        raise ValueError("Volume and VAD threshold must be between 0 and 1")
    if not (
        0 < d.keywords_threshold <= 1
        and d.keywords_score > 0
        and d.num_threads >= 1
        and d.num_trailing_blanks >= 1
        and d.vad_num_threads >= 0
        and d.feature_dim > 0
        and d.max_active_paths > 0
        and d.device >= 0
    ):
        raise ValueError("Invalid keyword detection settings")
    if not (
        d.wake_phrase.strip()
        and all(c.isascii() and (c.isalpha() or c == " ") for c in d.wake_phrase)
    ):
        raise ValueError("wake_phrase must contain English letters and spaces")
    if not (
        a.settle_seconds >= 0
        and d.vad_min_speech_seconds > 0
        and d.vad_min_silence_seconds > 0
        and d.vad_max_speech_seconds >= 0
        and d.vad_buffer_seconds > 0
        and d.provider in ("cpu", "cuda", "coreml")
    ):
        raise ValueError("Invalid settle or VAD speech duration")
    if not (
        0 < r.speech_wait_seconds <= r.max_seconds
        and 0 < r.silence_seconds <= r.max_seconds
        and r.pre_roll_seconds >= d.vad_min_speech_seconds
        and 0 <= r.tail_seconds <= r.silence_seconds
    ):
        raise ValueError("Invalid recording timing or insufficient pre-roll for VAD confirmation")
    _positive(
        connect_timeout_seconds=n.connect_timeout_seconds,
        read_timeout_seconds=n.read_timeout_seconds,
        write_timeout_seconds=n.write_timeout_seconds,
        pool_timeout_seconds=n.pool_timeout_seconds,
        chunk_bytes=n.chunk_bytes,
        max_connections=n.max_connections,
    )
    if n.read_timeout_seconds <= t.poll_timeout:
        raise ValueError("network.read_timeout_seconds must exceed telegram.poll_timeout")
    if not 0 <= n.max_keepalive_connections <= n.max_connections:
        raise ValueError("max_keepalive_connections cannot exceed max_connections")
    _positive(
        initial_seconds=retry.initial_seconds,
        max_seconds=retry.max_seconds,
        permanent_error_seconds=retry.permanent_error_seconds,
    )
    if (
        retry.max_seconds < retry.initial_seconds
        or retry.multiplier < 1
        or not 0 <= retry.jitter_ratio <= 1
    ):
        raise ValueError("Invalid retry limits, multiplier, or jitter")
    _positive(
        queue_interval_seconds=runtime.queue_interval_seconds,
        notice_interval_seconds=runtime.notice_interval_seconds,
        doctor_record_seconds=runtime.doctor_record_seconds,
        pairing_timeout_seconds=pairing.timeout_seconds,
    )
    if not 12 <= pairing.code_bytes <= 64:
        raise ValueError("pairing.code_bytes must be between 12 and 64")
    if config.logging.level not in ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"):
        raise ValueError("Invalid logging.level")
    _positive(
        ready_frequency_hz=tones.ready_frequency_hz,
        error_frequency_hz=tones.error_frequency_hz,
        duration_seconds=tones.duration_seconds,
    )
    if (
        max(tones.ready_frequency_hz, tones.error_frequency_hz) >= a.output_sample_rate / 2
        or tones.gap_seconds < 0
        or tones.ready_repeats < 1
        or tones.error_repeats < 1
        or int(tones.duration_seconds * a.output_sample_rate) < 1
    ):
        raise ValueError("Invalid tone frequency, gap, or repeat count")
    if (
        not codec.executable
        or not 6000 <= codec.bitrate_bps <= 510000
        or codec.sample_rate not in (8000, 12000, 16000, 24000, 48000)
        or codec.application not in ("voip", "audio", "lowdelay")
        or codec.frame_duration_ms not in (2.5, 5, 10, 20, 40, 60, 80, 100, 120)
        or not 0 <= codec.complexity <= 10
        or codec.threads < 0
    ):
        raise ValueError("Invalid Opus encoding settings")
    _positive(
        codec_timeout_seconds=codec.timeout_seconds,
        codec_chunk_bytes=codec.chunk_bytes,
        max_encoded_bytes=codec.max_encoded_bytes,
    )
    if codec.max_encoded_bytes > 50_000_000:
        raise ValueError("Encoded voice messages cannot exceed 50,000,000 bytes")
    if not codec.allowed_input_formats:
        raise ValueError("At least one allowed_input_format is required")
    for field in ("encoder", "decoder", "joiner", "tokens", "bpe_model", "vad"):
        if not getattr(models, field).strip():
            raise ValueError(f"models.{field} cannot be empty")
    _url(models.kws_archive_url, "models.kws_archive_url")
    _url(models.vad_url, "models.vad_url")
    root = Path(models.kws_archive_root)
    if root.is_absolute() or ".." in root.parts:
        raise ValueError("kws_archive_root must be a relative archive path")
    _positive(
        download_timeout_seconds=models.download_timeout_seconds,
        max_archive_bytes=models.max_archive_bytes,
        max_vad_bytes=models.max_vad_bytes,
        max_model_file_bytes=models.max_model_file_bytes,
    )
