import telegram_access


class TestTelegramAccess:
    def test_normalize_entry_defaults(self):
        entry = telegram_access._normalize_entry({"user_id": 123})
        assert entry["user_id"] == 123
        assert entry["role"] == telegram_access.ROLE_USER
        assert entry["profile"] == "default"

    def test_normalize_registry_adds_owner(self, monkeypatch):
        monkeypatch.setattr(telegram_access.config, "NOTIFY_CHAT_ID", 999)
        registry = telegram_access._normalize_registry({"users": []})
        assert registry["users"][0]["user_id"] == 999
        assert registry["users"][0]["role"] == telegram_access.ROLE_ADMIN
