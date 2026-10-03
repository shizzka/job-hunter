import asyncio

import telegram_bot


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
