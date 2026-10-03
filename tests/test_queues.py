import pytest

from telegram_talkie.codec import CodecError
from telegram_talkie.storage import InstanceLock, StorageFull, Store
from telegram_talkie.telegram import TelegramError

from .conftest import message


async def test_authorization_private_chat_both_ids_and_media_types(rig):
    group = message(4)
    group["message"]["chat"]["type"] = "group"
    rig.telegram.batch = [
        message(1, chat_id=999),
        message(2, user_id=999),
        {"update_id": 3, "message": {"text": "hello"}},
        group,
        message(5),
        message(6, kind="audio"),
    ]
    await rig.poll_once()
    assert rig.store.offset == 7
    assert rig.store.db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 2
    await rig.prepare_once()
    await rig.prepare_once()
    assert rig.telegram.downloads == ["5", "6"]


async def test_duplicate_updates_and_acknowledgment_after_durable_ingest(rig):
    rig.telegram.batch = [message(10), message(10)]
    await rig.poll_once()
    assert rig.telegram.offsets == [0]
    assert rig.store.offset == 11
    await rig.poll_once()
    assert rig.telegram.offsets == [0, 11]
    assert rig.store.db.execute("SELECT COUNT(*) FROM inbox").fetchone()[0] == 1
    await rig.prepare_once()
    await rig.playback_once()
    await rig.poll_once()
    assert rig.store.head("inbox", ("pending",)) is None


def test_ingest_rollback_does_not_ack_or_deduplicate_partial_batch(rig):
    with pytest.raises(TypeError):
        rig.store.ingest(
            [
                message(1),
                {
                    "update_id": 2,
                    "message": {
                        "chat": {"id": 101, "type": "private"},
                        "from": {"id": 101},
                        "voice": {"file_id": "2", "file_size": "invalid"},
                    },
                },
            ],
            101,
            101,
            20,
            10,
        )
    assert rig.store.offset == 0
    assert rig.store.db.execute("SELECT COUNT(*) FROM seen").fetchone()[0] == 0


async def test_upload_retry_does_not_overtake_and_cleans_completed_audio(rig):
    first = rig.store.enqueue_voice(101, b"first")
    rig.store.enqueue_voice(101, b"second")
    rig.telegram.failure = TelegramError("sendVoice", 429, 30)
    assert await rig.send_once()
    row = rig.store.head("outbox", ("pending",))
    assert row["id"] == first and row["attempts"] == 1
    assert rig.store.used_bytes == len(b"firstsecond")
    rig.telegram.failure = None
    assert not await rig.send_once()
    rig.store.db.execute("UPDATE outbox SET next_attempt=0")
    assert await rig.send_once()
    assert await rig.send_once()
    assert [blob for _, blob in rig.telegram.sent] == [b"first", b"second"]
    assert rig.store.used_bytes == 0


async def test_permanent_upload_error_retains_audio(rig):
    rig.store.enqueue_voice(101, b"preserve")
    rig.telegram.failure = TelegramError("sendVoice", 403)
    await rig.send_once()
    assert rig.store.used_bytes == 8
    assert rig.store.head("outbox", ("pending",))["attempts"] == 1


async def test_download_retry_blocks_later_playback(rig):
    rig.telegram.batch = [message(1), message(2)]
    await rig.poll_once()
    rig.telegram.failure = TelegramError("getFile", 500)
    await rig.prepare_once()
    rig.telegram.failure = None
    assert not await rig.prepare_once()
    assert not await rig.playback_once()
    rig.store.db.execute("UPDATE inbox SET next_attempt=0")
    await rig.prepare_once()
    await rig.prepare_once()
    assert rig.telegram.downloads == ["1", "1", "2"]
    assert await rig.playback_once()
    assert await rig.playback_once()


async def test_oversized_and_undecodable_audio_rejected_then_later_audio_plays(rig):
    rig.telegram.batch = [message(1, file_size=20_000_001), message(2), message(3)]
    await rig.poll_once()
    assert rig.store.head_notice()
    rig.codec.decode_error = CodecError("audio cannot be decoded or encoded")
    await rig.prepare_once()
    assert rig.store.used_bytes == 0
    rig.codec.decode_error = None
    await rig.prepare_once()
    assert await rig.playback_once()
    assert rig.store.used_bytes == 0
    assert rig.telegram.downloads == ["2", "3"]
    await rig.notice_once()
    await rig.notice_once()
    assert len(rig.telegram.notices) == 2


async def test_capacity_covers_partial_download_and_outgoing(rig):
    rig.store.max_audio_bytes = 12
    rig.store.enqueue_voice(101, b"keep-me")
    rig.telegram.batch = [message(1)]
    await rig.poll_once()
    await rig.prepare_once()
    assert rig.store.used_bytes == 7
    assert rig.store.head("inbox", ("failed",))
    assert rig.store.head_notice()
    part = rig.store.stage()
    rig.store.append(part, b"12")
    with pytest.raises(StorageFull):
        rig.store.enqueue_voice(101, b"1234")
    assert rig.store.used_bytes == 9
    part.unlink()


async def test_playback_interrupted_restarts_from_beginning(rig):
    import asyncio

    rig.telegram.batch = [message(1)]
    await rig.poll_once()
    await rig.prepare_once()

    async def stop():
        raise asyncio.CancelledError

    rig.audio.on_play = stop
    with pytest.raises(asyncio.CancelledError):
        await rig.playback_once()
    assert rig.store.head("inbox", ("playing",))
    assert rig.store.used_bytes > 0
    rig.store.recover()
    rig.audio.on_play = None
    await rig.playback_once()
    assert len(rig.audio.played) == 2
    assert (rig.audio.played[0] == rig.audio.played[1]).all()
    assert rig.store.used_bytes == 0


async def test_restart_recovers_sending_downloading_playing_and_offset(rig):
    rig.telegram.batch = [message(1), message(2), message(3)]
    await rig.poll_once()
    await rig.prepare_once()
    ready = rig.store.head("inbox", ("ready",))
    rig.store.state("inbox", ready["id"], "playing")
    pending = rig.store.head("inbox", ("pending",))
    rig.store.state("inbox", pending["id"], "downloading")
    rig.store.enqueue_voice(101, b"outgoing")
    outgoing = rig.store.head("outbox", ("pending",))
    rig.store.state("outbox", outgoing["id"], "sending")
    part = rig.store.stage()
    rig.store.append(part, b"interrupted")
    rig.store.close()
    rig.store = Store(rig.config.storage.directory, 512 * 1024 * 1024)
    rig.store.bind("123:rotated-fixture", 101, 101)
    rig.store.recover()
    assert rig.store.offset == 4
    assert rig.store.head("inbox", ("ready",))
    assert rig.store.head("inbox", ("pending",))
    assert rig.store.head("outbox", ("pending",))
    assert not part.exists()
    assert await rig.playback_once()
    assert await rig.send_once()
    await rig.prepare_once()
    await rig.playback_once()


def test_binding_refuses_a_different_bot_or_user_without_cleanup(rig):
    rig.store.enqueue_voice(101, b"keep")
    with pytest.raises(ValueError, match="another bot/user"):
        rig.store.bind("456:fixture", 101, 101)
    with pytest.raises(ValueError):
        rig.store.bind("123:fixture", 202, 202)
    assert rig.store.used_bytes == 4


def test_only_one_instance_can_use_a_queue(tmp_path):
    with InstanceLock(tmp_path):
        with pytest.raises(RuntimeError, match="Another"):
            InstanceLock(tmp_path)
    with InstanceLock(tmp_path):
        pass
