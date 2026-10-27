"""Saved-run diagnosis is conditional, bound, repeatable, and read-only."""
import asyncio
import json
from types import SimpleNamespace

import pytest

import analytics
from telegram_app import history
from telegram_app.commands import TelegramCommands
from telegram_app.monitoring import TelegramMonitoring
from telegram_app.callback_handlers.navigation import handle_navigation


def finished(run_id="empty", **overrides):
    return {"run_id": run_id, "finished_at": "2026-10-27T10:00:00", "status": "success",
            "counters": {"matcher_pass": 0, "applied": 0, "fetched": 5, "evaluated": 5}, **overrides}


@pytest.mark.parametrize("run,eligible", [
    (finished(), True),
    (finished(counters={"matcher_pass": 2, "applied": 0}), False),
    (finished(counters={"matcher_pass": None, "applied": 0}), False),
    (finished(counters={"applied": 0}), False),
    (finished(counters={"matcher_pass": False}), False),
    (finished(counters={"matcher_pass": 0, "applied": 1}), False),
    (finished(counters={"matcher_pass": 0, "uncertain": 1}), False),
    (finished(manual=1), False),
    (finished(reason_breakdown={"manual_required": 1}), False),
    (finished(status="incomplete"), False),
    (finished(counter_status="minimum_or_unknown"), False),
    (finished(finished_at=None), False),
    (None, False),
])
def test_button_requires_finished_exact_suitable_zero(tmp_path, run, eligible):
    markup = history.empty_search_markup(tmp_path, run)
    assert bool(markup) is eligible
    assert bool(history.empty_search_diagnosis(run)) is eligible
    if eligible:
        button = markup["inline_keyboard"][0][0]
        assert button["text"] == "❓ Почему ничего не нашлось"
        assert len(button["callback_data"].encode()) <= 64
        assert "Подходящих вакансий: 0" in history.empty_search_diagnosis(run)
        assert "0 откликов" not in history.empty_search_diagnosis(run)


class Harness(TelegramMonitoring, TelegramCommands):
    def __init__(self, home):
        self.profile = SimpleNamespace(home_dir=str(home), analytics_events_file=str(home / "events.jsonl"),
                                       run_history_file=str(home / "runs.jsonl"))
        self.sent = []
        self.dispatched = []
        self.runs = [finished("old"), finished("new", counters={"matcher_pass": 3, "applied": 0})]

    def _profile(self, name):
        return self.profile

    def _selected_profile(self, principal):
        return "client"

    def _recent_runs(self, name, limit):
        return list(reversed(self.runs))[:limit]

    def _latest_run(self, name):
        return self.runs[-1] if self.runs else None

    def _menu_reply_markup(self, principal):
        return {"keyboard": []}

    async def _send_text(self, chat_id, text, **kwargs):
        self.sent.append(text)

    async def _answer_callback_query(self, *args, **kwargs):
        pass

    async def _dispatch(self, chat_id, principal, command, arg):
        self.dispatched.append((command, arg))
        await self._route_why_no_apply(chat_id, principal, command, arg, "client", None)


def context(data, *, allowed=True, private=True):
    return SimpleNamespace(raw_data=data, message={"chat": {"type": "private" if private else "group"}},
        callback_id="1", principal={"role": "user", "user_id": 1}, chat_id=1,
        can_access_profile=lambda profile: allowed)


@pytest.fixture
def bot(tmp_path, monkeypatch):
    bot = Harness(tmp_path)
    monkeypatch.setattr(analytics, "history_events", lambda *args, **kwargs: [])
    def summary(run_id=None, **kwargs):
        assert kwargs["scope"] == "active"
        return next((r for r in bot.runs if r["run_id"] == run_id), None) if run_id else bot.runs[-1]
    monkeypatch.setattr(analytics, "get_run_summary", summary)
    return bot


def test_bound_old_empty_search_does_not_diagnose_new_search(bot):
    data = history.empty_search_markup(bot.profile.home_dir, bot.runs[0])["inline_keyboard"][0][0]["callback_data"]
    asyncio.run(handle_navigation(bot, context(data)))
    asyncio.run(handle_navigation(bot, context(data)))
    assert len(bot.sent) == 2
    assert bot.sent[0] == bot.sent[1]
    assert "запуск old" in bot.sent[0]
    assert "запуск new" not in bot.sent[0]
    assert [r["run_id"] for r in bot.runs] == ["old", "new"]


@pytest.mark.parametrize("failure", ["profile", "removed", "access", "group", "nonempty", "unfinished"])
def test_stale_denied_or_changed_card_never_falls_back(bot, failure):
    data = history.empty_search_markup(bot.profile.home_dir, bot.runs[0])["inline_keyboard"][0][0]["callback_data"]
    if failure == "profile":
        bot.profile.home_dir += "/other-profile"
    elif failure == "removed":
        bot.runs.pop(0)
    elif failure == "nonempty":
        bot.runs[0]["counters"]["matcher_pass"] = 1
    elif failure == "unfinished":
        bot.runs[0]["status"] = "incomplete"
    asyncio.run(handle_navigation(bot, context(data, allowed=failure != "access", private=failure != "group")))
    assert all("Подходящих вакансий: 0" not in text for text in bot.sent)
    if failure in {"profile", "access", "group"}:
        assert not bot.dispatched


def test_slash_command_does_not_confuse_zero_sent_with_zero_suitable(bot):
    asyncio.run(bot._route_why_no_apply(1, {}, "/why_no_apply", "", "client", None))
    assert "Нет завершённого поиска" in bot.sent[-1]


def test_completion_does_not_attach_previous_or_nonempty_search(bot):
    bot.runs = [finished()]
    assert bot._empty_search_completion_markup("client", "empty") is None
    assert bot._empty_search_completion_markup("client", "earlier")
    bot.runs[-1]["counters"]["matcher_pass"] = 1
    assert bot._empty_search_completion_markup("client", "earlier") is None


def test_direction_change_invalidates_card(bot, monkeypatch):
    selected = {"name": "qa"}
    monkeypatch.setattr(history, "HistoryScope", lambda home: SimpleNamespace(name=selected["name"]))
    data = history.empty_search_markup(bot.profile.home_dir, bot.runs[0])["inline_keyboard"][0][0]["callback_data"]
    selected["name"] = "developer"
    asyncio.run(handle_navigation(bot, context(data)))
    assert not bot.dispatched
    assert "изменился" in bot.sent[-1]


def test_summary_preserves_legacy_manual_evidence(bot):
    bot.runs = [finished("legacy")]
    with open(bot.profile.run_history_file, "w") as stream:
        stream.write(json.dumps({"run_id": "legacy", "manual": 1,
                                 "reason_breakdown": {"manual_required": 1}}) + "\n")
    summary = history.search_summary(bot.profile, "client")
    assert not history.empty_search_markup(bot.profile.home_dir, summary)
    asyncio.run(bot._route_why_no_apply(1, {}, "/why_no_apply", "", "client", None))
    assert "Нет завершённого поиска" in bot.sent[-1]


def test_legacy_manual_journal_decision_suppresses_empty_action(bot, monkeypatch):
    bot.runs = [finished("legacy")]
    monkeypatch.setattr(analytics, "history_events", lambda *args, **kwargs: [
        {"event": "decision", "run_id": "legacy", "decision": "manual_review"}])
    summary = history.search_summary(bot.profile, "client")
    assert not history.empty_search_markup(bot.profile.home_dir, summary)


def test_run_summary_adds_action_only_to_empty_active_run(bot, monkeypatch):
    monkeypatch.setattr("telegram_bot_ui.build_run_summary_text", lambda *args, **kwargs: "summary")
    bot.runs = [finished()]
    _, _, markup = history._run_report(bot, {"role": "user"}, "client", "/log", "")
    assert any(b["text"] == history.EMPTY_SEARCH_LABEL for row in markup["inline_keyboard"] for b in row)
    bot.runs[0]["counters"]["matcher_pass"] = 1
    _, _, markup = history._run_report(bot, {"role": "user"}, "client", "/log", "")
    assert not any(b["text"] == history.EMPTY_SEARCH_LABEL for row in markup["inline_keyboard"] for b in row)
