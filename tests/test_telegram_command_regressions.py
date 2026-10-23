"""Night-crawler command regressions; synthetic storage and no HH requests."""
import asyncio
from unittest.mock import Mock

import pytest

import analytics
import telegram_bot_ui as ui
from run_summary import build_run_summary
from tests.test_run_summary import finished_run
from tests.test_telegram_menu_routing import menu_bot


def dispatch(bot, command, arg="", role="user"):
    asyncio.run(bot._dispatch(42, {"user_id": 42, "role": role, "profile": "qa"}, command, arg))
    return [call.args[1] for call in bot._send_text.call_args_list]


@pytest.mark.parametrize("command", ["/log", "/hunter_log"])
@pytest.mark.parametrize("error", [OSError, PermissionError, ValueError, RuntimeError])
def test_t13_log_read_failure_always_answers(menu_bot, monkeypatch, command, error):
    bot, _ = menu_bot
    monkeypatch.setattr(analytics, "get_run_summary", Mock(side_effect=error("private /srv/profile/history.jsonl")))
    texts = dispatch(bot, command)
    assert len(texts) == 1
    assert "Не удалось прочитать сводку" in texts[0]
    assert "Повторите /log" in texts[0]
    assert "private" not in texts[0] and "/srv" not in texts[0] and "/raw_log" not in texts[0]


def test_t13_log_empty_is_one_plain_sentence(menu_bot):
    bot, _ = menu_bot
    assert dispatch(bot, "/log") == ["Запусков пока не было."]


@pytest.mark.parametrize("run_id,expected", [("", "latest"), ("older", "older")])
def test_t13_log_selects_last_or_requested_run(menu_bot, tmp_path, run_id, expected):
    from state_store.private_journal import append_json
    bot, _ = menu_bot
    profile = bot._profile("qa")
    for name in ("older", "latest"):
        append_json(profile.run_history_file, finished_run(run_id=name))
    texts = dispatch(bot, "/log", run_id)
    assert f"Run: {expected}" in texts[-1]


def test_t13_missing_selected_run_is_not_an_empty_history(menu_bot):
    bot, _ = menu_bot
    texts = dispatch(bot, "/log", "missing")
    assert len(texts) == 1 and "Запуск не найден" in texts[0] and "/log" in texts[0]


@pytest.mark.parametrize("has_run", [False, True])
def test_t14_user_raw_log_shows_summary_without_reading_raw_journal(menu_bot, monkeypatch, has_run):
    bot, _ = menu_bot
    tail = Mock(return_value=("/srv/private/search.log", "RAW-PRIVATE-JOURNAL"))
    monkeypatch.setattr(bot, "_log_tail", tail)
    summary = build_run_summary(finished_run()) if has_run else None
    monkeypatch.setattr(analytics, "get_run_summary", Mock(return_value=summary))
    texts = dispatch(bot, "/raw_log")
    tail.assert_not_called()
    if has_run:
        assert "Run: run-a" in texts[-1]
        assert "RAW-PRIVATE-JOURNAL" not in "\n".join(texts)
    else:
        assert texts == ["Запусков пока не было."]


@pytest.mark.parametrize("content", ["", 'INFO complete /srv/private/search.log trace/report.json C:\\private\\cookies.json token=private-key'])
def test_t14_admin_raw_log_has_no_paths_and_preserves_redaction(menu_bot, monkeypatch, content):
    bot, _ = menu_bot
    monkeypatch.setattr(bot, "_log_tail", lambda *a, **kw: ("/srv/private/search.log", content))
    texts = dispatch(bot, "/raw_log", role="admin")
    assert len(texts) == 1
    if content:
        assert "INFO complete" in texts[0]
        assert all(value not in texts[0] for value in ("/srv/", "trace/report.json", "C:\\private", "private-key"))
    else:
        assert texts == ["Журнал пока пуст."]


def test_b30_summary_is_plain_text_and_hint_can_be_copied():
    summary = build_run_summary(finished_run(run_id="run<test>&"))
    text = ui.build_run_summary_text(summary, profile_name="<QA> & test")
    assert "другой запуск: /log run_id" in text
    assert "Run: run<test>&" in text
    assert "&lt;" not in text and "&amp;" not in text


@pytest.mark.parametrize("directions", [False, True])
@pytest.mark.parametrize("known_error", [False, True])
def test_b32_unavailable_counter_gives_action_without_technical_details(menu_bot, tmp_path, monkeypatch, directions, known_error):
    import hh_response_counter as counter
    if directions:
        from tests.direction_view_fakes import two_directions
        two_directions(tmp_path)
    bot, _ = menu_bot
    error = counter.HHResponseCounterError if known_error else ValueError
    monkeypatch.setattr(counter, "refresh", Mock(side_effect=error("private /srv/counter.json")))
    text = dispatch(bot, "/hh_responses")[-1]
    assert "Общее число откликов с hh.ru сейчас не загрузилось" in text
    assert "Попробуйте позже: нажмите «Отклики на hh.ru»" in text
    assert "Вход в hh.ru" in text
    assert "private" not in text and "/srv" not in text


@pytest.mark.parametrize("with_directions", [False, True])
def test_b32_counter_time_is_readable(tmp_path, monkeypatch, with_directions):
    stamp = "2026-10-19T21:34:56.523609+03:00"
    if with_directions:
        from tests.direction_view_fakes import two_directions
        import direction_views
        two_directions(tmp_path)
        value = {"selected": 2, "complete": True, "newer_partial": False, "unmapped": 0, "fetched_at": stamp}
        monkeypatch.setattr(direction_views, "response_counts", lambda home: {"active": value, "archived": None})
    text = ui.build_hh_response_count_text({"total": 3, "fetched_at": stamp}, home_dir=str(tmp_path))
    assert "19 октября в 21:34" in text
    assert stamp not in text and ".523609" not in text and "+03:00" not in text


def test_t13_summary_does_not_resolve_ownership_for_unrelated_old_events(tmp_path, monkeypatch):
    from direction_history import HistoryScope
    from state_store.private_journal import append_json
    history, events = tmp_path / "history.jsonl", tmp_path / "events.jsonl"
    append_json(history, finished_run(funnel={}))
    for index in range(50):
        append_json(events, {"event": "decision", "run_id": f"unrelated-{index}", "vacancy_id": str(index)})
    append_json(events, {"event": "search_started", "run_id": "run-a"})
    append_json(events, {"event": "decision", "run_id": "run-a", "vacancy_id": "1", "decision": "applied_auto", "outcome": "applied"})
    original_decode = analytics.json.loads
    def decode(value, *args, **kwargs):
        assert b"unrelated-" not in (value.encode() if isinstance(value, str) else value)
        return original_decode(value, *args, **kwargs)
    monkeypatch.setattr(analytics.json, "loads", decode)
    original = HistoryScope.events
    def scoped(self, rows):
        assert all(not row.get("run_id", "").startswith("unrelated-") for row in rows)
        return original(self, rows)
    monkeypatch.setattr(HistoryScope, "events", scoped)
    assert analytics.get_run_summary(history_file=str(history), events_file=str(events))["counters"]["applied"] == 1


def test_t14_user_summary_hides_artifact_paths(menu_bot, monkeypatch):
    bot, _ = menu_bot
    summary = build_run_summary(finished_run(), [{"event": "debug_artifact", "run_id": "run-a",
        "artifact_type": "trace", "artifact_ref": "/srv/private/trace.json"}])
    monkeypatch.setattr(analytics, "get_run_summary", Mock(return_value=summary))
    text = "\n".join(dispatch(bot, "/raw_log"))
    assert "Run: run-a" in text and "/srv/private" not in text
    assert summary["artifacts"]  # Rendering must not mutate stored diagnostics.


def test_t14_raw_tail_reads_a_bounded_suffix(tmp_path, monkeypatch):
    path = tmp_path / "large.log"
    path.write_text("старые записи\n" * 10000 + "новая запись\n")
    original = open
    read_sizes = []
    class Reader:
        def __enter__(self):
            self.stream = original(path, "rb")
            return self
        def __exit__(self, *args):
            self.stream.close()
        def seek(self, *args):
            return self.stream.seek(*args)
        def tell(self):
            return self.stream.tell()
        def read(self, size=-1):
            assert 0 < size <= 14400
            read_sizes.append(size)
            return self.stream.read(size)
        def __iter__(self):
            pytest.fail("Raw log command scanned the whole journal")
    monkeypatch.setattr(ui, "open", lambda *a, **kw: Reader(), raising=False)
    assert ui._tail_text_file(str(path), lines=1) == "новая запись"
    assert sum(read_sizes) <= 14400


def test_b32_profile_conflict_hint_requires_admin_repair(menu_bot, monkeypatch):
    import hh_response_counter as counter
    bot, _ = menu_bot
    monkeypatch.setattr(counter, "refresh", Mock(side_effect=counter.HHResponseCounterProfileError("private /srv/state.json")))
    text = dispatch(bot, "/hh_responses")[-1]
    assert "другому профилю" in text and "администратора" in text and "Отклики на hh.ru" in text
    assert "Вход в hh.ru" not in text and "/srv" not in text


@pytest.mark.parametrize("stamp", ["2026-10-19 21:34:56.523609+03:00", "2026-10-19T21:34:00+03:00"])
def test_b32_counter_accepts_saved_datetime_variants(stamp):
    assert "Обновлено: 19 октября в 21:34" in ui.build_hh_response_count_text({"fetched_at": stamp})


def test_b32_bad_saved_datetime_does_not_leak_raw_value():
    text = ui.build_hh_response_count_text({"fetched_at": "private-invalid-stamp"})
    assert "Обновлено: время неизвестно" in text
    assert "private-invalid-stamp" not in text


def test_t13_checkpoint_ownership_keeps_lifecycle_precedence(tmp_path):
    from tests.direction_view_fakes import two_directions
    from state_store.private_journal import append_json
    two_directions(tmp_path)
    history, events = tmp_path / "history.jsonl", tmp_path / "events.jsonl"
    append_json(history, finished_run(search_query="QA Engineer"))
    append_json(events, {"event": "search_finished", "run_id": "run-a", "direction": "electric"})
    assert analytics.get_run_summary("run-a", history_file=str(history), events_file=str(events)) is None
