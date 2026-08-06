import asyncio

import pytest

import telegram_bot
from telegram_app import auth_bridge as telegram_auth_bridge
from telegram_app.auth_bridge import (
    TelegramHHAuthBridge,
    can_answer_hh_auth_prompt,
    clean_hh_auth_code_text,
    looks_like_standalone_hh_auth_code,
)


class _FakeBridge:
    def __init__(
        self,
        pending=None,
        *,
        peek_error: Exception | None = None,
        write_error: Exception | None = None,
    ):
        self.pending = pending
        self.peek_error = peek_error
        self.write_error = write_error
        self.writes = []

    def peek_pending(self):
        if self.peek_error:
            raise self.peek_error
        return self.pending

    def write_response(self, request_id: str, answer: str):
        if self.write_error:
            raise self.write_error
        self.writes.append((request_id, answer))


class _Host(TelegramHHAuthBridge):
    def __init__(self, *, menu_buttons=()):
        self.messages = []
        self.audit_events = []
        menu_buttons = set(menu_buttons)
        super().__init__(
            admin_role="admin",
            is_menu_button=lambda value: value in menu_buttons,
        )

    async def _send_text(self, chat_id: int, text: str, **kwargs):
        self.messages.append((chat_id, text, kwargs))
        return {}

    def _append_debug_log(self, event: str, **payload):
        self.audit_events.append((event, payload))


def test_hh_auth_helpers_preserve_existing_contract():
    assert clean_hh_auth_code_text("12 34-56") == "123456"
    assert clean_hh_auth_code_text("код: 9876") == "9876"
    assert looks_like_standalone_hh_auth_code("7113") is True
    assert looks_like_standalone_hh_auth_code("123") is False
    assert looks_like_standalone_hh_auth_code("код: 7113") is False

    pending = {"profile_name": "client_42"}
    assert can_answer_hh_auth_prompt(
        {"user_id": 1, "role": "admin"},
        pending,
    )
    assert can_answer_hh_auth_prompt(
        {"user_id": 42, "role": "user", "profile": "client_42"},
        pending,
    )
    assert not can_answer_hh_auth_prompt(
        {"user_id": 43, "role": "user", "profile": "client_43"},
        pending,
    )


def test_telegram_bot_reexports_helpers_and_uses_bridge_mixin():
    bot = telegram_bot.TelegramBot("qa")

    assert isinstance(bot, TelegramHHAuthBridge)
    assert telegram_bot._clean_hh_auth_code_text is clean_hh_auth_code_text
    assert (
        telegram_bot._looks_like_standalone_hh_auth_code
        is looks_like_standalone_hh_auth_code
    )
    assert (
        telegram_bot._can_answer_hh_auth_prompt
        is can_answer_hh_auth_prompt
    )


@pytest.mark.parametrize(
    "text",
    ["", "/status", "➡ Назад", "Статус"],
)
def test_auth_bridge_ignores_commands_and_menu_buttons(
    text,
    monkeypatch,
):
    host = _Host(menu_buttons={"Статус"})

    def fail_load():
        raise AssertionError("bridge must not be loaded")

    monkeypatch.setattr(telegram_auth_bridge, "_load_bridge", fail_load)

    accepted = asyncio.run(
        host._maybe_accept_hh_auth_response(
            42,
            {"role": "admin"},
            text,
        )
    )

    assert accepted is False
    assert host.messages == []


def test_auth_bridge_rejects_other_profile_response(monkeypatch):
    bridge = _FakeBridge(
        {
            "id": "request-1",
            "kind": "code",
            "profile_name": "client_42",
        }
    )
    monkeypatch.setattr(
        telegram_auth_bridge,
        "_load_bridge",
        lambda: bridge,
    )
    host = _Host()

    accepted = asyncio.run(
        host._maybe_accept_hh_auth_response(
            43,
            {
                "user_id": 43,
                "role": "user",
                "profile": "client_43",
            },
            "1234",
        )
    )

    assert accepted is False
    assert bridge.writes == []
    assert host.messages == []


def test_auth_bridge_accepts_and_normalizes_code(monkeypatch):
    bridge = _FakeBridge(
        {
            "id": "request-1",
            "kind": "code",
            "profile_name": "client_42",
        }
    )
    monkeypatch.setattr(
        telegram_auth_bridge,
        "_load_bridge",
        lambda: bridge,
    )
    host = _Host()

    accepted = asyncio.run(
        host._maybe_accept_hh_auth_response(
            42,
            {
                "user_id": 42,
                "role": "user",
                "profile": "client_42",
            },
            "код: 12 34",
        )
    )

    assert accepted is True
    assert bridge.writes == [("request-1", "1234")]
    assert host.audit_events == [
        (
            "hh_auth_response_accepted",
            {
                "user_id": 42,
                "profile_name": "client_42",
                "kind": "code",
            },
        )
    ]
    assert host.messages == [
        (
            42,
            (
                "✅ Принял SMS-код HH для профиля client_42. "
                "Ввожу в браузер HH…"
            ),
            {},
        )
    ]


def test_auth_bridge_prompts_again_for_invalid_code(monkeypatch):
    bridge = _FakeBridge(
        {
            "id": "request-1",
            "kind": "code",
            "profile_name": "qa",
        }
    )
    monkeypatch.setattr(
        telegram_auth_bridge,
        "_load_bridge",
        lambda: bridge,
    )
    host = _Host()

    accepted = asyncio.run(
        host._maybe_accept_hh_auth_response(
            1,
            {"user_id": 1, "role": "admin"},
            "12",
        )
    )

    assert accepted is True
    assert bridge.writes == []
    assert host.messages[0][1] == (
        "🔐 Жду HH SMS-код: 4-8 цифр без лишнего текста."
    )


@pytest.mark.parametrize(
    ("value", "expected_write", "expected_message"),
    [
        (
            "candidate@example.com",
            ("request-2", "candidate@example.com"),
            "✅ Принял логин HH для профиля qa. Ввожу в браузер HH…",
        ),
        (
            "x",
            None,
            "🔐 Жду телефон или email для входа HH.",
        ),
    ],
)
def test_auth_bridge_validates_login(
    value,
    expected_write,
    expected_message,
    monkeypatch,
):
    bridge = _FakeBridge(
        {
            "id": "request-2",
            "kind": "login",
            "profile_name": "qa",
        }
    )
    monkeypatch.setattr(
        telegram_auth_bridge,
        "_load_bridge",
        lambda: bridge,
    )
    host = _Host()

    accepted = asyncio.run(
        host._maybe_accept_hh_auth_response(
            1,
            {"user_id": 1, "role": "admin"},
            value,
        )
    )

    assert accepted is True
    assert bridge.writes == (
        [expected_write] if expected_write is not None else []
    )
    assert host.messages[0][1] == expected_message


def test_auth_bridge_ignores_unknown_prompt_kind(monkeypatch):
    bridge = _FakeBridge(
        {
            "id": "request-3",
            "kind": "captcha",
            "profile_name": "qa",
        }
    )
    monkeypatch.setattr(
        telegram_auth_bridge,
        "_load_bridge",
        lambda: bridge,
    )
    host = _Host()

    accepted = asyncio.run(
        host._maybe_accept_hh_auth_response(
            1,
            {"user_id": 1, "role": "admin"},
            "answer",
        )
    )

    assert accepted is False
    assert bridge.writes == []
    assert host.messages == []


def test_auth_bridge_treats_peek_failure_as_no_pending(monkeypatch):
    bridge = _FakeBridge(peek_error=RuntimeError("read failed"))
    monkeypatch.setattr(
        telegram_auth_bridge,
        "_load_bridge",
        lambda: bridge,
    )
    host = _Host()

    accepted = asyncio.run(
        host._maybe_accept_hh_auth_response(
            1,
            {"user_id": 1, "role": "admin"},
            "1234",
        )
    )

    assert accepted is False
    assert host.messages == []


def test_auth_bridge_reports_response_write_failure(monkeypatch):
    bridge = _FakeBridge(
        {
            "id": "request-4",
            "kind": "code",
            "profile_name": "qa",
        },
        write_error=RuntimeError("disk full"),
    )
    monkeypatch.setattr(
        telegram_auth_bridge,
        "_load_bridge",
        lambda: bridge,
    )
    host = _Host()

    accepted = asyncio.run(
        host._maybe_accept_hh_auth_response(
            1,
            {"user_id": 1, "role": "admin"},
            "1234",
        )
    )

    assert accepted is True
    assert host.messages[0][1] == (
        "❌ Не смог передать ответ в HH auth: disk full"
    )
