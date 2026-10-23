"""Exercise the real polling loop with synthetic updates and no network."""
import asyncio
import logging

import pytest

import telegram_bot
from telegram_app.api import TelegramAPIError
from tests.test_telegram_polling_health import bot  # shared isolated runtime fixture


@pytest.mark.parametrize("failure", ["slow", "rate_limit", "exception", "cancelled"])
def test_bad_update_cannot_strand_batch_or_next_poll(bot, monkeypatch, caplog, failure):
    async def scenario():
        monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda *a: None)
        # Production deadlines are 10/30 seconds; keep this deterministic and fast.
        monkeypatch.setattr(bot, "UPDATE_TIMEOUT_SECONDS", 0.04, raising=False)
        monkeypatch.setattr(bot, "SLOW_UPDATE_SECONDS", 0.01, raising=False)
        polls, handled, cancelled = [], [], []

        async def poll():
            polls.append(True)
            if len(polls) == 1:
                return [{"update_id": n, "message": {"text": "/help"}} for n in range(1, 12)]
            return [{"update_id": 12}]

        async def once(method, payload, **kwargs):
            raise TelegramAPIError(method, 429, {"parameters": {"retry_after": 3600}})

        async def handle(update):
            if update["update_id"] == 1:
                try:
                    if failure == "slow":
                        await asyncio.Event().wait()
                    elif failure == "rate_limit":
                        await bot._api_request("sendMessage", {"chat_id": 1})
                    elif failure == "exception":
                        raise ValueError("private text must not be logged")
                    else:
                        raise asyncio.CancelledError()
                finally:
                    cancelled.append(True)
            else:
                handled.append(update["update_id"])
            if update["update_id"] == 12:
                bot._stop_event.set()

        async def maintenance():
            pass

        monkeypatch.setattr(bot, "_api_request_once", once)
        monkeypatch.setattr(bot, "_get_updates", poll)
        monkeypatch.setattr(bot, "_handle_update", handle)
        monkeypatch.setattr(bot, "_maybe_send_daily_summary", maintenance)
        monkeypatch.setattr(bot, "_maybe_send_health_alert", maintenance)
        await asyncio.wait_for(bot.run(), timeout=0.8)
        assert handled == list(range(2, 13))
        assert len(polls) == 2
        assert cancelled == [True]

    with caplog.at_level(logging.INFO):
        asyncio.run(scenario())
    assert "update_id=1" in caplog.text
    assert "handler=" in caplog.text
    assert "duration=" in caplog.text
    assert "private text" not in caplog.text
    if failure in {"slow", "rate_limit"}:
        assert "slow" in caplog.text
        assert "timeout" in caplog.text


@pytest.mark.parametrize('maintenance_failure', ['slow', 'exception'])
def test_maintenance_failure_cannot_stop_next_poll(bot, monkeypatch, maintenance_failure):
    async def scenario():
        monkeypatch.setattr(asyncio.get_running_loop(), 'add_signal_handler', lambda *a: None)
        monkeypatch.setattr(bot, 'UPDATE_TIMEOUT_SECONDS', 0.04)
        monkeypatch.setattr(bot, 'SLOW_UPDATE_SECONDS', 0.01)
        polls, handled = [], []

        async def poll():
            polls.append(True)
            return [{'update_id': len(polls)}]

        async def handle(update):
            handled.append(update['update_id'])
            if len(handled) == 2:
                bot._stop_event.set()

        async def daily():
            if maintenance_failure == 'slow':
                await asyncio.Event().wait()
            raise ValueError('synthetic')

        async def health():
            pass

        monkeypatch.setattr(bot, '_get_updates', poll)
        monkeypatch.setattr(bot, '_handle_update', handle)
        monkeypatch.setattr(bot, '_maybe_send_daily_summary', daily)
        monkeypatch.setattr(bot, '_maybe_send_health_alert', health)
        await asyncio.wait_for(bot.run(), timeout=2)
        assert handled == [1, 2]
        assert bot._foreground_task is None

    asyncio.run(scenario())


@pytest.mark.parametrize('owner_cancel', [False, True])
def test_shutdown_cancels_current_handler_and_skips_remaining_batch(bot, monkeypatch, owner_cancel):
    async def scenario():
        handlers, entered, cancelled = {}, asyncio.Event(), asyncio.Event()
        monkeypatch.setattr(asyncio.get_running_loop(), 'add_signal_handler', handlers.__setitem__)

        async def poll():
            return [{'update_id': 1}, {'update_id': 2}]

        async def handle(update):
            assert update['update_id'] == 1
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()

        async def forbidden():
            pytest.fail('Maintenance started after shutdown')

        monkeypatch.setattr(bot, '_get_updates', poll)
        monkeypatch.setattr(bot, '_handle_update', handle)
        monkeypatch.setattr(bot, '_maybe_send_daily_summary', forbidden)
        monkeypatch.setattr(bot, '_maybe_send_health_alert', forbidden)
        running = asyncio.create_task(bot.run())
        await asyncio.wait_for(entered.wait(), timeout=2)
        if owner_cancel:
            running.cancel()
            with pytest.raises(asyncio.CancelledError):
                await running
        else:
            import signal
            handlers[signal.SIGTERM]()
            handlers[signal.SIGTERM]()
            await asyncio.wait_for(running, timeout=2)
        assert cancelled.is_set()
        assert bot._foreground_task is None

    asyncio.run(scenario())
