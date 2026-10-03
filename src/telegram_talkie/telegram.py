from __future__ import annotations

import math
import re
from collections.abc import AsyncIterator
from pathlib import Path
from urllib.parse import quote

import httpx

from .config import NetworkConfig, TelegramConfig


class TelegramError(Exception):
    """A safe error that never contains request URLs, credentials, or response text."""

    def __init__(self, operation: str, code: int = 0, retry_after: float = 0):
        self.code = code
        self.retryable = code == 0 or code in (408, 409, 429) or code >= 500
        self.retry_after = retry_after
        super().__init__(f"Telegram {operation} failed (status {code or 'network'})")


class IncomingRejected(Exception):
    pass


class Telegram:
    def __init__(
        self,
        token: str,
        poll_timeout: int = 30,
        client: httpx.AsyncClient | None = None,
        *,
        config: TelegramConfig | None = None,
        network: NetworkConfig | None = None,
    ):
        if not re.fullmatch(r"[0-9]+:[A-Za-z0-9_-]+", token):
            raise ValueError("The bot token has an invalid format")
        settings = config or TelegramConfig(poll_timeout=poll_timeout)
        self.network = network or NetworkConfig()
        base = settings.api_base_url.rstrip("/")
        self._base = f"{base}/bot{token}/"
        self._files = f"{base}/file/bot{token}/"
        self.poll_timeout = settings.poll_timeout
        self._owned = client is None
        self.client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(
                connect=self.network.connect_timeout_seconds,
                read=self.network.read_timeout_seconds,
                write=self.network.write_timeout_seconds,
                pool=self.network.pool_timeout_seconds,
            ),
            limits=httpx.Limits(
                max_connections=self.network.max_connections,
                max_keepalive_connections=self.network.max_keepalive_connections,
            ),
            follow_redirects=False,
        )

    async def close(self) -> None:
        if self._owned:
            await self.client.aclose()

    @staticmethod
    def _result(response: httpx.Response, operation: str):
        try:
            body = response.json()
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, UnicodeError):
            raise TelegramError(
                operation, response.status_code if response.is_error else 502
            ) from None
        if response.is_error or not body.get("ok"):
            retry = body.get("parameters", {}).get("retry_after", 0)
            retry = float(retry) if type(retry) in (int, float) and math.isfinite(retry) else 0
            code = body.get("error_code", response.status_code)
            raise TelegramError(operation, code, max(0, retry))
        if "result" not in body:
            raise TelegramError(operation, 502)
        return body["result"]

    async def call(self, method: str, data: dict | None = None):
        try:
            response = await self.client.post(self._base + method, json=data or {})
        except httpx.HTTPError:
            raise TelegramError(method) from None
        return self._result(response, method)

    async def updates(self, offset: int) -> list[dict]:
        return await self.call(
            "getUpdates",
            {
                "offset": offset,
                "timeout": self.poll_timeout,
                "allowed_updates": ["message"],
            },
        )

    async def send_voice(self, chat_id: int, path: Path) -> None:
        try:
            with path.open("rb") as audio:
                response = await self.client.post(
                    self._base + "sendVoice",
                    data={"chat_id": str(chat_id)},
                    files={"voice": ("message.ogg", audio, "audio/ogg")},
                )
        except httpx.HTTPError:
            raise TelegramError("sendVoice") from None
        self._result(response, "sendVoice")

    async def send_text(self, chat_id: int, message: str) -> None:
        await self.call("sendMessage", {"chat_id": chat_id, "text": message})

    async def download(self, file_id: str, max_bytes: int) -> AsyncIterator[bytes]:
        info = await self.call("getFile", {"file_id": file_id})
        if info.get("file_size", 0) > max_bytes:
            raise IncomingRejected("file exceeds the download limit")
        path = info.get("file_path")
        # Never accept an external URL, traversal, or query string as a file path.
        if (
            not isinstance(path, str)
            or not path
            or path.startswith("/")
            or any(p in ("..", ".") for p in path.split("/"))
            or any(c in path for c in (":", "?", "#", "\\"))
        ):
            raise IncomingRejected("invalid Telegram file path")
        try:
            async with self.client.stream("GET", self._files + quote(path, safe="/")) as response:
                if response.is_error:
                    # URLs expire. Fetch getFile again on the next attempt.
                    raise TelegramError("download", 502)
                length = response.headers.get("content-length")
                if length and length.isdecimal() and int(length) > max_bytes:
                    raise IncomingRejected("file exceeds the download limit")
                total = 0
                async for chunk in response.aiter_bytes(self.network.chunk_bytes):
                    total += len(chunk)
                    if total > max_bytes:
                        raise IncomingRejected("file exceeds the download limit")
                    yield chunk
                if total == 0:
                    raise IncomingRejected("file is empty")
        except httpx.HTTPError:
            raise TelegramError("download") from None


def pairing_ids(update: dict, code: str) -> tuple[int, int] | None:
    message = update.get("message", {})
    chat, user = message.get("chat", {}), message.get("from", {})
    if (
        chat.get("type") == "private"
        and not user.get("is_bot", False)
        and message.get("text", "").strip() == f"/pair {code}"
        and type(chat.get("id")) is int
        and type(user.get("id")) is int
    ):
        return chat["id"], user["id"]
    return None
