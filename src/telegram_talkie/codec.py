from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
from anyio import CancelScope, fail_after

from .config import MODEL_SAMPLE_RATE, CodecConfig


class CodecError(Exception):
    pass


class FFmpegCodec:
    def __init__(self, executable: str | None = None, config: CodecConfig | None = None):
        self.config = config or CodecConfig()
        self.executable = executable if executable is not None else self.config.executable

    async def _convert(self, args: list[str], max_bytes: int, data: bytes | None = None) -> bytes:
        process = await asyncio.create_subprocess_exec(
            self.executable,
            "-hide_banner",
            "-loglevel",
            "error",
            "-nostdin",
            *args,
            stdin=asyncio.subprocess.PIPE if data is not None else asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        async def feed() -> None:
            if data is not None:
                try:
                    process.stdin.write(data)
                    await process.stdin.drain()
                except (BrokenPipeError, ConnectionResetError):
                    pass
                finally:
                    process.stdin.close()

        feeder = asyncio.create_task(feed())
        output = bytearray()
        try:
            with fail_after(self.config.timeout_seconds):
                while chunk := await process.stdout.read(self.config.chunk_bytes):
                    if len(output) + len(chunk) > max_bytes:
                        raise CodecError("audio exceeds the decoded duration or output limit")
                    output.extend(chunk)
                await feeder
                await process.wait()
            if process.returncode != 0 or not output:
                raise CodecError("audio cannot be decoded or encoded")
            return bytes(output)
        except TimeoutError:
            raise CodecError("audio conversion timed out") from None
        finally:
            if process.returncode is None:
                process.kill()
            # A cancelled worker group must still reap FFmpeg and its input feeder.
            with CancelScope(shield=True):
                await process.wait()
                feeder.cancel()
                await asyncio.gather(feeder, return_exceptions=True)

    async def encode(self, samples: np.ndarray, sample_rate: int = MODEL_SAMPLE_RATE) -> bytes:
        return await self._convert(
            [
                "-f",
                "f32le",
                "-ar",
                str(sample_rate),
                "-ac",
                "1",
                "-i",
                "pipe:0",
                "-ar",
                str(self.config.sample_rate),
                "-c:a",
                "libopus",
                "-b:a",
                str(self.config.bitrate_bps),
                "-application",
                self.config.application,
                "-frame_duration",
                str(self.config.frame_duration_ms),
                "-compression_level",
                str(self.config.complexity),
                "-threads",
                str(self.config.threads),
                "-f",
                "ogg",
                "pipe:1",
            ],
            max_bytes=self.config.max_encoded_bytes,
            data=samples.astype("<f4").tobytes(),
        )

    async def decode(self, path: Path, sample_rate: int, max_seconds: float) -> np.ndarray:
        output = await self._convert(
            [
                # Disable network protocols and select only audio from untrusted incoming media.
                "-protocol_whitelist",
                "file,pipe",
                "-format_whitelist",
                ",".join(self.config.allowed_input_formats),
                "-threads",
                str(self.config.threads),
                "-i",
                str(path),
                "-map",
                "0:a:0",
                "-vn",
                "-sn",
                "-dn",
                "-ar",
                str(sample_rate),
                "-ac",
                "1",
                "-f",
                "f32le",
                "pipe:1",
            ],
            max_bytes=int(sample_rate * max_seconds) * 4,
        )
        if len(output) % 4:
            raise CodecError("invalid decoded audio")
        samples = np.frombuffer(output, dtype="<f4").copy()
        if not np.isfinite(samples).all():
            raise CodecError("audio contains invalid samples")
        return samples
