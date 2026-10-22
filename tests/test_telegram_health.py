import asyncio
from unittest.mock import AsyncMock, Mock

import pytest

import telegram_bot
from tests.direction_view_fakes import make_direction


def test_health_alert_does_not_grant_admin_menu_to_unknown_recipient(monkeypatch, tmp_path):
    monkeypatch.setattr(telegram_bot.config, "TELEGRAM_BOT_STATE_FILE", str(tmp_path / "bot-state.json"))
    bot = telegram_bot.TelegramBot(profile_name="qa")
    sent_messages = []
    menu_calls = []

    monkeypatch.setattr(telegram_bot.config, "TELEGRAM_HEALTH_CHECK_ENABLED", True)
    monkeypatch.setattr(telegram_bot.config, "TELEGRAM_HEALTH_CHECK_INTERVAL_MIN", 5)
    monkeypatch.setattr(telegram_bot.telegram_access, "resolve_user", lambda user_id: None)
    monkeypatch.setattr(bot, "_profile_recipient_ids", lambda profile_name: [123])
    monkeypatch.setattr(bot, "_append_debug_log", lambda *args, **kwargs: None)

    async def collect_diagnostics(profile_name):
        return [{"name": "HH auth", "ok": False, "detail": "expired"}]

    def menu_reply_markup(principal):
        menu_calls.append(principal)
        return {"admin": True}

    async def send_text_safely(chat_id, text, *, reply_markup=None):
        sent_messages.append((chat_id, text, reply_markup))
        return {"ok": True}

    monkeypatch.setattr(bot, "_collect_diagnostics", collect_diagnostics)
    monkeypatch.setattr(bot, "_menu_reply_markup", menu_reply_markup)
    monkeypatch.setattr(bot, "_send_text_safely", send_text_safely)

    asyncio.run(bot._maybe_send_health_alert())

    assert menu_calls == []
    assert sent_messages[0][0] == 123
    assert sent_messages[0][2] is None
    assert bot._load_state()["health_check"]["last_alert_signature"] == "HH auth"


@pytest.mark.parametrize("direction_state", ["missing", "invalid", "valid"])
def test_health_alert_skips_tester_and_reaches_following_admin(monkeypatch, tmp_path, direction_state):
    config = telegram_bot.config
    access = telegram_bot.telegram_access
    monkeypatch.setenv("JOB_HUNTER_HOME", str(tmp_path))
    monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))
    monkeypatch.setattr(config, "JOB_HUNTER_INSTANCE_KIND", "dev")
    monkeypatch.setattr(config, "NOTIFY_CHAT_ID", 0)
    monkeypatch.setattr(config, "TELEGRAM_ACCESS_FILE", str(tmp_path / "telegram-users.json"))
    monkeypatch.setattr(config, "TELEGRAM_BOT_STATE_FILE", str(tmp_path / "bot-state.json"))
    monkeypatch.setattr(config, "TELEGRAM_HEALTH_CHECK_ENABLED", True)
    monkeypatch.setattr(config, "TELEGRAM_HEALTH_CHECK_INTERVAL_MIN", 5)

    home = tmp_path / "profiles" / "synthetic-dev"
    home.mkdir(parents=True)
    (home / "profile.env").write_text("HH_ENABLED=0\n")
    direction = "direction_a1b2c3d4"
    make_direction(home, direction)
    access.upsert_user(101, profile="synthetic-dev", role="tester", direction=direction)
    admin = access.upsert_user(202, profile="synthetic-dev", role="admin")
    direction_path = home / "directions" / (direction + ".json")
    if direction_state == "missing":
        direction_path.unlink()
    elif direction_state == "invalid":
        direction_path.write_text("{}")
    if direction_state != "valid":
        with pytest.raises(PermissionError):
            access.resolve_user(101)

    bot = telegram_bot.TelegramBot(profile_name="synthetic-dev")
    monkeypatch.setattr(bot, "_collect_diagnostics", AsyncMock(return_value=[
        {"name": "HH auth", "ok": False, "detail": "expired"},
    ]))
    monkeypatch.setattr(bot, "_menu_reply_markup", Mock(return_value={"admin": True}))
    monkeypatch.setattr(bot, "_send_text_safely", AsyncMock(return_value={"ok": True}))
    monkeypatch.setattr(bot, "_append_debug_log", lambda *args, **kwargs: None)
    assert bot._profile_recipient_ids("synthetic-dev") == [101, 202]

    asyncio.run(bot._maybe_send_health_alert())

    bot._menu_reply_markup.assert_called_once_with(admin)
    bot._send_text_safely.assert_awaited_once()
    assert bot._send_text_safely.call_args.args[0] == 202
    assert bot._send_text_safely.call_args.kwargs["reply_markup"] == {"admin": True}
    assert bot._load_state()["health_check"]["last_alert_signature"] == "HH auth"
