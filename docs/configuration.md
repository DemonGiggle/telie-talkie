# Configuration guide

All operational settings are listed, with defaults and comments, in [config.example.toml](../config.example.toml). Copy it to a local configuration file and edit the values for your device. Configuration is read at startup; restart the process or service after changing it. Omitted fields use defaults, so older configuration files remain valid. Unknown fields, incorrect types, non-finite numbers, and invalid combinations fail before the device starts.

The token is loaded from a systemd credential, a token file, or an environment variable; its value is never stored in TOML. Keep your actual configuration, audio, database, model downloads, and logs out of Git.

## Settings by table

| Table | Controls |
| --- | --- |
| `telegram` | Authorized chat/user, token source variable names and credential filename, API base URL, long-poll timeout, incoming size and duration limits |
| `audio` | Devices, native rates, channels, channel selection, gains, hardware/processing blocks, buffers, latency, resampling quality, read timeout, speaker settling |
| `tones` | Ready/error frequencies, duration, repeat counts, and spacing |
| `detection` | Wake phrase, keyword thresholds/boosting, trailing blanks, beam paths, feature dimension, inference provider/device/threads, VAD thresholds/timing/buffer |
| `recording` | Speech wait, finishing silence, maximum duration, pre-roll, and retained trailing silence |
| `codec` | FFmpeg executable, Opus bitrate/rate/application/frame duration/complexity, threads, conversion timeout, output size, read chunks, accepted input containers |
| `network` | Connect/read/write/pool timeouts, connection limits, and download chunk size |
| `retry` | Initial delay, multiplier, cap, jitter, and delay for retained outgoing audio after permanent API errors |
| `runtime` | Queue-check intervals, notification interval, and doctor recording duration |
| `models` | Model directory and individual files, download URLs, archive layout, transfer timeout, and download/extraction limits |
| `pairing` | Code lifetime and random code length; `pair --timeout` overrides the configured lifetime |
| `logging` | DEBUG, INFO, WARNING, ERROR, or CRITICAL; HTTP credential logging remains suppressed |
| `storage` | State directory and combined compressed-audio budget, including partial downloads |

## Bot token sources

The app checks these sources in order:

1. The file path in the variable named by `telegram.token_file_env` (default `TELEGRAM_BOT_TOKEN_FILE`), when nonempty.
2. The file named by `telegram.token_credential` (default `telegram_bot_token`) inside systemd's `CREDENTIALS_DIRECTORY`, when that directory is provided.
3. The value in the variable named by `telegram.token_env` (default `TELEGRAM_BOT_TOKEN`).

An explicitly selected file or credential must be readable and valid; a failure never falls back to another source. Names must be valid distinct environment variable names, and the credential name must be a filename without path separators. A file contains only a single UTF-8 token and an optional final newline, at most 4096 bytes. Empty tokens, embedded whitespace, and control characters are rejected. Token read errors omit paths and contents.

For deployment, use the supplied [systemd service and credential instructions](systemd.md). For other secret-file providers, set `TELEGRAM_BOT_TOKEN_FILE` to the mounted secret's path. The file path is configuration; keep the actual token out of command arguments and TOML. Changes take effect on restart.

## Native microphone rates and channels

For a microphone that exposes 48 kHz stereo, change the existing `[audio]` table:

```toml
[audio]
input_sample_rate = 48000
input_channels = 2
input_channel = 0
block_size = 1536
processing_block_size = 512
buffer_blocks = 128
input_latency = 0.05
output_sample_rate = 48000
output_channels = 2
output_latency = "high"
read_timeout_seconds = 2.0
resample_quality = "HQ"
```

Select the device name/index using `devices`. `input_channel` is zero-based; use `1` for the second channel or `-1` to average all captured channels. Opposite-polarity channels can cancel when averaged, so select a channel when appropriate. Playback duplicates mono audio across `output_channels`.

`block_size` counts frames at the hardware input rate. `processing_block_size` counts samples at the normalized 16 kHz rate. The example uses 32 ms for both. A 44.1 kHz device can use `input_sample_rate = 44100` and `block_size = 1411` or another block size accepted by its driver; hardware and processing blocks do not need an exact integer ratio.

Input is converted to mono, scaled by `input_gain`, and resampled using a stateful SoXR filter. Gain and resampling output are clipped to the normalized sample range. Resampler history and buffered input are cleared after tones/playback. HQ/VHQ use more filtering and can add lookahead latency; MQ/LQ trade some quality for lower cost. QQ uses quick interpolation and offers little protection against aliasing when downsampling.

Use smaller blocks or `"low"` latency if the device supports them reliably. Increase buffering when brief scheduling delays cause overflow. Persistent overflow means processing cannot keep up: larger buffers alone will increase latency. `read_timeout_seconds` covers gathering a processing block, including resampler lookahead.

## Small CPUs and bandwidth

For a slower CPU, adjust the existing tables rather than duplicating them:

```toml
[detection]
num_threads = 1
vad_num_threads = 1
max_active_paths = 4

[codec]
bitrate_bps = 16000
complexity = 3
threads = 1
application = "voip"
```

Thread settings depend on the model and FFmpeg codec; some codecs do not use multiple threads. Lower keyword search capacity or encoding complexity can reduce CPU cost, but verify wake accuracy and recording intelligibility on the device.

Opus rates are 8, 12, 16, 24, or 48 kHz. `codec.sample_rate` controls the encoder input conversion; Opus playback is resampled to your configured speaker rate. Incoming container names use FFmpeg demuxer names, such as `mov` for M4A. Outgoing voice messages retain the OGG/Opus format required by the application.

## Slow networks and longer recordings

```toml
[network]
connect_timeout_seconds = 30.0
read_timeout_seconds = 120.0
write_timeout_seconds = 180.0
pool_timeout_seconds = 30.0

[retry]
initial_seconds = 2.0
multiplier = 2.0
max_seconds = 300.0
jitter_ratio = 0.1
permanent_error_seconds = 600.0

[recording]
max_seconds = 120.0

[codec]
timeout_seconds = 90.0
max_encoded_bytes = 5000000
```

The HTTP read timeout must exceed the Telegram long-poll timeout. Backoff grows exponentially, adds positive jitter, and stays within `retry.max_seconds`. A server-provided `retry_after` takes precedence even if it exceeds that cap. Queue order is preserved while a head item waits.

The previous 60-second recording ceiling is now a default. Longer recording and incoming playback limits are supported. Provide enough RAM for recording and decoded audio, storage for compressed files, conversion time, and an appropriate encoded-file limit. The cap still preserves existing queued files and rejects unstorable new audio.

## Model files and download mirrors

Relative `models.directory` and `storage.directory` resolve beside the TOML file. Individual model file paths resolve inside `models.directory`; absolute paths are also accepted. For an alternative model, set the encoder, decoder, joiner, tokens, BPE, and VAD paths explicitly. Keyword models must be compatible English BPE streaming transducer models with matching tokenization and feature dimensions. The VAD must be compatible with sherpa's Silero wrapper. Selecting files does not convert model architectures.

For `setup-models`, change `kws_archive_url`, `kws_archive_root`, and `vad_url` to your source or mirror. The archive must be tar.bz2 and contain regular files whose basenames match the selected keyword model paths, beneath the configured archive root. Files are validated against the configured size limits before publication; links and unexpected archive paths are not extracted. Model paths can live on a separate filesystem. `download_timeout_seconds` controls model response reads; the shared network settings control connection/write/pool timeouts and chunks.

`provider = "cpu"` is suitable for Raspberry Pi. CUDA/CoreML require corresponding hardware and a compatible sherpa runtime build. `device` selects the keyword detector's CUDA device; the VAD wrapper uses its provider's default device. `vad_num_threads = 0` inherits the keyword thread count. `vad_max_speech_seconds = 0` follows the recording limit plus a one-second margin.

VAD hangover (`vad_min_silence_seconds`) adds detector latency before the recorder's separate silence timer. Keep `pre_roll_seconds` at least as long as `vad_min_speech_seconds`; the loader validates this relationship.

## Protocol and model requirements

Some values describe compatibility requirements rather than device tuning. The Silero wrapper requires 16 kHz mono processing with a 512-sample internal VAD window; selectable capture rates are resampled to this format, and processing blocks are buffered by the detector as needed. Audio samples use float32 internally. Outgoing voice files remain OGG/Opus, playback and recording remain serialized, and runtime authorization requires both configured IDs in a private chat.

The hosted Telegram API retains its download and polling limits. An alternative `telegram.api_base_url` must implement the same Bot API methods and HTTP file-download paths, and may configure a larger download budget. Absolute filesystem paths returned by a server's local mode are not opened by the app. URL credentials are rejected. Pairing codes retain at least 12 random bytes, and credentials are redacted at every selectable log level.

Run `doctor --online --audio-check` after changing your configuration, then complete the hardware acceptance checks in the [setup guide](setup.md#hardware-acceptance).
