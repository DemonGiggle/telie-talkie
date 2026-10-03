from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import tarfile
import tempfile
from pathlib import Path

import httpx
import numpy as np

from .config import Config

MODEL_NAME = "sherpa-onnx-kws-zipformer-gigaspeech-3.3M-2024-01-01"
MODEL_URL = (
    f"https://github.com/k2-fsa/sherpa-onnx/releases/download/kws-models/{MODEL_NAME}.tar.bz2"
)
VAD_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx"
MODEL_FILES = {
    role: f"{role}-epoch-12-avg-2-chunk-16-left-64.int8.onnx"
    for role in ("encoder", "decoder", "joiner")
}
REQUIRED_FILES = (*MODEL_FILES.values(), "tokens.txt", "bpe.model")
SAMPLE_RATE = 16000
BLOCK_SIZE = 512


def check_models(directory: Path) -> None:
    for name in (*REQUIRED_FILES, "silero_vad.onnx"):
        path = directory / name
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"Missing model {name}; run setup-models")


async def _download(client: httpx.AsyncClient, url: str, path: Path, limit: int) -> None:
    total = 0
    try:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            with path.open("wb") as file:
                async for chunk in response.aiter_bytes(65536):
                    total += len(chunk)
                    if total > limit:
                        raise ValueError("Model download exceeded its size limit")
                    file.write(chunk)
    except httpx.HTTPError:
        raise RuntimeError(
            "Model download failed; check connectivity and retry setup-models"
        ) from None


async def setup_models(directory: Path) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    # Publish only named regular files. No archive paths or links are extracted.
    with tempfile.TemporaryDirectory(prefix=".setup-", dir=directory) as temp:
        staging = Path(temp)
        async with httpx.AsyncClient(follow_redirects=True, timeout=120) as client:
            await _download(client, MODEL_URL, staging / "kws.tar.bz2", 100_000_000)
            await _download(client, VAD_URL, staging / "silero_vad.onnx", 20_000_000)
        with tarfile.open(staging / "kws.tar.bz2", "r:bz2") as archive:
            for name in REQUIRED_FILES:
                member = archive.getmember(f"{MODEL_NAME}/{name}")
                if not member.isfile() or not 0 < member.size <= 30_000_000:
                    raise ValueError("Invalid model archive member")
                with archive.extractfile(member) as source, (staging / name).open("wb") as target:
                    shutil.copyfileobj(source, target)
        manifest = {
            name: hashlib.sha256((staging / name).read_bytes()).hexdigest()
            for name in (*REQUIRED_FILES, "silero_vad.onnx")
        }
        for name in manifest:
            (staging / name).replace(directory / name)
        (directory / "download-manifest.json").write_text(
            json.dumps({"sources": [MODEL_URL, VAD_URL], "sha256": manifest}, indent=2) + "\n",
            encoding="utf-8",
        )


def keyword_text(config: Config) -> str:
    import sentencepiece as spm

    processor = spm.SentencePieceProcessor(model_file=str(config.models.directory / "bpe.model"))
    phrase = " ".join(config.detection.wake_phrase.upper().split())
    tokens = processor.encode(phrase, out_type=str)
    vocabulary = {
        line.rsplit(maxsplit=1)[0]
        for line in (config.models.directory / "tokens.txt")
        .read_text(encoding="utf-8")
        .splitlines()
    }
    if not tokens or any(token not in vocabulary or token == "<unk>" for token in tokens):
        raise ValueError("Wake phrase cannot be represented by the keyword model")
    return " ".join(tokens) + " @" + phrase.replace(" ", "_") + "\n"


class LocalDetector:
    def __init__(self, config: Config):
        import sherpa_onnx

        check_models(config.models.directory)
        directory, detection = config.models.directory, config.detection
        # Generate outside the models directory so systemd can mount models read-only.
        config.storage.directory.mkdir(parents=True, exist_ok=True)
        keywords = config.storage.directory / "keywords.txt"
        keywords.write_text(keyword_text(config), encoding="utf-8")
        self.kws = sherpa_onnx.KeywordSpotter(
            **{role: str(directory / filename) for role, filename in MODEL_FILES.items()},
            tokens=str(directory / "tokens.txt"),
            keywords_file=str(keywords),
            num_threads=detection.num_threads,
            provider="cpu",
            sample_rate=SAMPLE_RATE,
            keywords_score=detection.keywords_score,
            keywords_threshold=detection.keywords_threshold,
            num_trailing_blanks=detection.num_trailing_blanks,
        )
        vad_config = sherpa_onnx.VadModelConfig()
        vad_config.silero_vad.model = str(directory / "silero_vad.onnx")
        vad_config.silero_vad.threshold = detection.vad_threshold
        vad_config.silero_vad.min_speech_duration = detection.vad_min_speech_seconds
        # The recorder owns the final silence timer; this keeps detector hangover short.
        vad_config.silero_vad.min_silence_duration = BLOCK_SIZE / SAMPLE_RATE
        vad_config.silero_vad.max_speech_duration = config.recording.max_seconds + 1
        vad_config.silero_vad.window_size = BLOCK_SIZE
        vad_config.sample_rate = SAMPLE_RATE
        vad_config.num_threads = detection.num_threads
        self.vad = sherpa_onnx.VoiceActivityDetector(vad_config, buffer_size_in_seconds=65)
        self.reset()

    def reset(self) -> None:
        self.stream = self.kws.create_stream()
        self.vad.reset()

    def _wake(self, samples: np.ndarray) -> bool:
        self.stream.accept_waveform(SAMPLE_RATE, samples)
        while self.kws.is_ready(self.stream):
            self.kws.decode_stream(self.stream)
            if self.kws.get_result(self.stream):
                self.kws.reset_stream(self.stream)
                return True
        return False

    async def wake(self, samples: np.ndarray) -> bool:
        return await asyncio.to_thread(self._wake, samples)

    def _speech(self, samples: np.ndarray) -> bool:
        self.vad.accept_waveform(samples)
        detected = self.vad.is_speech_detected()
        while not self.vad.empty():
            self.vad.pop()
        return detected

    async def speech(self, samples: np.ndarray) -> bool:
        return await asyncio.to_thread(self._speech, samples)
