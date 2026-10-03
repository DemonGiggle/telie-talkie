from __future__ import annotations

import asyncio
import os
import shutil
import signal
import sys
from dataclasses import replace
from pathlib import Path

import pytest
from anyio import fail_after

from telegram_talkie.cli import configure_logging, require_token
from telegram_talkie.config import Config
from telegram_talkie.storage import Store


@pytest.fixture
def credentials(monkeypatch):
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN", raising=False)
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN_FILE", raising=False)
    monkeypatch.delenv("CREDENTIALS_DIRECTORY", raising=False)
    return Config()


def test_interactive_environment_token_still_works(credentials, monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:fixture-only")
    assert require_token(credentials) == "123:fixture-only"


def test_credential_file_wins_and_redacts_logs(credentials, monkeypatch, tmp_path, capsys):
    import logging

    file = tmp_path / "telegram_bot_token"
    file.write_text("123:file-fixture-only\n")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_FILE", str(file))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:environment-fixture-only")
    token = require_token(credentials)
    assert token == "123:file-fixture-only"
    previous_handlers = logging.getLogger().handlers[:]
    previous_level = logging.getLogger().level
    try:
        configure_logging(token, "DEBUG")
        logging.info("Credential %s", token)
        output = capsys.readouterr().err
        assert token not in output and "[redacted]" in output
    finally:
        logging.getLogger().handlers = previous_handlers
        logging.getLogger().setLevel(previous_level)


@pytest.mark.parametrize(
    "content", [b"", b" \n", b"two\nvalues", b"invalid\x00value", b"\xff", b"x" * 4097]
)
def test_invalid_file_never_falls_back_to_environment(credentials, monkeypatch, tmp_path, content):
    file = tmp_path / "private-token"
    file.write_bytes(content)
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_FILE", str(file))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:fixture-only")
    with pytest.raises(ValueError) as error:
        require_token(credentials, required=False)
    assert str(file) not in str(error.value)
    assert "123:fixture-only" not in str(error.value)


@pytest.mark.parametrize("failure", [FileNotFoundError, PermissionError, IsADirectoryError])
def test_file_read_errors_hide_paths_and_contents(credentials, monkeypatch, failure):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_FILE", "/private/token-location")

    def unreadable(*args, **kwargs):
        raise failure("private path and secret contents")

    monkeypatch.setattr(Path, "open", unreadable)
    with pytest.raises(ValueError, match="Cannot read bot token file") as error:
        require_token(credentials)
    assert "private path" not in str(error.value)
    assert "/private/token-location" not in str(error.value)


def test_custom_token_variable_names(credentials, monkeypatch, tmp_path):
    credentials = replace(
        credentials,
        telegram=replace(credentials.telegram, token_env="BOT_KEY", token_file_env="BOT_KEY_FILE"),
    )
    file = tmp_path / "token"
    file.write_text("123:fixture-only")
    monkeypatch.setenv("BOT_KEY_FILE", str(file))
    assert require_token(credentials) == "123:fixture-only"


def test_named_systemd_credential_and_explicit_file_precedence(credentials, monkeypatch, tmp_path):
    credentials = replace(
        credentials, telegram=replace(credentials.telegram, token_credential="custom_bot_key")
    )
    file = tmp_path / "custom_bot_key"
    file.write_text("123:credential-fixture-only")
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:environment-fixture-only")
    assert require_token(credentials) == "123:credential-fixture-only"
    override = tmp_path / "override"
    override.write_text("123:file-fixture-only")
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN_FILE", str(override))
    assert require_token(credentials) == "123:file-fixture-only"


def test_missing_systemd_credential_never_uses_environment(credentials, monkeypatch, tmp_path):
    monkeypatch.setenv("CREDENTIALS_DIRECTORY", str(tmp_path))
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "123:fixture-only")
    with pytest.raises(ValueError, match="Cannot read bot token file"):
        require_token(credentials)


def test_offline_commands_can_run_without_token(credentials):
    assert require_token(credentials, required=False) == ""
    with pytest.raises(ValueError, match="TELEGRAM_BOT_TOKEN_FILE or TELEGRAM_BOT_TOKEN"):
        require_token(credentials)


@pytest.mark.parametrize("token", ["two values", "two\nvalues", "invalid\x00value"])
def test_invalid_environment_value_is_not_echoed(credentials, monkeypatch, token):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", token.replace("\x00", "\x01"))
    with pytest.raises(ValueError, match="without whitespace") as error:
        require_token(credentials)
    assert token not in str(error.value)


async def test_sigterm_closes_workers_and_preserves_queue(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[telegram]\nuser_id=101\n[storage]\ndirectory="."\nmax_audio_bytes=1024\n'
    )
    file = tmp_path / "bot.token"
    file.write_text("123:fixture-only")
    script = """
import asyncio
import os
from pathlib import Path
from telegram_talkie import cli
from telegram_talkie.storage import Store

directory = Path(os.environ['TELEGRAM_BOT_TOKEN_FILE']).parent

class Telegram:
    def __init__(self, token, **kwargs):
        assert token == '123:fixture-only'

    async def updates(self, offset):
        await asyncio.Future()

    async def send_voice(self, *args):
        await asyncio.Future()

    async def close(self):
        (directory / 'http-closed').touch()

class Audio:
    def __init__(self, config):
        pass

    async def start(self):
        pass

    async def read(self):
        await asyncio.Future()

    async def close(self):
        (directory / 'audio-closed').touch()

class TrackedStore(Store):
    def close(self):
        super().close()
        (directory / 'sqlite-closed').touch()

class Talkie(cli.Talkie):
    async def run(self):
        self.store.enqueue_voice(101, b'queued voice')
        (directory / 'ready').touch()
        await super().run()

cli.Telegram = Telegram
cli.SoundDeviceAudio = Audio
cli.LocalDetector = lambda config: None
cli.Store = TrackedStore
cli.Talkie = Talkie
cli.main()
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        "--config",
        str(tmp_path / "config.toml"),
        "run",
        env={**os.environ, "TELEGRAM_BOT_TOKEN_FILE": str(file)},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        with fail_after(10):
            while not (tmp_path / "ready").exists():
                if process.returncode is not None:
                    pytest.fail("Worker exited before becoming ready")
                await asyncio.sleep(0.01)
            process.send_signal(signal.SIGTERM)
            _, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode()
        assert all(
            (tmp_path / name).exists() for name in ("audio-closed", "http-closed", "sqlite-closed")
        )
        assert b"Traceback" not in stderr
        store = Store(tmp_path, 1024)
        try:
            store.recover()
            row = store.head("outbox", ("pending",))
            assert row["chat_id"] == 101
            assert store.path(row["path"]).read_bytes() == b"queued voice"
        finally:
            store.close()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="systemd-analyze is not installed")
async def test_supplied_unit_validates_in_isolated_installation(tmp_path):
    units = tmp_path / "etc/systemd/system"
    units.mkdir(parents=True)
    shutil.copyfile(
        Path(__file__).parents[1] / "deploy/telie-talkie.service", units / "telie-talkie.service"
    )
    for name in (
        "basic.target",
        "sysinit.target",
        "shutdown.target",
        "network-online.target",
        "sound.target",
    ):
        (units / name).write_text("[Unit]\nDefaultDependencies=no\n")
    # Verification checks the install path exists. Runtime behavior is exercised separately.
    executable = tmp_path / "opt/telie-talkie/.venv/bin/telie-talkie"
    executable.parent.mkdir(parents=True)
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    process = await asyncio.create_subprocess_exec(
        "systemd-analyze",
        f"--root={tmp_path}",
        "verify",
        "/etc/systemd/system/telie-talkie.service",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    with fail_after(10):
        _, stderr = await process.communicate()
    assert process.returncode == 0, stderr.decode()


@pytest.mark.skipif(
    os.environ.get("TELIE_TEST_SYSTEMD") != "1",
    reason="Opt-in test requires a user systemd manager",
)
async def test_systemd_loads_credential_without_token_environment(tmp_path):
    file = tmp_path / "bot.token"
    file.write_text("123:systemd-fixture-only\n")
    file.chmod(0o600)
    script = """
import os
from pathlib import Path
from telegram_talkie.cli import require_token
from telegram_talkie.config import Config

assert 'TELEGRAM_BOT_TOKEN' not in os.environ
assert 'TELEGRAM_BOT_TOKEN_FILE' not in os.environ
assert (Path(os.environ['CREDENTIALS_DIRECTORY']) / 'telegram_bot_token').is_file()
assert require_token(Config()) == '123:systemd-fixture-only'
print('Systemd credential loading passed')
"""
    process = await asyncio.create_subprocess_exec(
        "systemd-run",
        "--user",
        "--wait",
        "--pipe",
        "--collect",
        "--property=Type=exec",
        f"--property=LoadCredential=telegram_bot_token:{file}",
        "--property=UnsetEnvironment=TELEGRAM_BOT_TOKEN TELEGRAM_BOT_TOKEN_FILE",
        sys.executable,
        "-c",
        script,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        with fail_after(20):
            stdout, stderr = await process.communicate()
        assert process.returncode == 0, stderr.decode()
        assert b"Systemd credential loading passed" in stdout
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
