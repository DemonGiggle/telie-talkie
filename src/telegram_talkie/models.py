from __future__ import annotations

import asyncio
import hashlib
import json
import os
import shutil
import tarfile
import tempfile
from pathlib import Path

import httpx
import numpy as np

from .config import MODEL_SAMPLE_RATE, Config, ModelsConfig, NetworkConfig

KWS_FILES = ("encoder", "decoder", "joiner", "tokens", "bpe_model")
# These are model format requirements, independent of hardware capture settings.
VAD_WINDOW_SIZE = 512


def check_models(settings: ModelsConfig) -> None:
    for field in (*KWS_FILES, "vad"):
        path = settings.path(field)
        if not path.is_file() or path.stat().st_size == 0:
            raise ValueError(f"Missing {field} model file; run setup-models or set models.{field}")


async def _download(
    client: httpx.AsyncClient, url: str, path: Path, limit: int, chunk_bytes: int
) -> None:
    total = 0
    try:
        async with client.stream("GET", url) as response:
            response.raise_for_status()
            with path.open("wb") as file:
                async for chunk in response.aiter_bytes(chunk_bytes):
                    total += len(chunk)
                    if total > limit:
                        raise ValueError("Model download exceeded its size limit")
                    file.write(chunk)
    except httpx.HTTPError:
        raise RuntimeError(
            "Model download failed; check connectivity and retry setup-models"
        ) from None


async def setup_models(settings: ModelsConfig, network: NetworkConfig | None = None) -> None:
    network = network or NetworkConfig()
    directory = settings.directory
    directory.mkdir(parents=True, exist_ok=True)
    # Publish only named regular files. No archive paths or links are extracted.
    with tempfile.TemporaryDirectory(prefix=".setup-", dir=directory) as temp:
        staging = Path(temp)
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(
                settings.download_timeout_seconds,
                connect=network.connect_timeout_seconds,
                write=network.write_timeout_seconds,
                pool=network.pool_timeout_seconds,
            ),
            limits=httpx.Limits(
                max_connections=network.max_connections,
                max_keepalive_connections=network.max_keepalive_connections,
            ),
        ) as client:
            await _download(
                client,
                settings.kws_archive_url,
                staging / "kws.tar.bz2",
                settings.max_archive_bytes,
                network.chunk_bytes,
            )
            await _download(
                client,
                settings.vad_url,
                staging / "vad",
                settings.max_vad_bytes,
                network.chunk_bytes,
            )
        with tarfile.open(staging / "kws.tar.bz2", "r:bz2") as archive:
            for field in KWS_FILES:
                name = Path(getattr(settings, field)).name
                member = archive.getmember(str(Path(settings.kws_archive_root) / name))
                if not member.isfile() or not 0 < member.size <= settings.max_model_file_bytes:
                    raise ValueError("Invalid model archive member")
                with archive.extractfile(member) as source, (staging / field).open("wb") as target:
                    shutil.copyfileobj(source, target)
        manifest = {
            field: hashlib.sha256((staging / field).read_bytes()).hexdigest()
            for field in (*KWS_FILES, "vad")
        }
        for field in manifest:
            destination = settings.path(field)
            destination.parent.mkdir(parents=True, exist_ok=True)
            # A selected model path may live on a separate filesystem. Publish each validated
            # file using a temporary sibling so its final rename stays atomic.
            with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as output:
                temporary = Path(output.name)
                try:
                    with (staging / field).open("rb") as source:
                        shutil.copyfileobj(source, output)
                    output.flush()
                    os.fsync(output.fileno())
                    os.fchmod(output.fileno(), 0o644)
                    output.close()
                    temporary.replace(destination)
                finally:
                    temporary.unlink(missing_ok=True)
        (directory / "download-manifest.json").write_text(
            json.dumps(
                {"sources": [settings.kws_archive_url, settings.vad_url], "sha256": manifest},
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )


def keyword_text(config: Config) -> str:
    import sentencepiece as spm

    processor = spm.SentencePieceProcessor(model_file=str(config.models.path("bpe_model")))
    vocabulary = {
        line.rsplit(maxsplit=1)[0]
        for line in config.models.path("tokens").read_text(encoding="utf-8").splitlines()
    }
    lines = []
    for phrase in config.detection.wake_phrases:
        tokens = processor.encode(phrase, out_type=str)
        if not tokens or any(token not in vocabulary or token == "<unk>" for token in tokens):
            raise ValueError("Wake phrase cannot be represented by the keyword model")
        lines.append(" ".join(tokens) + " @" + phrase.replace(" ", "_") + "\n")
    return "".join(lines)


class LocalDetector:
    def __init__(self, config: Config):
        import sherpa_onnx

        check_models(config.models)
        detection = config.detection
        # Generate outside the models directory so systemd can mount models read-only.
        config.storage.directory.mkdir(parents=True, exist_ok=True)
        keywords = config.storage.directory / "keywords.txt"
        keywords.write_text(keyword_text(config), encoding="utf-8")
        self.kws = sherpa_onnx.KeywordSpotter(
            **{role: str(config.models.path(role)) for role in ("encoder", "decoder", "joiner")},
            tokens=str(config.models.path("tokens")),
            keywords_file=str(keywords),
            num_threads=detection.num_threads,
            provider=detection.provider,
            device=detection.device,
            sample_rate=MODEL_SAMPLE_RATE,
            feature_dim=detection.feature_dim,
            max_active_paths=detection.max_active_paths,
            keywords_score=detection.keywords_score,
            keywords_threshold=detection.keywords_threshold,
            num_trailing_blanks=detection.num_trailing_blanks,
        )
        vad_config = sherpa_onnx.VadModelConfig()
        vad_config.silero_vad.model = str(config.models.path("vad"))
        vad_config.silero_vad.threshold = detection.vad_threshold
        vad_config.silero_vad.min_speech_duration = detection.vad_min_speech_seconds
        # The recorder owns the final silence timer; this keeps detector hangover short.
        vad_config.silero_vad.min_silence_duration = detection.vad_min_silence_seconds
        vad_config.silero_vad.max_speech_duration = (
            detection.vad_max_speech_seconds or config.recording.max_seconds + 1
        )
        vad_config.silero_vad.window_size = VAD_WINDOW_SIZE
        vad_config.sample_rate = MODEL_SAMPLE_RATE
        vad_config.num_threads = detection.vad_num_threads or detection.num_threads
        vad_config.provider = detection.provider
        self.vad = sherpa_onnx.VoiceActivityDetector(
            vad_config, buffer_size_in_seconds=detection.vad_buffer_seconds
        )
        self.reset()

    def reset(self) -> None:
        self.stream = self.kws.create_stream()
        self.vad.reset()

    def _wake(self, samples: np.ndarray) -> bool:
        self.stream.accept_waveform(MODEL_SAMPLE_RATE, samples)
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
