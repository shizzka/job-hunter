import asyncio
import json

import pytest

import telegram_bot
from telegram_app import api as telegram_api
from telegram_app.api import TelegramAPIClient


class _FakeResponse:
    def __init__(self, status: int, payload: dict):
        self.status = status
        self.payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def json(self, *, content_type=None):
        return self.payload


class _FakeSession:
    def __init__(self, response: _FakeResponse | None = None):
        self.response = response
        self.closed = False
        self.calls = []
        self.close_calls = 0

    def post(self, url: str, **kwargs):
        self.calls.append((url, kwargs))
        return self.response

    async def close(self):
        self.close_calls += 1
        self.closed = True


class _FakeFormData:
    def __init__(self):
        self.fields = []

    def add_field(self, name: str, value, **kwargs):
        self.fields.append((name, value, kwargs))


def test_telegram_bot_keeps_api_methods_through_inheritance():
    bot = telegram_bot.TelegramBot("qa")

    assert isinstance(bot, TelegramAPIClient)
    assert bot._sessions == {}
    assert bot._force_direct is False


def test_get_session_reuses_open_session(monkeypatch):
    created = []

    class FakeClientSession(_FakeSession):
        def __init__(self, *, connector, timeout):
            super().__init__()
            self.connector = connector
            self.timeout = timeout
            created.append(self)

    monkeypatch.setattr(telegram_api.aiohttp, "ClientSession", FakeClientSession)
    client = TelegramAPIClient()

    async def scenario():
        first = await client._get_session(False, timeout=17)
        second = await client._get_session(False, timeout=17)
        return first, second

    first, second = asyncio.run(scenario())

    assert first is second
    assert created == [first]
    assert first.connector is None
    assert first.timeout.total == 17


def test_api_request_retries_direct_after_proxy_failure(monkeypatch):
    monkeypatch.setattr(
        telegram_api.config,
        "TELEGRAM_PROXY",
        "socks5://127.0.0.1:1080",
    )
    client = TelegramAPIClient()
    attempts = []

    async def fake_request_once(method, payload, *, use_proxy, timeout):
        attempts.append((method, payload, use_proxy, timeout))
        if use_proxy:
            raise RuntimeError("proxy unavailable")
        return {"message_id": 7}

    client._api_request_once = fake_request_once

    result = asyncio.run(
        client._api_request("sendMessage", {"chat_id": 42}, timeout=23)
    )

    assert result == {"message_id": 7}
    assert attempts == [
        ("sendMessage", {"chat_id": 42}, True, 23),
        ("sendMessage", {"chat_id": 42}, False, 23),
    ]
    assert client._force_direct is True


def test_send_document_retries_direct_after_proxy_failure(monkeypatch):
    monkeypatch.setattr(
        telegram_api.config,
        "TELEGRAM_PROXY",
        "socks5://127.0.0.1:1080",
    )
    client = TelegramAPIClient()
    attempts = []
    markup = {"inline_keyboard": []}

    async def fake_send_document_once(
        chat_id,
        *,
        filename,
        content,
        caption,
        reply_markup,
        use_proxy,
    ):
        attempts.append(
            (
                chat_id,
                filename,
                content,
                caption,
                reply_markup,
                use_proxy,
            )
        )
        if use_proxy:
            raise RuntimeError("proxy unavailable")
        return {"document": True}

    client._send_document_once = fake_send_document_once

    result = asyncio.run(
        client._send_document(
            42,
            filename="resume.md",
            content=b"resume",
            caption="caption",
            reply_markup=markup,
        )
    )

    assert result == {"document": True}
    assert attempts == [
        (42, "resume.md", b"resume", "caption", markup, True),
        (42, "resume.md", b"resume", "caption", markup, False),
    ]
    assert client._force_direct is True


def test_api_request_once_uses_bot_endpoint(monkeypatch):
    monkeypatch.setattr(
        telegram_api.config,
        "TELEGRAM_CONTROL_BOT_TOKEN",
        "test-token",
    )
    session = _FakeSession(
        _FakeResponse(200, {"ok": True, "result": [{"update_id": 9}]})
    )
    client = TelegramAPIClient()
    session_requests = []

    async def fake_get_session(use_proxy, *, timeout):
        session_requests.append((use_proxy, timeout))
        return session

    client._get_session = fake_get_session

    result = asyncio.run(
        client._api_request_once(
            "getUpdates",
            {"timeout": 4},
            use_proxy=False,
            timeout=24,
        )
    )

    assert result == [{"update_id": 9}]
    assert session_requests == [(False, 24)]
    assert session.calls == [
        (
            "https://api.telegram.org/bottest-token/getUpdates",
            {"json": {"timeout": 4}},
        )
    ]


def test_api_request_once_rejects_telegram_error(monkeypatch):
    monkeypatch.setattr(
        telegram_api.config,
        "TELEGRAM_CONTROL_BOT_TOKEN",
        "test-token",
    )
    session = _FakeSession(
        _FakeResponse(429, {"ok": False, "description": "rate limited"})
    )
    client = TelegramAPIClient()

    async def fake_get_session(use_proxy, *, timeout):
        return session

    client._get_session = fake_get_session

    with pytest.raises(RuntimeError, match="getUpdates failed: 429"):
        asyncio.run(
            client._api_request_once(
                "getUpdates",
                {},
                use_proxy=False,
                timeout=20,
            )
        )


def test_send_document_once_builds_multipart_payload(monkeypatch):
    monkeypatch.setattr(
        telegram_api.config,
        "TELEGRAM_CONTROL_BOT_TOKEN",
        "test-token",
    )
    monkeypatch.setattr(telegram_api.aiohttp, "FormData", _FakeFormData)
    session = _FakeSession(
        _FakeResponse(200, {"ok": True, "result": {"document": True}})
    )
    client = TelegramAPIClient()
    session_requests = []

    async def fake_get_session(use_proxy, *, timeout):
        session_requests.append((use_proxy, timeout))
        return session

    client._get_session = fake_get_session
    markup = {"inline_keyboard": [[{"text": "Ок"}]]}

    result = asyncio.run(
        client._send_document_once(
            42,
            filename="resume.md",
            content=b"resume",
            caption="caption",
            reply_markup=markup,
            use_proxy=True,
        )
    )

    assert result == {"document": True}
    assert session_requests == [(True, 120)]
    assert session.calls[0][0] == (
        "https://api.telegram.org/bottest-token/sendDocument"
    )
    form = session.calls[0][1]["data"]
    fields = {name: (value, kwargs) for name, value, kwargs in form.fields}
    assert fields["chat_id"][0] == "42"
    assert fields["caption"][0] == "caption"
    assert json.loads(fields["reply_markup"][0]) == markup
    assert fields["document"] == (
        b"resume",
        {
            "filename": "resume.md",
            "content_type": "text/markdown; charset=utf-8",
        },
    )


def test_close_sessions_closes_open_sessions_and_clears_cache():
    open_session = _FakeSession()
    closed_session = _FakeSession()
    closed_session.closed = True
    client = TelegramAPIClient()
    client._sessions = {False: open_session, True: closed_session}

    asyncio.run(client._close_sessions())

    assert open_session.close_calls == 1
    assert closed_session.close_calls == 0
    assert client._sessions == {}
