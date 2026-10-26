"""The home count reuses unchanged inputs and expires at real time boundaries."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import analytics
import application_history
import client_hh_auth
import directions
from telegram_app.navigation import TelegramNavigation
from tests.direction_view_fakes import make_direction, two_directions


@pytest.fixture
def clock(monkeypatch):
    value = [datetime(2026, 10, 26, 12, tzinfo=timezone(timedelta(hours=3)))]
    monkeypatch.setattr(analytics, "_now", lambda: value[0])
    return value


def write_events(home, rows):
    (home / "analytics_events.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))


def receipt(identity, created_at, **extra):
    return {"event": "decision", "decision": "applied_auto", "vacancy_id": identity,
            "created_at": created_at.isoformat(), **extra}


def spy_summary(monkeypatch):
    spy = Mock(wraps=application_history._summarize)
    monkeypatch.setattr(application_history, "_summarize", spy)
    return spy


def test_second_unchanged_home_render_does_not_resummarize(tmp_path, monkeypatch, clock):
    make_direction(tmp_path)
    directions.set_active_direction(tmp_path, "qa")
    active = directions.active_direction(tmp_path)
    active["application_mode"] = "confirm"
    monkeypatch.setattr(directions, "active_direction", lambda home: active)
    monkeypatch.setattr(client_hh_auth, "load_hh_resume_catalog", lambda name: [{"id": "synthetic"}])
    write_events(tmp_path, [receipt("one", clock[0] - timedelta(days=1), direction="qa")])
    cookies = tmp_path / "cookies.json"
    cookies.write_text("{}")
    profile = SimpleNamespace(home_dir=tmp_path, analytics_events_file=tmp_path / "analytics_events.jsonl",
        seen_file=tmp_path / "seen_vacancies.json", hh=SimpleNamespace(cookies_file=cookies, application_mode="confirm"))
    bot = SimpleNamespace(_profile=lambda name: profile, _daemon_state=lambda name: {"running": False},
        _review_accessible_items=lambda name: [],
        _profile_schedule=lambda name: (0, 0), _set_selected_menu=lambda *args: None,
        _menu_reply_markup=lambda *args, **kwargs: {}, _send_text=AsyncMock())
    spy = spy_summary(monkeypatch)

    async def render():
        assert await TelegramNavigation._send_start_summary(bot, 42, {"user_id": 42}, "synthetic")
        clock[0] += timedelta(minutes=5)
        assert await TelegramNavigation._send_start_summary(bot, 42, {"user_id": 42}, "synthetic")

    asyncio.run(render())
    assert spy.call_count == 1
    assert all("Отправлено за неделю: 1" in call.args[1] for call in bot._send_text.call_args_list)


def test_period_cache_expires_when_earliest_row_leaves_inclusive_window(tmp_path, monkeypatch, clock):
    expires = clock[0] + timedelta(minutes=10)
    write_events(tmp_path, [receipt("edge", expires - timedelta(days=7)),
                            receipt("newer", clock[0] - timedelta(days=1))])
    spy = spy_summary(monkeypatch)
    assert application_history.summarize(tmp_path, days=7)["count"] == 2
    clock[0] += timedelta(minutes=5)
    assert application_history.summarize(tmp_path, days=7)["count"] == 2
    assert spy.call_count == 1
    clock[0] = expires
    assert application_history.summarize(tmp_path, days=7)["count"] == 2
    assert spy.call_count == 1
    clock[0] += timedelta(microseconds=1)
    assert application_history.summarize(tmp_path, days=7)["count"] == 1
    assert spy.call_count == 2


def test_future_row_enters_without_file_change(tmp_path, clock):
    enters = clock[0] + timedelta(minutes=10)
    write_events(tmp_path, [receipt("future", enters)])
    assert application_history.summarize(tmp_path, days=7)["count"] == 0
    clock[0] = enters
    assert application_history.summarize(tmp_path, days=7)["count"] == 1


def test_real_window_expiry_reuses_application_rows_without_journal_scan(tmp_path, monkeypatch, clock):
    write_events(tmp_path, [receipt("edge", clock[0] - timedelta(days=7) + timedelta(minutes=10))])
    assert application_history.summarize(tmp_path, days=7)["count"] == 1
    monkeypatch.setattr(application_history, "_applications",
                        lambda *args: pytest.fail("A window expiry rescanned unchanged journal inputs"))
    clock[0] += timedelta(minutes=11)
    assert application_history.summarize(tmp_path, days=7)["count"] == 0


def test_seen_only_application_has_the_same_window_expiry(tmp_path, clock):
    saved = clock[0] - timedelta(days=7) + timedelta(minutes=10)
    (tmp_path / "seen_vacancies.json").write_text(json.dumps({"seen": {
        "action": "applied", "date": saved.isoformat()}}))
    assert application_history.summarize(tmp_path, days=7)["count"] == 1
    clock[0] += timedelta(minutes=11)
    assert application_history.summarize(tmp_path, days=7)["count"] == 0


def test_transient_read_failure_does_not_cache_incomplete_count_until_midnight(tmp_path, monkeypatch, clock):
    write_events(tmp_path, [receipt("one", clock[0] - timedelta(days=1))])
    original = application_history.cache.journal
    failed = [False]
    def journal(path):
        if not failed[0]:
            failed[0] = True
            def read(path):
                raise OSError("transient read failure")
            return SimpleNamespace(read=read)
        return original(path)
    monkeypatch.setattr(application_history.cache, 'journal', journal)
    assert application_history.summarize(tmp_path, days=7)['incomplete']
    clock[0] += timedelta(seconds=2)
    result = application_history.summarize(tmp_path, days=7)
    assert not result['incomplete'] and result['count'] == 1


@pytest.mark.parametrize("changed", ["journal", "seen", "snapshots"])
def test_changed_input_invalidates_period_cache(tmp_path, monkeypatch, clock, changed):
    write_events(tmp_path, [receipt("one", clock[0] - timedelta(days=1))])
    spy = spy_summary(monkeypatch)
    assert application_history.summarize(tmp_path, days=7)["count"] == 1
    if changed == "journal":
        with (tmp_path / "analytics_events.jsonl").open("a") as stream:
            stream.write(json.dumps(receipt("two", clock[0])) + "\n")
    elif changed == "seen":
        (tmp_path / "seen_vacancies.json").write_text(json.dumps({"two": {
            "action": "applied", "date": clock[0].isoformat()}}))
    else:
        (tmp_path / "negotiation_snapshots.jsonl").write_text('{}\n')
    result = application_history.summarize(tmp_path, days=7)
    assert result["count"] == (1 if changed == "snapshots" else 2)
    assert spy.call_count == 2


def test_midnight_refresh_and_scope_cache_are_separate(tmp_path, monkeypatch, clock):
    two_directions(tmp_path)
    clock[0] = clock[0].replace(hour=23, minute=59)
    write_events(tmp_path, [receipt("qa", clock[0], direction="qa"),
                            receipt("electric", clock[0], direction="electric")])
    spy = spy_summary(monkeypatch)
    assert application_history.summarize(tmp_path, days=7)["count"] == 1
    assert application_history.summarize(tmp_path, scope="electric", days=7)["count"] == 1
    clock[0] += timedelta(minutes=2)
    assert application_history.summarize(tmp_path, days=7)["count"] == 1
    assert spy.call_count == 3
