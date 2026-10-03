from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np


class CodecError(Exception):
    pass


class FFmpegCodec:
    def __init__(self, executable: str = "ffmpeg"):
        self.executable = executable

    async def _convert(
        self, args: list[str], max_bytes: int, data: bytes | None = None, timeout: float = 45
    ) -> bytes:
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
            async with asyncio.timeout(timeout):
                while chunk := await process.stdout.read(65536):
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
            await process.wait()
            feeder.cancel()
            await asyncio.gather(feeder, return_exceptions=True)

    async def encode(self, samples: np.ndarray, sample_rate: int = 16000) -> bytes:
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
                "-c:a",
                "libopus",
                "-b:a",
                "24k",
                "-application",
                "voip",
                "-f",
                "ogg",
                "pipe:1",
            ],
            max_bytes=2_000_000,
            data=samples.astype("<f4").tobytes(),
        )

    async def decode(self, path: Path, sample_rate: int, max_seconds: float) -> np.ndarray:
        output = await self._convert(
            [
                # Disable network protocols and select only audio from untrusted incoming media.
                "-protocol_whitelist",
                "file,pipe",
                "-format_whitelist",
                "ogg,mp3,mov,matroska,webm,wav,flac,aac,amr",
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
