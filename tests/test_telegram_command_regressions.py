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
    texts = dispatch(bot, "/log", run_id, role="admin")
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
        assert "📊 Последний / выбранный запуск" in texts[-1]
        assert "run-a" not in texts[-1] and "/log run_id" not in texts[-1]
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
    assert "Попробуйте позже: нажмите «📨 Отклики»" in text
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
    # The compact journal now parses once for all reports. A run lookup must
    # reuse that index and still avoid ownership work on unrelated decisions.
    analytics.read_cache.journal(events).read(events)
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
    original_owner = HistoryScope.owner
    def owner(self, row):
        assert not row.get("run_id", "").startswith("unrelated-")
        return original_owner(self, row)
    monkeypatch.setattr(HistoryScope, "owner", owner)
    assert analytics.get_run_summary(history_file=str(history), events_file=str(events))["counters"]["applied"] == 1


def test_t14_user_summary_hides_artifact_paths(menu_bot, monkeypatch):
    bot, _ = menu_bot
    summary = build_run_summary(finished_run(), [{"event": "debug_artifact", "run_id": "run-a",
        "artifact_type": "trace", "artifact_ref": "/srv/private/trace.json"}])
    monkeypatch.setattr(analytics, "get_run_summary", Mock(return_value=summary))
    text = "\n".join(dispatch(bot, "/raw_log"))
    assert "📊 Последний / выбранный запуск" in text
    assert "run-a" not in text and "/log run_id" not in text and "/srv/private" not in text
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
    assert "другому профилю" in text and "администратора" in text and "📨 Отклики" in text
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


@pytest.mark.parametrize("role", [ui.ROLE_USER, ui.ROLE_ADMIN])
@pytest.mark.parametrize("stamp", ["2026-10-23T13:14:15.123456+03:00", "2026-10-23 13:14:15+03:00", "2026-10-23T13:14:15Z"])
def test_status_button_hides_raw_dates_through_real_dispatch(menu_bot, monkeypatch, role, stamp):
    import json
    import re
    from pathlib import Path

    bot, _ = menu_bot
    runtime = {"action": "search_collect", "status": "working", "mode": "search",
               "message": "Собираю вакансии", "updated_at": stamp}
    monkeypatch.setattr(bot, "_runtime_status", lambda name: runtime)
    bot_runtime = Path(bot.runtime_paths.bot_runtime_file)
    bot_runtime.parent.mkdir(parents=True, exist_ok=True)
    bot_runtime.write_text(json.dumps({**runtime, "message": "Бот работает", "updated_at": "2026-10-23T13:15:16.654321+03:00"}))
    monkeypatch.setattr(bot, "_latest_run", lambda name: {
        "created_at": "2026-10-22T12:11:10+03:00", "mode": "search", "ok": True,
        "found": 9, "applied": 2, "skipped": 7, "source_stats": {},
    })
    command, arg = ui._resolve_message_command(ui.BUTTON_STATUS, role)
    assert (command, arg) == ("/status", "")
    text = dispatch(bot, command, arg, role=role)[-1]
    assert not re.search(r"\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}", text)
    assert all(value in text for value in ("23 октября в 13:14", "23 октября в 13:15", "22 октября в 12:11",
                                           "Собираю вакансии", "Бот работает", "Найдено: 9", "Отклики: 2", "Пропущено: 7"))


@pytest.mark.parametrize("role", [ui.ROLE_USER, ui.ROLE_ADMIN])
def test_resume_picker_button_never_uses_internal_id_as_missing_title(menu_bot, monkeypatch, role):
    import telegram_bot

    bot, _ = menu_bot
    # Synthetic identities only: the displayed ordinal is separate from the HH identity.
    catalog = [{"id": "910000011", "title": ""}, {"id": "910000012"},
               {"id": "910000013", "title": "Электрик, техник"}]
    monkeypatch.setattr(telegram_bot.client_hh_auth, "load_hh_resume_catalog", lambda name: catalog)
    command, arg = ui._resolve_message_command(ui.BUTTON_SEARCH_RESUME, role)
    assert (command, arg) == ("/search_resume", "")
    text = dispatch(bot, command, arg, role=role)[-1]
    assert all(row["id"] not in text for row in catalog)
    assert "1. Резюме без названия" in text and "2. Резюме без названия" in text
    assert "3. Электрик, техник" in text
    pending = bot._search_settings_state(42)
    assert pending["search_input"] == "resume" and pending["search_input_profile"] == "qa"
    assert catalog[0]["id"] == "910000011"  # Presentation does not replace the stored selection identity.


@pytest.mark.parametrize("role", [ui.ROLE_USER, ui.ROLE_ADMIN])
@pytest.mark.parametrize("mode,expected", [("confirm", "Подтверждать отклик"), ("auto", "Автоматически")])
def test_application_mode_button_and_screen_use_the_same_terms(menu_bot, monkeypatch, role, mode, expected):
    import profile as profile_mod

    bot, _ = menu_bot
    profile = bot._profile("qa")
    profile.hh.application_mode = mode
    updates = Mock()
    monkeypatch.setattr(profile_mod, "update_profile_env", updates)
    command, arg = ui._resolve_message_command(ui.BUTTON_SEARCH_APPLICATION_MODE, role)
    assert (command, arg) == ("/search_application_mode", "")
    text = next(text for text in dispatch(bot, command, arg, role=role) if text.startswith("🛡 Режим откликов"))
    assert f"Сейчас: {expected}" in text
    assert "✋ Подтверждать отклик —" in text and "⚡ Автоматически —" in text
    assert "Спрашивать меня" not in text and "автоотклик" not in text.casefold()
    buttons = [b for row in bot._send_text.call_args.kwargs["reply_markup"]["inline_keyboard"] for b in row]
    marked = [button for button in buttons if button["text"].startswith("✓ ")]
    assert len(marked) == 1 and expected in marked[0]["text"]
    assert marked[0]["callback_data"].endswith(":search_application_" + mode)
    assert profile.hh.application_mode == mode
    updates.assert_not_called()


@pytest.mark.parametrize("role", [ui.ROLE_USER, ui.ROLE_ADMIN])
@pytest.mark.parametrize("has_run", [False, True])
def test_search_history_button_and_screen_use_the_same_terms(menu_bot, monkeypatch, role, has_run):
    bot, _ = menu_bot
    runs = [{"created_at": "2026-10-23T13:14:00+03:00", "mode": "search", "ok": True,
             "found": 9, "applied": 2, "skipped": 7, "source_stats": {}}] if has_run else []
    monkeypatch.setattr(bot, "_recent_runs", lambda *args, **kwargs: runs)
    command, arg = ui._resolve_message_command(ui.BUTTON_RUNS, role)
    assert (command, arg) == ("/runs", "") and ui.BUTTON_RUNS == "🕓 Поиски"
    text = dispatch(bot, command, arg, role=role)[-1]
    assert "🕓 Поиски ·" in text
    assert ("Последние поиски" if has_run else "История поисков пока пустая") in text
    assert "прогон" not in text.casefold()
    if has_run:
        assert "23 октября в 13:14" in text and "2026-10-23" not in text
