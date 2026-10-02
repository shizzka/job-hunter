import telegram_resume_limits


class TestTelegramResumeLimits:
    def test_get_user_snapshot_creates_default(self, tmp_path, monkeypatch):
        import config

        monkeypatch.setattr(config, "TELEGRAM_AI_LIMITS_FILE", str(tmp_path / "telegram-ai-limits.json"))
        monkeypatch.setattr(config, "TELEGRAM_AI_FREE_ANALYSES", 1)

        snapshot = telegram_resume_limits.get_user_snapshot(42)
        assert snapshot["user_id"] == 42
        assert snapshot["free_total"] == 1
        assert snapshot["free_used"] == 0
        assert snapshot["available_soft"] == 1

    def test_record_resume_analysis_tracks_profile(self, tmp_path, monkeypatch):
        import config

        monkeypatch.setattr(config, "TELEGRAM_AI_LIMITS_FILE", str(tmp_path / "telegram-ai-limits.json"))
        monkeypatch.setattr(config, "TELEGRAM_AI_FREE_ANALYSES", 1)

        snapshot = telegram_resume_limits.record_resume_analysis(42, profile_name="qa")
        assert snapshot["free_used"] == 1
        assert snapshot["analysis_total"] == 1
        assert snapshot["profiles"]["qa"]["analysis_count"] == 1

    def test_grant_bonus_and_reset_free_limit(self, tmp_path, monkeypatch):
        import config

        monkeypatch.setattr(config, "TELEGRAM_AI_LIMITS_FILE", str(tmp_path / "telegram-ai-limits.json"))
        monkeypatch.setattr(config, "TELEGRAM_AI_FREE_ANALYSES", 1)

        telegram_resume_limits.record_resume_analysis(42, profile_name="qa")
        granted = telegram_resume_limits.grant_bonus(42, amount=2, actor_user_id=7)
        assert granted["bonus_total"] == 2
        assert granted["available_soft"] == 2

        reset = telegram_resume_limits.reset_free_limit(42, actor_user_id=7, free_total=3)
        assert reset["free_total"] == 3
        assert reset["free_used"] == 0
        assert reset["bonus_total"] == 2
        assert reset["available_soft"] == 5

        events = telegram_resume_limits.recent_events(limit=3)
        assert events[0]["action"] == "reset_free_limit"
        assert events[1]["action"] == "grant_bonus"
