import asyncio
import shutil
import subprocess

import numpy as np
import pytest

from telegram_talkie.codec import CodecError, FFmpegCodec

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg is not installed")


async def test_real_ogg_opus_round_trip_and_volume(tmp_path):
    codec = FFmpegCodec()
    time = np.arange(16000, dtype=np.float32) / 16000
    samples = (0.2 * np.sin(2 * np.pi * 440 * time)).astype(np.float32)
    data = await codec.encode(samples)
    assert data.startswith(b"OggS") and b"OpusHead" in data
    path = tmp_path / "clip.ogg"
    path.write_bytes(data)
    decoded = await codec.decode(path, 48000, 2)
    assert len(decoded) == 48000
    assert np.isfinite(decoded).all()
    assert 0.1 < np.sqrt(np.mean(decoded**2)) < 0.2
    assert len(data) < 20000


@pytest.mark.parametrize("suffix,encoder", [("mp3", "libmp3lame"), ("m4a", "aac")])
async def test_real_incoming_audio_formats(tmp_path, suffix, encoder):
    path = tmp_path / f"incoming.{suffix}"
    process = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-v",
        "error",
        "-f",
        "lavfi",
        "-i",
        "sine=frequency=440:duration=0.25",
        "-c:a",
        encoder,
        str(path),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    assert await process.wait() == 0
    decoded = await FFmpegCodec().decode(path, 48000, 1)
    assert 10000 <= len(decoded) <= 16000


async def test_decode_rejects_corrupt_and_actual_duration_over_limit(tmp_path):
    codec = FFmpegCodec()
    path = tmp_path / "incoming.media"
    path.write_bytes(b"this is not audio")
    with pytest.raises(CodecError):
        await codec.decode(path, 16000, 1)
    path.write_bytes(await codec.encode(np.zeros(32000, dtype=np.float32)))
    with pytest.raises(CodecError, match="limit"):
        await codec.decode(path, 16000, 1)
