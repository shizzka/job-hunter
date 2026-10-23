"""Critic regressions for summary deadlines and initialization failures."""
import asyncio
import threading
from unittest.mock import Mock

import pytest

import analytics
import profile_access
from telegram_app import history
from tests.direction_view_fakes import two_directions
from tests.test_b7_critic_regressions import scoped_bot
from tests.test_telegram_menu_routing import menu_bot


@pytest.mark.parametrize("command", ["/log", "/raw_log"])
def test_summary_timeout_answers_before_foreground_cancellation(menu_bot, monkeypatch, command):
    bot, _ = menu_bot
    entered, release = threading.Event(), threading.Event()
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=3)
        return None
    monkeypatch.setattr(analytics, "get_run_summary", blocked)
    monkeypatch.setattr(history, "RUN_SUMMARY_TIMEOUT_SECONDS", 0.1, raising=False)
    monkeypatch.setattr(bot, "UPDATE_TIMEOUT_SECONDS", 0.8)
    monkeypatch.setattr(bot, "SLOW_UPDATE_SECONDS", 0.7)
    principal = {"user_id": 42, "role": "user", "profile": "qa"}
    async def scenario():
        try:
            await bot._run_foreground(bot._dispatch, 42, principal, command, "")
            assert entered.is_set()
            bot._send_text.assert_awaited_once()
            text = bot._send_text.call_args.args[1]
            assert "Сводка готовится слишком долго" in text and "/log" in text
        finally:
            release.set()
    asyncio.run(scenario())
    # Timed-out read-only work must never publish a late second reply.
    bot._send_text.assert_awaited_once()


@pytest.mark.parametrize("command", ["/log", "/hunter_log", "/raw_log"])
@pytest.mark.parametrize("stage", ["profile", "scope", "markup"])
def test_summary_initialization_failure_answers_without_rebuilding_markup(menu_bot, monkeypatch, command, stage):
    bot, _ = menu_bot
    menu = Mock(side_effect=OSError("private /srv/profile/markup.json"))
    monkeypatch.setattr(bot, "_menu_reply_markup", menu)
    if stage == "profile":
        monkeypatch.setattr(bot, "_profile", Mock(side_effect=OSError("private /srv/profile/profile.env")))
    elif stage == "scope":
        monkeypatch.setattr(history, "HistoryScope", Mock(side_effect=ValueError("private /srv/profile/direction.json")))
    else:
        monkeypatch.setattr(analytics, "get_run_summary", Mock(return_value=None))
    principal = {"user_id": 42, "role": "user", "profile": "qa"}
    asyncio.run(bot._dispatch(42, principal, command, ""))
    bot._send_text.assert_awaited_once()
    call = bot._send_text.call_args
    assert "Не удалось прочитать сводку" in call.args[1] and "Повторите /log" in call.args[1]
    assert "private" not in call.args[1] and "/srv" not in call.args[1]
    assert not call.kwargs.get("reply_markup")
    assert menu.call_count == (1 if stage == "markup" else 0)


@pytest.mark.parametrize("command", ["/log", "/raw_log"])
def test_invalid_direction_json_gets_plain_error(menu_bot, tmp_path, monkeypatch, command):
    two_directions(tmp_path)
    (tmp_path / "directions" / "qa.json").write_text("{")
    bot, _ = menu_bot
    monkeypatch.setattr(bot, "_menu_reply_markup", Mock(side_effect=AssertionError("Do not rebuild broken storage")))
    principal = {"user_id": 42, "role": "user", "profile": "qa"}
    asyncio.run(bot._dispatch(42, principal, command, ""))
    bot._send_text.assert_awaited_once()
    assert "Не удалось прочитать сводку" in bot._send_text.call_args.args[1]


def test_summary_initialization_preserves_tester_direction_denial(scoped_bot, monkeypatch):
    bot, tester, profile = scoped_bot
    summary = Mock(side_effect=AssertionError("Forbidden scope must not load analytics"))
    monkeypatch.setattr(analytics, "get_run_summary", summary)
    monkeypatch.setattr(bot, "_menu_reply_markup", Mock(side_effect=AssertionError("No markup on denial")))
    with profile_access.principal_scope(tester):
        asyncio.run(history.show(bot, 202, tester, "qa-dev", "/log", "all"))
    bot._send_text.assert_awaited_once()
    assert "🔒" in bot._send_text.call_args.args[1]
    summary.assert_not_called()


def test_owner_cancellation_is_not_reported_as_a_summary_timeout(menu_bot, monkeypatch):
    bot, _ = menu_bot
    entered, release = threading.Event(), threading.Event()
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=3)
        return None
    monkeypatch.setattr(analytics, "get_run_summary", blocked)
    principal = {"user_id": 42, "role": "user", "profile": "qa"}
    async def scenario():
        task = asyncio.create_task(bot._dispatch(42, principal, "/log", ""))
        try:
            assert await asyncio.to_thread(entered.wait, 2)
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
            bot._send_text.assert_not_awaited()
        finally:
            release.set()
    asyncio.run(scenario())
    bot._send_text.assert_not_awaited()
