import telegram_clients


class TestTelegramClients:
    def test_normalize_entry_defaults(self):
        entry = telegram_clients._normalize_entry({"user_id": 123})
        assert entry["user_id"] == 123
        assert entry["status"] == telegram_clients.STATUS_NEW
        assert entry["auth_status"] == telegram_clients.AUTH_NOT_STARTED

    def test_submit_application(self, tmp_path, monkeypatch):
        import config

        monkeypatch.setattr(config, "TELEGRAM_CLIENTS_FILE", str(tmp_path / "telegram-clients.json"))
        client = telegram_clients.submit_application(
            42,
            username="tester",
            full_name="Test User",
            target_role="QA Engineer",
            target_location="Remote",
            notes="note",
        )
        assert client["status"] == telegram_clients.STATUS_PENDING_REVIEW
        assert client["full_name"] == "Test User"
        assert client["target_role"] == "QA Engineer"

    def test_set_status(self, tmp_path, monkeypatch):
        import config

        monkeypatch.setattr(config, "TELEGRAM_CLIENTS_FILE", str(tmp_path / "telegram-clients.json"))
        telegram_clients.submit_application(
            42,
            full_name="Test User",
            target_role="QA Engineer",
        )
        updated = telegram_clients.set_status(
            42,
            status=telegram_clients.STATUS_APPROVED,
            auth_status=telegram_clients.AUTH_PENDING_WEB,
            profile_name="client_42",
        )
        assert updated["status"] == telegram_clients.STATUS_APPROVED
        assert updated["auth_status"] == telegram_clients.AUTH_PENDING_WEB
        assert updated["profile_name"] == "client_42"
