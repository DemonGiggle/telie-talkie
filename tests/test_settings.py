import json
from dataclasses import replace

import httpx
import numpy as np
import pytest

from telegram_talkie.app import backoff
from telegram_talkie.cli import configure_logging, pair
from telegram_talkie.codec import CodecError, FFmpegCodec
from telegram_talkie.config import CodecConfig, NetworkConfig, PairingConfig, RetryConfig
from telegram_talkie.telegram import Telegram, TelegramError


def test_retry_profile_caps_jitter_and_respects_server_delay():
    profile = RetryConfig(initial_seconds=4, multiplier=3, max_seconds=100, jitter_ratio=0)
    assert backoff(0, settings=profile) == pytest.approx(4)
    assert backoff(1, settings=profile) == pytest.approx(12)
    assert backoff(2, settings=profile) == pytest.approx(36)
    assert backoff(100000, settings=profile) == pytest.approx(100)
    assert backoff(100000, retry_after=200, settings=profile) == 200
    jittered = replace(profile, jitter_ratio=1)
    assert all(backoff(1000, settings=jittered) <= 100 for _ in range(20))


async def test_permanent_upload_retry_uses_configured_delay_and_keeps_audio(rig):
    import time

    rig.config = replace(rig.config, retry=RetryConfig(permanent_error_seconds=40))
    rig.store.enqueue_voice(101, b"keep this audio")
    rig.telegram.failure = TelegramError("sendVoice", 403)
    before = time.time()
    await rig.send_once()
    row = rig.store.head("outbox", ("pending",))
    assert before + 40 <= row["next_attempt"] <= time.time() + 40
    assert rig.store.path(row["path"]).read_bytes() == b"keep this audio"


async def test_configured_network_endpoint_timeouts_and_download_chunks(rig):
    requests = []

    async def handle(request):
        requests.append(request)
        if request.url.path.endswith("getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "audio/test"}})
        if request.url.path.endswith("getUpdates"):
            return httpx.Response(200, json={"ok": True, "result": []})
        return httpx.Response(200, content=b"1234567")

    settings = replace(
        rig.config.telegram, api_base_url="https://example.invalid/api", poll_timeout=4
    )
    network = NetworkConfig(
        connect_timeout_seconds=2,
        read_timeout_seconds=12,
        write_timeout_seconds=8,
        pool_timeout_seconds=3,
        max_connections=5,
        max_keepalive_connections=2,
        chunk_bytes=3,
    )
    # The production constructor supplies HTTPX's timeouts and connection limits.
    bot = Telegram("123:fixture-only", config=settings, network=network)
    assert bot.client.timeout.connect == 2 and bot.client.timeout.read == 12
    assert bot.client.timeout.write == 8 and bot.client.timeout.pool == 3
    await bot.close()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        bot = Telegram("123:fixture-only", client=client, config=settings, network=network)
        await bot.updates(9)
        chunks = [chunk async for chunk in bot.download("file", 20)]
    assert list(map(len, chunks)) == [3, 3, 1]
    assert json.loads(requests[0].content)["timeout"] == 4
    assert all(str(request.url).startswith("https://example.invalid/api/") for request in requests)


async def test_configured_pairing_timeout_and_code_length(monkeypatch):
    from .conftest import FakeTelegram

    sizes = []
    monkeypatch.setattr(
        "telegram_talkie.cli.secrets.token_hex", lambda size: sizes.append(size) or "test-code"
    )
    bot = FakeTelegram()
    bot.batch = [
        {
            "update_id": 1,
            "message": {
                "text": "/pair test-code",
                "chat": {"type": "private", "id": 101},
                "from": {"id": 101},
            },
        }
    ]
    output = []
    await pair(bot, output=output.append, settings=PairingConfig(timeout_seconds=25, code_bytes=16))
    assert sizes == [16]
    assert any("25 seconds" in line for line in output)


async def test_pairing_timeout_reports_expiry():
    from .conftest import FakeTelegram

    with pytest.raises(ValueError, match="Pairing code expired"):
        await pair(FakeTelegram(), timeout=0.01, output=lambda text: None)


def test_debug_logging_keeps_http_credentials_redacted(capsys):
    import logging

    token = "123:fixture-only"
    previous_handlers = logging.getLogger().handlers[:]
    previous_level = logging.getLogger().level
    try:
        configure_logging(token, "DEBUG")
        logging.debug("Debug credential %s", token)
        output = capsys.readouterr().err
        assert "DEBUG" in output and "[redacted]" in output and token not in output
        assert logging.getLogger("httpx").level == logging.CRITICAL
    finally:
        logging.getLogger().handlers[:] = previous_handlers
        logging.getLogger().setLevel(previous_level)


async def test_real_nondefault_opus_settings_and_size_limit(tmp_path):
    import shutil

    if not shutil.which("ffmpeg"):
        pytest.skip("FFmpeg is not installed")
    time = np.arange(16000, dtype=np.float32) / 16000
    source = (0.2 * np.sin(2 * np.pi * 440 * time)).astype(np.float32)
    settings = CodecConfig(
        bitrate_bps=48000,
        sample_rate=48000,
        application="audio",
        frame_duration_ms=40,
        complexity=3,
        threads=1,
        chunk_bytes=127,
    )
    codec = FFmpegCodec(config=settings)
    blob = await codec.encode(source)
    assert blob.startswith(b"OggS") and b"OpusHead" in blob
    default_blob = await FFmpegCodec().encode(source)
    assert len(blob) > len(default_blob)
    path = tmp_path / "check.ogg"
    path.write_bytes(blob)
    decoded = await codec.decode(path, 48000, 2)
    assert len(decoded) == 48000
    bounded = FFmpegCodec(config=replace(settings, max_encoded_bytes=100))
    with pytest.raises(CodecError, match="limit"):
        await bounded.encode(source)


async def test_configured_conversion_timeout_kills_stalled_executable(tmp_path):
    executable = tmp_path / "stalled-converter"
    executable.write_text("#!/usr/bin/env python3\nimport time\ntime.sleep(3)\n")
    executable.chmod(0o755)
    codec = FFmpegCodec(config=CodecConfig(executable=str(executable), timeout_seconds=0.01))
    with pytest.raises(CodecError, match="timed out"):
        await codec.encode(np.zeros(1600, dtype=np.float32))
