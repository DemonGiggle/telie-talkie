import httpx
import pytest

from telegram_talkie.cli import RedactSecrets, dispatch, pair, parser
from telegram_talkie.telegram import IncomingRejected, Telegram, TelegramError, pairing_ids


@pytest.fixture
def token():
    return "123:fixture-token"  # Deliberately invalid placeholder, never a live credential.


async def test_voice_upload_uses_multipart_ogg_and_poll_offsets(tmp_path, token):
    requests = []

    async def handle(request):
        requests.append(request)
        return httpx.Response(200, json={"ok": True, "result": []})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        bot = Telegram(token, client=client)
        path = tmp_path / "voice.ogg"
        path.write_bytes(b"OggS-test")
        await bot.send_voice(101, path)
        await bot.updates(42)
    assert requests[0].url.path.endswith("/sendVoice")
    assert b"audio/ogg" in requests[0].content
    assert b"OggS-test" in requests[0].content
    assert b'"offset":42' in requests[1].content
    assert b'"allowed_updates":["message"]' in requests[1].content


async def test_file_download_and_size_enforcement(token):
    async def handle(request):
        if request.url.path.endswith("/getFile"):
            return httpx.Response(
                200, json={"ok": True, "result": {"file_path": "voice/clip.oga", "file_size": 4}}
            )
        return httpx.Response(200, content=b"OggS", headers={"content-length": "4"})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        bot = Telegram(token, client=client)
        assert b"".join([c async for c in bot.download("file", 4)]) == b"OggS"
        with pytest.raises(IncomingRejected):
            _ = [c async for c in bot.download("file", 3)]


@pytest.mark.parametrize("size_header", [False, True])
async def test_unknown_download_size_is_capped_from_headers_and_bytes(token, size_header):
    async def handle(request):
        if request.url.path.endswith("/getFile"):
            return httpx.Response(200, json={"ok": True, "result": {"file_path": "audio/file"}})
        return httpx.Response(
            200, content=b"x" * 70000, headers={"content-length": "70000"} if size_header else {}
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        bot = Telegram(token, client=client)
        with pytest.raises(IncomingRejected):
            _ = [c async for c in bot.download("file", 65536)]


@pytest.mark.parametrize("path", ["../secret", "https://example.com", "/secret", "a?b", "a\\b"])
async def test_telegram_file_paths_cannot_redirect_or_traverse(token, path):
    async def handle(request):
        return httpx.Response(200, json={"ok": True, "result": {"file_path": path}})

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        bot = Telegram(token, client=client)
        with pytest.raises(IncomingRejected):
            _ = [c async for c in bot.download("file", 100)]


async def test_retry_after_and_exception_redaction(token):
    async def handle(request):
        return httpx.Response(
            429,
            json={
                "ok": False,
                "error_code": 429,
                "description": token,
                "parameters": {"retry_after": 37},
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        bot = Telegram(token, client=client)
        with pytest.raises(TelegramError) as result:
            await bot.updates(0)
    assert result.value.retryable
    assert result.value.retry_after == 37
    assert token not in str(result.value)


async def test_network_exception_url_never_appears_in_safe_error(token):
    async def handle(request):
        raise httpx.ConnectError(str(request.url), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        bot = Telegram(token, client=client)
        with pytest.raises(TelegramError) as result:
            await bot.call("getMe")
    assert token not in str(result.value)
    assert result.value.__suppress_context__


def test_logging_redacts_formatted_credentials(token):
    import logging

    record = logging.LogRecord("test", logging.WARNING, "example.py", 1, "%s", (token,), None)
    assert RedactSecrets(token).filter(record)
    assert token not in record.getMessage()


async def test_pairing_requires_correct_private_code_and_prints_matching_ids(monkeypatch):
    monkeypatch.setattr("telegram_talkie.cli.secrets.token_hex", lambda _: "test-code")
    from .conftest import FakeTelegram

    bot = FakeTelegram()
    bad = {
        "update_id": 1,
        "message": {
            "text": "/pair wrong-code",
            "chat": {"type": "private", "id": 101},
            "from": {"id": 101},
        },
    }
    good = {
        "update_id": 2,
        "message": {
            "text": "/pair test-code",
            "chat": {"type": "private", "id": 101},
            "from": {"id": 101},
        },
    }
    assert pairing_ids(bad, "test-code") is None
    good["message"]["chat"]["type"] = "group"
    assert pairing_ids(good, "test-code") is None
    good["message"]["chat"]["type"] = "private"
    bot.batch = [bad, good]
    output = []
    assert await pair(bot, 1, output.append) == (101, 101)
    assert "user_id = 101" in output
    assert not any(line.startswith("chat_id =") for line in output)


async def test_known_id_shortcut_needs_no_config_token_or_network(monkeypatch, capsys):
    def unexpected(*args, **kwargs):
        pytest.fail("Known user ID setup must not load configuration or contact Telegram")

    monkeypatch.setattr("telegram_talkie.cli.load_config", unexpected)
    monkeypatch.setattr("telegram_talkie.cli.Telegram", unexpected)
    args = parser().parse_args(["pair", "--user-id", "101"])
    assert await dispatch(args) == 0
    output = capsys.readouterr().out
    assert "user_id = 101" in output and "tap Start" in output


@pytest.mark.parametrize("value", ["0", "-1"])
async def test_known_id_shortcut_rejects_nonpositive_ids(value):
    with pytest.raises(ValueError, match="user ID must be positive"):
        await dispatch(parser().parse_args(["pair", "--user-id", value]))
