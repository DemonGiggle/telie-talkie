import io
import tarfile
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

from telegram_talkie.config import (
    Config,
    DetectionConfig,
    ModelsConfig,
    NetworkConfig,
    StorageConfig,
)
from telegram_talkie.models import KWS_FILES, LocalDetector, keyword_text, setup_models


@pytest.mark.parametrize(
    ("wake_phrase", "expected_phrases"),
    [
        ("Hey Buddy", ("HEY BUDDY",)),
        (("Hey Buddy", "Wake up"), ("HEY BUDDY", "WAKE UP")),
        ((" Hey  Buddy ", "HEY BUDDY", "Wake up"), ("HEY BUDDY", "WAKE UP")),
    ],
)
def test_selected_model_files_and_detection_options_reach_runtime(
    tmp_path, monkeypatch, wake_phrase, expected_phrases
):
    import sys

    models = ModelsConfig(
        directory=tmp_path / "models",
        encoder="custom/encoder.onnx",
        decoder="custom/decoder.onnx",
        joiner="custom/joiner.onnx",
        tokens="custom/tokens.txt",
        bpe_model="custom/bpe.model",
        vad="custom/vad.onnx",
    )
    for field in (*KWS_FILES, "vad"):
        path = models.path(field)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"fixture-model")
    models.path("tokens").write_text("HEY 0\nBUDDY 1\nWAKE 2\nUP 3\n")
    calls = {}

    class Keyword:
        def __init__(self, **kwargs):
            calls["keyword"] = kwargs
            self.result = None

        def create_stream(self):
            return SimpleNamespace(accept_waveform=lambda rate, samples: None)

        def is_ready(self, stream):
            return self.result is not None

        def decode_stream(self, stream):
            pass

        def get_result(self, stream):
            return self.result

        def reset_stream(self, stream):
            self.result = None
            calls["resets"] = calls.get("resets", 0) + 1

    class VadConfig:
        def __init__(self):
            self.silero_vad = SimpleNamespace()

    class Vad:
        def __init__(self, config, **kwargs):
            calls["vad"] = config
            calls["buffer"] = kwargs["buffer_size_in_seconds"]

        def reset(self):
            pass

    class Tokenizer:
        def __init__(self, model_file):
            assert model_file == str(models.path("bpe_model"))

        def encode(self, phrase, out_type):
            assert phrase in expected_phrases and out_type is str
            calls.setdefault("encoded", []).append(phrase)
            return phrase.split()

    monkeypatch.setitem(
        sys.modules,
        "sherpa_onnx",
        SimpleNamespace(
            KeywordSpotter=Keyword, VadModelConfig=VadConfig, VoiceActivityDetector=Vad
        ),
    )
    monkeypatch.setitem(
        sys.modules, "sentencepiece", SimpleNamespace(SentencePieceProcessor=Tokenizer)
    )
    config = Config(
        models=models,
        storage=StorageConfig(tmp_path / "state"),
        detection=DetectionConfig(
            wake_phrase=wake_phrase,
            provider="cuda",
            device=2,
            num_threads=3,
            vad_num_threads=1,
            feature_dim=64,
            max_active_paths=6,
            vad_min_silence_seconds=0.1,
            vad_max_speech_seconds=120,
            vad_buffer_seconds=20,
        ),
    )
    detector = LocalDetector(config)
    keyword = calls["keyword"]
    assert keyword["encoder"] == str(models.path("encoder"))
    assert keyword["tokens"] == str(models.path("tokens"))
    assert Path(keyword["keywords_file"]).read_text() == "".join(
        phrase + " @" + phrase.replace(" ", "_") + "\n" for phrase in expected_phrases
    )
    assert calls["encoded"] == list(expected_phrases)
    assert keyword["provider"] == "cuda" and keyword["device"] == 2
    assert keyword["feature_dim"] == 64 and keyword["max_active_paths"] == 6
    assert calls["vad"].silero_vad.model == str(models.path("vad"))
    assert calls["vad"].silero_vad.min_silence_duration == 0.1
    assert calls["vad"].silero_vad.max_speech_duration == 120
    assert calls["vad"].num_threads == 1 and calls["buffer"] == 20
    samples = np.zeros(512, dtype=np.float32)
    for phrase in expected_phrases:
        detector.kws.result = phrase.replace(" ", "_")
        assert detector._wake(samples)
        assert not detector._wake(samples)
    assert calls["resets"] == len(expected_phrases)


@pytest.mark.parametrize("invalid_tokens", [[], ["MISSING"], ["<unk>"]])
def test_every_wake_phrase_must_be_representable(tmp_path, monkeypatch, invalid_tokens):
    import sys

    models = ModelsConfig(directory=tmp_path)
    models.path("tokens").write_text("HEY 0\nBUDDY 1\n<unk> 2\n")

    class Tokenizer:
        def __init__(self, model_file):
            pass

        def encode(self, phrase, out_type):
            return ["HEY", "BUDDY"] if phrase == "HEY BUDDY" else invalid_tokens

    monkeypatch.setitem(
        sys.modules, "sentencepiece", SimpleNamespace(SentencePieceProcessor=Tokenizer)
    )
    config = Config(
        models=models,
        detection=DetectionConfig(wake_phrase=("HEY BUDDY", "UNKNOWN PHRASE")),
    )
    with pytest.raises(ValueError, match="Wake phrase cannot be represented"):
        keyword_text(config)


@pytest.mark.parametrize("malicious_link", [False, True])
async def test_setup_custom_archive_files_limits_and_destination_paths(
    tmp_path, monkeypatch, malicious_link
):
    settings = ModelsConfig(
        directory=tmp_path / "models",
        encoder="selected/encoder.onnx",
        kws_archive_root="fixture-root",
        kws_archive_url="https://example.invalid/kws.tar.bz2",
        vad_url="https://example.invalid/vad.onnx",
        download_timeout_seconds=25,
        max_archive_bytes=10000,
        max_vad_bytes=100,
        max_model_file_bytes=100,
    )
    payload = io.BytesIO()
    with tarfile.open(fileobj=payload, mode="w:bz2") as archive:
        for field in KWS_FILES:
            name = settings.path(field).name
            member = tarfile.TarInfo(f"fixture-root/{name}")
            content = f"fixture-{field}".encode()
            if malicious_link and field == "joiner":
                member.type = tarfile.SYMTYPE
                member.linkname = "../../outside"
                archive.addfile(member)
            else:
                member.size = len(content)
                archive.addfile(member, io.BytesIO(content))
    requests = []

    async def download(client, url, path, limit, chunk_bytes):
        requests.append((url, limit, chunk_bytes, client.timeout.read, client.timeout.connect))
        path.write_bytes(payload.getvalue() if url == settings.kws_archive_url else b"fixture-vad")

    monkeypatch.setattr("telegram_talkie.models._download", download)
    network = NetworkConfig(chunk_bytes=512, connect_timeout_seconds=3)
    if malicious_link:
        with pytest.raises(ValueError, match="archive member"):
            await setup_models(settings, network)
        assert not settings.path(
            "encoder"
        ).exists()  # Nothing published until validation completes.
    else:
        await setup_models(settings, network)
        for field in (*KWS_FILES, "vad"):
            assert settings.path(field).read_bytes() == f"fixture-{field}".encode()
        assert requests[0] == (settings.kws_archive_url, 10000, 512, 25, 3)
        assert requests[1][1] == 100
