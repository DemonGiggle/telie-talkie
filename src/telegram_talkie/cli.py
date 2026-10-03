from __future__ import annotations

import argparse
import asyncio
import logging
import math
import os
import secrets
import shutil
import signal
import sys
from pathlib import Path

import numpy as np
from anyio import fail_after

from . import __version__
from .app import Talkie, backoff
from .audio import SoundDeviceAudio, tone
from .codec import FFmpegCodec
from .config import MODEL_SAMPLE_RATE, Config, PairingConfig, RetryConfig, load_config
from .models import LocalDetector, check_models, setup_models
from .storage import InstanceLock, Store
from .telegram import Telegram, TelegramError, pairing_ids


class RedactSecrets(logging.Filter):
    def __init__(self, token: str):
        super().__init__()
        self.token = token

    def filter(self, record: logging.LogRecord) -> bool:
        if self.token:
            record.msg = record.getMessage().replace(self.token, "[redacted]")
            record.args = ()
        return True


def configure_logging(token: str, level: str = "INFO") -> None:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    handler.addFilter(RedactSecrets(token))
    logging.basicConfig(level=level, handlers=[handler], force=True)
    # HTTP request logging contains the token as part of the Telegram URL.
    logging.getLogger("httpx").setLevel(logging.CRITICAL)
    logging.getLogger("httpcore").setLevel(logging.CRITICAL)


def require_token(config: Config, *, required: bool = True) -> str:
    settings = config.telegram
    file = os.environ.get(settings.token_file_env, "").strip()
    credentials = os.environ.get("CREDENTIALS_DIRECTORY", "").strip()
    if not file and credentials:
        file = str(Path(credentials) / settings.token_credential)
    if file:
        try:
            with Path(file).open("rb") as source:
                raw = source.read(4097)
            if len(raw) > 4096:
                raise ValueError("Bot token file exceeds 4096 bytes")
            value = raw.decode("utf-8").strip()
        except (OSError, UnicodeError):
            # Filesystem exceptions can include private paths; never log their details.
            raise ValueError(
                "Cannot read bot token file; check its path, access, and encoding"
            ) from None
    else:
        value = os.environ.get(settings.token_env, "").strip()
    if value and (len(value) > 4096 or any(c.isspace() or not c.isprintable() for c in value)):
        raise ValueError("Bot token must be a single nonempty value without whitespace")
    if not value and (required or file):
        raise ValueError(
            f"Provide a bot token with {settings.token_file_env} or {settings.token_env}"
        )
    return value


def print_pairing_settings(chat_id: int, user_id: int, output=print) -> None:
    output("Set this in the [telegram] table in your configuration:")
    output(f"user_id = {user_id}")
    if chat_id != user_id:
        output(f"chat_id = {chat_id}")
    else:
        output("Omit chat_id or leave it at 0 to use your user ID for the private chat.")
    output("Open your bot's private chat and tap Start before running the device.")


async def pair(
    telegram,
    timeout: float | None = None,
    output=print,
    *,
    settings: PairingConfig | None = None,
    retry: RetryConfig | None = None,
) -> tuple[int, int]:
    settings = settings or PairingConfig()
    timeout = timeout if timeout is not None else settings.timeout_seconds
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("Pairing timeout must be positive and finite")
    code = secrets.token_hex(settings.code_bytes)
    output(f"Send this once in a PRIVATE chat to your bot: /pair {code}")
    output(f"Code expires in {int(timeout)} seconds. Keep the runtime stopped while pairing.")
    offset = 0
    attempts = 0
    try:
        with fail_after(timeout):
            while True:
                try:
                    updates = await telegram.updates(offset)
                except TelegramError as error:
                    logging.warning("Pairing poll will retry: %s", error)
                    await asyncio.sleep(backoff(attempts, error.retry_after, retry))
                    attempts += 1
                    continue
                attempts = 0
                for update in sorted(updates, key=lambda x: x["update_id"]):
                    if ids := pairing_ids(update, code):
                        print_pairing_settings(*ids, output)
                        return ids
                    offset = max(offset, update["update_id"] + 1)
    except TimeoutError:
        raise ValueError("Pairing code expired; run pair again for a new code") from None


async def doctor(config: Config, online: bool, audio_check: bool) -> bool:
    ok = True

    def report(name: str, success: bool, detail: str = "") -> None:
        nonlocal ok
        ok = ok and success
        print(f"{'PASS' if success else 'FAIL'} {name}{': ' + detail if detail else ''}")

    try:
        config.require_pairing()
        report("Authorized private chat/user", True)
    except ValueError as error:
        report("Authorized private chat/user", False, str(error))
    token = ""
    try:
        token = require_token(config)
        report("Bot token", True)
    except ValueError as error:
        report("Bot token", False, str(error))
    codec = FFmpegCodec(config=config.codec)
    if shutil.which(config.codec.executable):
        try:
            encoded = await codec.encode(np.zeros(1600, dtype=np.float32))
            # Temporary artifacts live in state, never next to source or config.
            config.storage.directory.mkdir(parents=True, exist_ok=True)
            import tempfile

            with tempfile.TemporaryDirectory(dir=config.storage.directory) as temp:
                path = Path(temp) / "check.ogg"
                path.write_bytes(encoded)
                await codec.decode(path, config.audio.output_sample_rate, 1)
            report("FFmpeg OGG/Opus round trip", True)
        except Exception as error:
            report("FFmpeg OGG/Opus round trip", False, type(error).__name__)
    else:
        report("FFmpeg", False, "Install ffmpeg with libopus support")
    try:
        check_models(config.models)
        with InstanceLock(config.storage.directory):
            LocalDetector(config)
        report("Keyword and Silero VAD model loading", True)
    except Exception as error:
        report("Keyword and Silero VAD model loading", False, type(error).__name__)
    try:
        import sounddevice as sd

        sd.check_input_settings(
            device=config.audio.input_device,
            channels=config.audio.input_channels,
            dtype="float32",
            samplerate=config.audio.input_sample_rate,
        )
        sd.check_output_settings(
            device=config.audio.output_device,
            channels=config.audio.output_channels,
            dtype="float32",
            samplerate=config.audio.output_sample_rate,
        )
        report("PortAudio microphone/speaker settings", True)
    except Exception as error:
        report("PortAudio microphone/speaker settings", False, type(error).__name__)
    if audio_check:
        audio = SoundDeviceAudio(config.audio)
        try:
            with InstanceLock(config.storage.directory):
                await audio.start()
                audio.suspend()
                await audio.play(
                    tone(
                        config.audio.output_sample_rate,
                        config.audio.beep_volume,
                        settings=config.tones,
                    )
                )
                await asyncio.sleep(config.audio.settle_seconds)
                audio.resume()
                seconds = config.runtime.doctor_record_seconds
                print(f"Speak for {seconds:g} seconds; the device will play your recording.")
                frames, size = [], 0
                target = int(seconds * MODEL_SAMPLE_RATE)
                while size < target:
                    chunk = (await audio.read())[: target - size]
                    frames.append(chunk)
                    size += len(chunk)
                audio.suspend()
                recorded = np.concatenate(frames)
                encoded = await codec.encode(recorded)
                import tempfile

                with tempfile.TemporaryDirectory(dir=config.storage.directory) as temp:
                    path = Path(temp) / "check.ogg"
                    path.write_bytes(encoded)
                    decoded = await codec.decode(path, config.audio.output_sample_rate, seconds + 1)
                await audio.play(np.clip(decoded * config.audio.volume, -1, 1))
            report("Physical microphone/speaker loop", True)
        except Exception as error:
            report("Physical microphone/speaker loop", False, type(error).__name__)
        finally:
            await audio.close()
    if online:
        telegram = None
        try:
            telegram = Telegram(
                token or require_token(config), config=config.telegram, network=config.network
            )
            await telegram.call("getMe")
            report("Telegram bot authentication", True)
            webhook = await telegram.call("getWebhookInfo")
            report(
                "Long polling available",
                not bool(webhook.get("url")),
                "Remove the bot's webhook if this check fails",
            )
        except Exception as error:
            report("Telegram connectivity", False, type(error).__name__)
        finally:
            if telegram:
                await telegram.close()
    return ok


async def dispatch(args) -> int:
    if args.command == "pair" and args.user_id is not None:
        if args.user_id <= 0:
            raise ValueError("The Telegram user ID must be positive")
        print_pairing_settings(args.user_id, args.user_id)
        return 0
    if args.command == "devices":
        import sounddevice as sd

        print(sd.query_devices())
        return 0
    config = load_config(args.config)
    token = require_token(config, required=args.command in ("run", "pair"))
    configure_logging(token, config.logging.level)
    if args.command == "setup-models":
        with InstanceLock(config.storage.directory):
            await setup_models(config.models, config.network)
            LocalDetector(config)
        print("Models ready; keyword and VAD loading verified.")
        return 0
    if args.command == "doctor":
        return 0 if await doctor(config, args.online, args.audio_check) else 1
    telegram = Telegram(token, config=config.telegram, network=config.network)
    try:
        with InstanceLock(config.storage.directory):
            if args.command == "pair":
                await pair(telegram, args.timeout, settings=config.pairing, retry=config.retry)
            else:
                config.require_pairing()
                store = Store(config.storage.directory, config.storage.max_audio_bytes)
                try:
                    store.bind(token, config.telegram.resolved_chat_id, config.telegram.user_id)
                    store.recover()
                    detector = LocalDetector(config)
                    await Talkie(
                        config,
                        store,
                        telegram,
                        SoundDeviceAudio(config.audio),
                        detector,
                        FFmpegCodec(config=config.codec),
                    ).run()
                finally:
                    store.close()
    finally:
        await telegram.close()
    return 0


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(description="Telie Talkie voice intercom")
    root.add_argument("--version", action="version", version=__version__)
    root.add_argument("--config", type=Path, default=Path("config.toml"), help="TOML configuration")
    commands = root.add_subparsers(dest="command", required=True)
    commands.add_parser("run", help="Start the voice intercom")
    commands.add_parser("devices", help="List PortAudio input/output devices")
    commands.add_parser("setup-models", help="Download and verify local speech models")
    pairing = commands.add_parser("pair", help="Configure a known user ID or discover it privately")
    pairing_options = pairing.add_mutually_exclusive_group()
    pairing_options.add_argument(
        "--user-id",
        type=int,
        help="Print settings for a known numeric user ID without contacting Telegram",
    )
    pairing_options.add_argument(
        "--timeout", type=float, default=None, help="Override pairing.timeout_seconds"
    )
    check = commands.add_parser("doctor", help="Check dependencies, models, and audio settings")
    check.add_argument("--online", action="store_true", help="Also verify Telegram authentication")
    check.add_argument(
        "--audio-check", action="store_true", help="Beep, record, and play an audio check"
    )
    return root


async def run_command(args) -> int:
    loop = asyncio.get_running_loop()
    task = asyncio.current_task()
    previous_handler = signal.getsignal(signal.SIGTERM)
    loop.add_signal_handler(signal.SIGTERM, task.cancel)
    try:
        return await dispatch(args)
    except asyncio.CancelledError:
        logging.info("Shutdown requested; queued messages will resume on next start")
        return 0
    finally:
        loop.remove_signal_handler(signal.SIGTERM)
        signal.signal(signal.SIGTERM, previous_handler)


def main() -> None:
    args = parser().parse_args()
    try:
        code = asyncio.run(run_command(args))
    except KeyboardInterrupt:
        code = 0
    except (ValueError, TelegramError, RuntimeError) as error:
        # Our runtime errors are safe; never display request exceptions or full tracebacks.
        logging.error("%s", error)
        code = 1
    except Exception as error:
        logging.error(
            "Startup or worker failure (%s); run doctor and check configuration",
            type(error).__name__,
        )
        code = 1
    sys.exit(code)
