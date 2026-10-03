import numpy as np
import pytest

from telegram_talkie.config import RecordingConfig
from telegram_talkie.recording import Recording

from .conftest import frames


def test_initial_silence_expires_at_five_seconds():
    recording = Recording(RecordingConfig())
    for _ in range(156):
        assert not recording.feed(np.zeros(512), False)
    assert recording.feed(np.zeros(512), False)
    assert recording.result() is None


def test_speech_silence_trim_and_new_speech_reset():
    recording = Recording(RecordingConfig())
    for chunk in frames(1, 0.5) + frames(0, 1) + frames(1, 0.5):
        assert not recording.feed(chunk, chunk[0] == 1)
    for chunk in frames(0, 1.5):
        recording.feed(chunk, False)
    assert recording.done
    result = recording.result()
    assert np.count_nonzero(result) == 32 * 512
    assert len(result) == 63 * 512 + 3200


def test_limit_is_exact_even_when_last_frame_straddles_it():
    recording = Recording(RecordingConfig())
    for chunk in frames(1, 60.1):
        recording.feed(chunk, True)
    assert recording.done
    assert len(recording.result()) == 60 * 16000


def test_pre_roll_keeps_speech_onset_while_vad_confirms():
    recording = Recording(RecordingConfig())
    for chunk in frames(2, 0.5):
        recording.feed(chunk, False)
    recording.feed(np.ones(512), True)
    assert len(recording.result()) == 4096
    assert np.all(recording.result()[:-512] == 2)


async def test_wake_beep_record_queue_and_speaker_suppression(rig):
    rig.audio.frames = frames(9, 0.032) + frames(1, 0.5) + frames(0, 1.6)
    with pytest.raises(EOFError):
        await rig.audio_loop()
    assert len(rig.audio.played) == 1  # Ready beep; encoded audio excludes wake and beep.
    assert len(rig.codec.encoded) == 1
    assert not np.any(rig.codec.encoded[0] == 9)
    assert len(rig.detector.wake_calls) < 6
    assert rig.store.head("outbox", ("pending",)) is not None
    assert rig.detector.resets == 2


async def test_empty_recording_is_discarded(rig):
    rig.audio.frames = frames(0, 5.1)
    await rig.record_once()
    assert not rig.codec.encoded
    assert rig.store.head("outbox", ("pending",)) is None
    assert rig.audio.enabled


async def test_maximum_recording_through_audio_adapter(rig):
    rig.audio.frames = frames(1, 61)
    await rig.record_once()
    assert len(rig.codec.encoded[0]) == 60 * 16000


async def test_overlapping_incoming_messages_wait_for_recording(rig):
    from .conftest import message

    rig.audio.frames = frames(1, 0.5) + frames(0, 1.5)
    injected = False

    async def incoming():
        nonlocal injected
        if not injected:
            injected = True
            rig.telegram.batch = [message(1), message(2, kind="audio")]
            await rig.poll_once()
            await rig.prepare_once()
            await rig.prepare_once()
            assert len(rig.audio.played) == 1

    rig.audio.on_read = incoming
    await rig.record_once()
    assert len(rig.audio.played) == 1
    assert await rig.playback_once()
    assert await rig.playback_once()
    assert len(rig.audio.played) == 3
    assert len(rig.detector.wake_calls) == 0
    assert rig.audio.enabled and not rig.audio.buffered


async def test_full_storage_reports_error_and_preserves_existing_voice(rig):
    first = rig.store.enqueue_voice(101, b"existing")
    rig.store.max_audio_bytes = len(b"existing")
    rig.audio.frames = frames(1, 0.5) + frames(0, 1.5)
    await rig.record_once()
    assert rig.store.head("outbox", ("pending",))["id"] == first
    assert rig.store.used_bytes == len(b"existing")
    assert rig.store.head_notice()
    assert len(rig.audio.played) == 2  # Ready and error tones.
    assert rig.audio.enabled
