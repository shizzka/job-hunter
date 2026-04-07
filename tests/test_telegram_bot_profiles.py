from telegram_bot import ROLE_USER, TelegramBot


def test_selected_profile_for_user_prefers_client_profile(monkeypatch):
    bot = TelegramBot(profile_name="default")
    principal = {"user_id": 42, "role": ROLE_USER, "profile": "qa"}

    monkeypatch.setattr(bot, "_default_profile_name", lambda: "qa")
    monkeypatch.setattr(bot, "_profile_names", lambda: ["qa", "client_42"])
    monkeypatch.setattr(bot, "_client_record", lambda user_id: {"profile_name": "client_42"})

    assert bot._selected_profile(principal) == "client_42"


def test_selected_profile_for_user_falls_back_to_access_profile(monkeypatch):
    bot = TelegramBot(profile_name="default")
    principal = {"user_id": 42, "role": ROLE_USER, "profile": "qa"}

    monkeypatch.setattr(bot, "_default_profile_name", lambda: "qa")
    monkeypatch.setattr(bot, "_profile_names", lambda: ["qa"])
    monkeypatch.setattr(bot, "_client_record", lambda user_id: {"profile_name": "client_42"})

    assert bot._selected_profile(principal) == "qa"
