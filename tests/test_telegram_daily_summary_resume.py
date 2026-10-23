"""Daily delivery resumes after a foreground timeout using private local state."""
import asyncio
from datetime import datetime
import pytest

import telegram_bot
from tests.test_telegram_polling_health import bot


@pytest.mark.parametrize('permanently_slow', [False, True])
def test_daily_summary_timeout_checkpoints_recipients_and_resumes(bot, monkeypatch, permanently_slow):
    class Evening(datetime):
        day_number = 23

        @classmethod
        def now(cls):
            return cls(2026, 10, cls.day_number, 21)

    monkeypatch.setattr(telegram_bot, 'datetime', Evening)
    monkeypatch.setattr(telegram_bot.config, 'TELEGRAM_DAILY_SUMMARY_ENABLED', True)
    monkeypatch.setattr(telegram_bot.config, 'TELEGRAM_DAILY_SUMMARY_HOUR', 0)
    monkeypatch.setattr(bot, '_profile_names', lambda: ['synthetic'])
    monkeypatch.setattr(telegram_bot.telegram_access, 'list_users', lambda: [
        {'user_id': uid, 'role': 'admin', 'profile': 'synthetic'} for uid in [1, 2, 3]])
    monkeypatch.setattr(bot, '_daily_summary_snapshot', lambda *a, **k: {
        'analytics_summary': {}, 'recent_runs': [], 'period_label': 'today'})
    monkeypatch.setattr(telegram_bot, 'build_daily_summary_text', lambda **k: 'summary')
    monkeypatch.setattr(bot, '_menu_reply_markup', lambda *a: {})
    delivered, attempts, cancelled = [], [], []

    async def send(uid, *a, **k):
        attempts.append(uid)
        if uid == 2 and (permanently_slow or attempts.count(2) == 1):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(uid)
        delivered.append(uid)
        return {'message_id': uid}

    async def scenario():
        # Reports now start in a real worker. Allow thread startup/disk work;
        # the injected delivery stall remains infinite and must be cancelled.
        bot.UPDATE_TIMEOUT_SECONDS = 2.0
        bot.SLOW_UPDATE_SECONDS = 0.01
        monkeypatch.setattr(bot, '_send_text_safely', send)
        await bot._run_foreground(bot._maybe_send_daily_summary)
        assert delivered == [1]
        assert cancelled == [2]
        # Resume from disk with a new bot instance, not an in-memory delivery set.
        resumed = telegram_bot.TelegramBot('synthetic', runtime_paths=bot.runtime_paths)
        for name in ('_profile_names', '_daily_summary_snapshot', '_menu_reply_markup',
                     '_send_text_safely', 'UPDATE_TIMEOUT_SECONDS', 'SLOW_UPDATE_SECONDS'):
            monkeypatch.setattr(resumed, name, getattr(bot, name))
        await resumed._run_foreground(resumed._maybe_send_daily_summary)
        await resumed._run_foreground(resumed._maybe_send_daily_summary)
        state = resumed._load_state()['daily_summary']
        if permanently_slow:
            assert delivered == [1, 3]
            assert attempts == [1, 2, 3, 2, 2]
            assert cancelled == [2, 2, 2]
            assert state.get('last_sent_date') != '2026-10-23'
            assert state['delivered'] == {'1': 'synthetic', '3': 'synthetic'}
            return
        assert delivered == [1, 3, 2]
        assert attempts == [1, 2, 3, 2]
        assert cancelled == [2]
        assert state['last_sent_date'] == '2026-10-23'
        assert state['recipients'] == 3
        Evening.day_number = 24
        await resumed._run_foreground(resumed._maybe_send_daily_summary)
        assert delivered == [1, 3, 2, 1, 2, 3]
        assert resumed._load_state()['daily_summary']['last_sent_date'] == '2026-10-24'

    asyncio.run(scenario())
