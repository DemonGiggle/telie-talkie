import shutil

import httpx
import numpy as np
import pytest

from telegram_talkie.codec import FFmpegCodec
from telegram_talkie.telegram import Telegram

from .conftest import frames, message


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg is not installed")
async def test_telegram_voice_audio_and_device_recording_round_trip(rig):
    codec = FFmpegCodec()
    blob = await codec.encode(np.ones(1600, dtype=np.float32) * 0.05)
    upload_bodies = []

    async def handle(request):
        operation = request.url.path.rsplit("/", 1)[-1]
        if operation == "getUpdates":
            result = [message(1), message(2, kind="audio"), message(3, user_id=999)]
        elif operation == "getFile":
            result = {"file_path": "voice/test.ogg", "file_size": len(blob)}
        elif operation == "sendVoice":
            upload_bodies.append(request.content)
            result = {"message_id": 1}
        else:
            return httpx.Response(200, content=blob)
        return httpx.Response(200, json={"ok": True, "result": result})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        rig.telegram = Telegram("123:fixture-only", client=client)
        rig.codec = codec
        await rig.poll_once()
        assert rig.store.offset == 4
        assert await rig.prepare_once()
        assert await rig.prepare_once()
        assert not await rig.prepare_once()
        assert await rig.playback_once()
        assert await rig.playback_once()
        assert len(rig.audio.played) == 2
        assert rig.store.used_bytes == 0
        rig.audio.frames = frames(1, 0.5) + frames(0, 1.5)
        await rig.record_once()
        assert await rig.send_once()
        assert b"OggS" in upload_bodies[0] and b"OpusHead" in upload_bodies[0]
        assert rig.store.used_bytes == 0
