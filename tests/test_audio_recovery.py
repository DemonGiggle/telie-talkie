from __future__ import annotations

import asyncio
import sys
import threading
from dataclasses import replace

import numpy as np
import pytest
from anyio import create_task_group, fail_after

from telegram_talkie import cli
from telegram_talkie.audio import AudioError, SoundDeviceAudio, check_device
from telegram_talkie.config import AudioConfig

from .conftest import frames, message


def endpoint(name, *, inputs=1, outputs=1, hostapi=0):
    return dict(name=name, max_input_channels=inputs, max_output_channels=outputs, hostapi=hostapi)


class PortAudio:
    PortAudioError = OSError

    class CallbackStop(Exception):
        pass

    def __init__(self):
        self._initialized = 1
        self.devices = [endpoint("USB Audio (hw:3,0)")]
        self.enumerated = self.devices[:]
        self.events = []
        self.streams = []
        self.input_error = self.output_error = self.play_error = self.start_error = None
        self.initialize_error = None
        self.played = []
        self.waiting = threading.Event()
        self.stopped = threading.Event()
        self.block_output = False
        self.output_status = False
        self.callback_size = 32

    def _terminate(self):
        assert all(stream.closed for stream in self.streams)
        self.events.append("terminate")
        self._initialized -= 1

    def _initialize(self):
        assert self._initialized == 0
        self.events.append("initialize")
        if self.initialize_error:
            raise self.initialize_error
        self._initialized += 1
        self.enumerated = self.devices[:]

    def query_hostapis(self, index):
        return {"name": ["ALSA", "PulseAudio"][index]}

    def query_devices(self, device=None, kind=None):
        if device is None:
            return self.enumerated
        if isinstance(device, int):
            return self.enumerated[device]
        matches = [
            info
            for info in self.enumerated
            if device.casefold() in info["name"].casefold() and info[f"max_{kind}_channels"] > 0
        ]
        if len(matches) != 1:
            raise ValueError(f"No unique {kind} device matching {device!r}")
        return matches[0]

    def check_input_settings(self, **kwargs):
        self.events.append(("check_input", kwargs))
        if self.input_error:
            raise self.input_error

    def check_output_settings(self, **kwargs):
        self.events.append(("check_output", kwargs))
        if self.output_error:
            raise self.output_error

    def InputStream(self, **kwargs):
        owner = self

        class Stream:
            closed = False

            def start(self):
                owner.events.append("start")
                if owner.start_error:
                    raise owner.start_error

            def abort(self):
                owner.events.append("abort")

            def close(self):
                self.closed = True
                owner.events.append("close")

        stream = Stream()
        stream.settings = kwargs
        self.streams.append(stream)
        return stream

    def OutputStream(self, **kwargs):
        if self.play_error:
            raise self.play_error
        owner = self

        class Stream:
            closed = False

            def start(self):
                owner.waiting.set()
                if owner.block_output:
                    return
                while True:
                    samples = np.empty((owner.callback_size, kwargs["channels"]), dtype=np.float32)
                    try:
                        kwargs["callback"](samples, len(samples), None, owner.output_status)
                    except owner.CallbackStop:
                        owner.played.append((samples.copy(), kwargs))
                        break
                    owner.played.append((samples.copy(), kwargs))
                assert kwargs["finished_callback"]() is None  # PortAudio's void callback contract.

            def abort(self):
                owner.stopped.set()
                owner.events.append("output_abort")

            def close(self):
                self.closed = True
                owner.events.append("output_close")

        stream = Stream()
        self.streams.append(stream)
        return stream

    def stop(self):
        self.stopped.set()


@pytest.fixture
def portaudio(monkeypatch):
    sd = PortAudio()
    monkeypatch.setitem(sys.modules, "sounddevice", sd)
    return sd


@pytest.mark.parametrize("direction", ["input", "output"])
@pytest.mark.parametrize("suffix", ["", ", ALSA", ", alsa "])
async def test_stale_card_suffix_resolves_and_reaches_the_actual_stream(
    portaudio, direction, suffix
):
    config = replace(AudioConfig(), **{f"{direction}_device": "USB Audio (hw:2,0)" + suffix})
    audio = SoundDeviceAudio(config)
    await audio.start()
    audio.suspend()
    await audio.play(np.zeros(32, dtype=np.float32))
    assert getattr(audio, f"{direction}_device") == 0
    if direction == "input":
        assert portaudio.streams[0].settings["device"] == 0
    else:
        assert portaudio.played[0][1]["device"] == 0
    assert getattr(config, f"{direction}_device").startswith("USB Audio (hw:2,0)")
    await audio.close()


@pytest.mark.parametrize(
    "devices",
    [
        [],
        [endpoint("USB Audio (hw:3,0)"), endpoint("USB Audio (hw:4,0)")],
        [endpoint("USB Audio (hw:3,1)")],
        [endpoint("USB Audio (hw:3,0)", outputs=0)],
        [endpoint("USB Audio (hw:3,0)", hostapi=1)],
        [endpoint("Other USB Audio (hw:3,0)")],
    ],
)
def test_stale_name_never_selects_an_ambiguous_or_different_endpoint(portaudio, devices):
    portaudio.enumerated = devices
    with pytest.raises(AudioError, match="Configured output"):
        check_device(portaudio, AudioConfig(output_device="USB Audio (hw:2,0)"), "output")


@pytest.mark.parametrize("device", [None, 0, "USB Audio", "USB Audio (hw:3,0)"])
def test_valid_selections_remain_unchanged(portaudio, device):
    assert check_device(portaudio, AudioConfig(output_device=device), "output") == device


@pytest.mark.parametrize("device", ["(hw:2,0)", "missing speaker"])
def test_unknown_names_do_not_fall_back_to_the_default_speaker(portaudio, device):
    with pytest.raises(AudioError):
        check_device(portaudio, AudioConfig(output_device=device), "output")


@pytest.mark.parametrize("direction", ["input", "output"])
async def test_validation_failure_precedes_stream_creation(portaudio, direction):
    setattr(portaudio, f"{direction}_error", OSError("unsupported format"))
    audio = SoundDeviceAudio(AudioConfig())
    with pytest.raises(AudioError, match=f"Configured {direction}"):
        await audio.start()
    assert not portaudio.streams and not audio.enabled


async def test_recovery_refreshes_the_cached_device_list_after_closing_streams(portaudio):
    audio = SoundDeviceAudio(AudioConfig(output_device="USB Audio"))
    await audio.start()
    first = audio.input
    portaudio.devices = [endpoint("USB Audio (hw:4,0)")]
    assert portaudio.enumerated[0]["name"].endswith("(hw:3,0)")
    await audio.start()
    assert first.closed and audio.input is not first
    assert portaudio.enumerated[0]["name"].endswith("(hw:4,0)")
    assert portaudio.events.index("close") < portaudio.events.index("terminate")
    assert portaudio._initialized == 1
    await audio.close()


async def test_failed_backend_reinitialization_can_be_retried(portaudio):
    audio = SoundDeviceAudio(AudioConfig())
    await audio.start()
    portaudio.initialize_error = OSError("device temporarily unavailable")
    with pytest.raises(AudioError):
        await audio.start()
    assert portaudio._initialized == 0 and audio.input is None
    portaudio.initialize_error = None
    await audio.start()
    assert audio.enabled and portaudio._initialized == 1
    assert portaudio.events.count("terminate") == 1
    await audio.close()


async def test_stream_start_failure_is_wrapped_and_closes_partial_stream(portaudio):
    portaudio.start_error = OSError("device busy")
    audio = SoundDeviceAudio(AudioConfig())
    with pytest.raises(AudioError, match="device busy"):
        await audio.start()
    assert portaudio.streams[0].closed and audio.input is None


async def test_playback_open_failure_is_recoverable(portaudio):
    audio = SoundDeviceAudio(AudioConfig())
    await audio.start()
    portaudio.play_error = OSError("speaker disconnected")
    with pytest.raises(AudioError, match="speaker disconnected"):
        await audio.play(np.zeros(32, dtype=np.float32))
    await audio.close()


async def test_cancelling_playback_closes_output_before_backend_refresh(portaudio):
    audio = SoundDeviceAudio(AudioConfig())
    await audio.start()
    portaudio.block_output = True
    playing = asyncio.create_task(audio.play(np.zeros(32, dtype=np.float32)))
    with fail_after(5):
        assert await asyncio.to_thread(portaudio.waiting.wait, 5)
        playing.cancel()
        with pytest.raises(asyncio.CancelledError):
            await playing
    assert portaudio.streams[-1].closed and portaudio.stopped.is_set()
    await audio.start()
    assert portaudio.events.index("output_close") < portaudio.events.index("terminate")
    await audio.close()


async def test_stalled_playback_times_out_and_closes_output(portaudio):
    audio = SoundDeviceAudio(AudioConfig(playback_grace_seconds=0.01))
    await audio.start()
    portaudio.block_output = True
    with fail_after(1), pytest.raises(AudioError, match="stopped completing playback"):
        await audio.play(np.zeros(32, dtype=np.float32))
    assert portaudio.streams[-1].closed and portaudio.stopped.is_set()
    await audio.close()


async def test_output_callback_preserves_samples_and_pads_only_the_final_block(portaudio):
    audio = SoundDeviceAudio(AudioConfig(output_channels=2))
    await audio.start()
    source = np.arange(70, dtype=np.float32) / 100
    await audio.play(source)
    played = np.concatenate([samples for samples, _ in portaudio.played])
    assert played.shape == (96, 2)
    assert np.array_equal(played[:70, 0], source)
    assert np.array_equal(played[:, 0], played[:, 1])
    assert not played[70:].any()
    await audio.close()


async def test_output_underflow_enters_audio_recovery(portaudio):
    audio = SoundDeviceAudio(AudioConfig())
    await audio.start()
    portaudio.output_status = True
    with pytest.raises(AudioError, match="underflow"):
        await audio.play(np.zeros(32, dtype=np.float32))
    assert portaudio.streams[-1].closed
    await audio.close()


async def test_cancelling_discovery_waits_for_setup_and_closes_the_stream(portaudio):
    entered, release = threading.Event(), threading.Event()
    check = portaudio.check_input_settings

    def blocked_check(**kwargs):
        entered.set()
        assert release.wait(5)
        check(**kwargs)

    portaudio.check_input_settings = blocked_check
    audio = SoundDeviceAudio(AudioConfig())
    opening = asyncio.create_task(audio.start())
    with fail_after(5):
        assert await asyncio.to_thread(entered.wait, 5)
        opening.cancel()
        await asyncio.sleep(0)
        assert not opening.done()
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await opening
    assert portaudio.streams[0].closed and audio.input is None and not audio.enabled


@pytest.mark.parametrize("direction", ["input", "output"])
async def test_doctor_reports_both_endpoints_even_if_one_fails(
    rig, portaudio, direction, monkeypatch, capsys
):
    setattr(portaudio, f"{direction}_error", OSError("unavailable"))
    monkeypatch.setattr(cli, "require_token", lambda config: "fixture")
    monkeypatch.setattr(cli.shutil, "which", lambda executable: None)
    monkeypatch.setattr(cli, "check_models", lambda config: None)
    monkeypatch.setattr(cli, "LocalDetector", lambda config: None)
    assert not await cli.doctor(rig.config, online=False, audio_check=False)
    output = capsys.readouterr().out
    for side, label in (("input", "microphone"), ("output", "speaker")):
        assert f"{'FAIL' if side == direction else 'PASS'} PortAudio {label} settings" in output


@pytest.mark.parametrize("failure", ["startup", "microphone", "speaker"])
async def test_audio_recovers_in_process_while_network_workers_keep_running(rig, failure):
    starts = closes = 0
    outage, resumed, network_active, allow_recovery = (asyncio.Event() for _ in range(4))
    downloaded, uploaded, played_all = (asyncio.Event() for _ in range(3))
    original_start, original_close = rig.audio.start, rig.audio.close
    rig.config = replace(rig.config, retry=replace(rig.config.retry, initial_seconds=0.01))
    rig.telegram.batch = [message(1), message(2)]
    rig.store.enqueue_voice(101, b"outgoing during outage")

    async def start():
        nonlocal starts
        starts += 1
        if starts == 1 and failure == "startup":
            outage.set()
            raise AudioError("speaker unavailable at startup")
        if starts > 1:
            await allow_recovery.wait()
            resumed.set()
        await original_start()

    async def close():
        nonlocal closes
        closes += 1
        await original_close()

    async def read():
        if starts == 1 and failure == "microphone":
            outage.set()
            raise AudioError("microphone disconnected")
        await asyncio.Future()

    async def play():
        if starts == 1 and failure == "speaker":
            outage.set()
            raise AudioError("speaker disconnected")

    # Exercise real queue transitions and network workers, then keep capture waiting.
    async def on_play():
        await play()

    rig.audio.start, rig.audio.close, rig.audio.read = start, close, read
    rig.audio.on_play = on_play
    prepare, send, playback = rig.prepare_once, rig.send_once, rig.playback_once

    async def prepare_once():
        result = await prepare()
        if len(rig.telegram.downloads) == 2:
            downloaded.set()
        return result

    async def send_once():
        result = await send()
        if rig.telegram.sent:
            uploaded.set()
        return result

    async def playback_once():
        result = await playback()
        if result and rig.store.head("inbox", ("ready", "playing")) is None:
            played_all.set()
        return result

    rig.prepare_once, rig.send_once, rig.playback_once = prepare_once, send_once, playback_once
    if failure == "speaker":
        await rig.poll_once()
        await rig.prepare_once()
        await rig.prepare_once()
    network_starts = {}
    for name in ("poll_loop", "prepare_loop", "send_loop", "notice_loop"):
        original = getattr(rig, name)

        async def tracked(name=name, original=original):
            network_starts[name] = network_starts.get(name, 0) + 1
            await original()

        setattr(rig, name, tracked)

    async def observe():
        await outage.wait()
        await downloaded.wait()
        await uploaded.wait()
        network_active.set()
        allow_recovery.set()

    with fail_after(5):
        async with create_task_group() as group:
            group.start_soon(rig.run)
            group.start_soon(observe)
            await network_active.wait()
            await resumed.wait()
            await played_all.wait()
            group.cancel_scope.cancel()
    assert starts == 2 and closes >= 2
    assert set(network_starts.values()) == {1}
    assert rig.telegram.downloads == ["1", "2"]
    assert rig.telegram.sent == [(101, b"outgoing during outage")]
    assert rig.store.used_bytes == 0 and not rig.audio.enabled


async def test_failed_playback_preserves_file_order_without_redownload(rig):
    rig.telegram.batch = [message(1), message(2)]
    await rig.poll_once()
    await rig.prepare_once()
    await rig.prepare_once()
    first = rig.store.head("inbox", ("ready",))

    async def fail():
        raise AudioError("speaker unavailable")

    rig.audio.on_play = fail
    with pytest.raises(AudioError):
        await rig.playback_once()
    retained = rig.store.head("inbox", ("ready",))
    assert retained["id"] == first["id"] and retained["path"] == first["path"]
    assert rig.store.path(retained["path"]).read_bytes() == b"valid-audio"
    rig.audio.on_play = None
    assert await rig.playback_once()
    assert rig.store.head("inbox", ("ready",))["update_id"] == 2
    assert await rig.playback_once()
    assert rig.telegram.downloads == ["1", "2"] and rig.store.used_bytes == 0


async def test_completion_tone_failure_preserves_completed_recording(rig):
    rig.audio.frames = frames(1, 0.5) + frames(0, 1.6)

    async def fail_completion():
        if len(rig.audio.played) == 2:
            raise AudioError("speaker disconnected")

    rig.audio.on_play = fail_completion
    with pytest.raises(AudioError):
        await rig.record_once()
    assert rig.store.head("outbox", ("pending",)) is not None
    assert await rig.send_once()
    assert rig.telegram.sent == [(101, b"OggS-fake")]


async def test_audio_retry_backoff_is_capped_and_shutdown_is_prompt(rig, monkeypatch):
    delays = []
    real_sleep = asyncio.sleep
    rig.config = replace(
        rig.config,
        retry=replace(rig.config.retry, initial_seconds=0.1, max_seconds=0.2, jitter_ratio=0),
    )

    async def unavailable():
        raise AudioError("microphone unavailable")

    async def sleep(delay):
        delays.append(delay)
        if len(delays) == 4:
            raise asyncio.CancelledError
        await real_sleep(0)

    rig.audio.start = unavailable
    monkeypatch.setattr("telegram_talkie.app.asyncio.sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await rig.audio_loop()
    assert delays == pytest.approx([0.1, 0.2, 0.2, 0.2])
    assert not rig.audio.enabled
