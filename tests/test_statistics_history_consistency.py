"""B8/B33/B34: synthetic profiles, saved reports only, no HH traffic."""
import asyncio
import json
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import analytics
import application_history
import directions
import telegram_bot
import telegram_bot_ui as ui
from direction_history import ALL, UNSPECIFIED, HistoryScope
from telegram_app import history
from tests.direction_view_fakes import make_direction, snapshot, two_directions, write_rows
from tests.test_telegram_menu_routing import menu_bot


def write(path, rows):
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def applied(vid, **extra):
    return {"event": "decision", "decision": "applied_auto", "source": "hh",
            "vacancy_id": str(vid), "created_at": "2026-10-20T12:00:00+03:00", **extra}


def bot_for(home, monkeypatch):
    profile = SimpleNamespace(home_dir=str(home), analytics_events_file=str(home / "analytics_events.jsonl"),
        seen_file=str(home / "seen_vacancies.json"), run_history_file=str(home / "run_history.jsonl"))
    bot = telegram_bot.TelegramBot("synthetic")
    monkeypatch.setattr(bot, "_profile", lambda name: profile)
    monkeypatch.setattr(bot, "_profile_names", lambda: ["synthetic"])
    return bot


def test_b8_single_direction_recovers_28_applications_not_140_status_polls(tmp_path):
    make_direction(tmp_path)
    directions.set_active_direction(tmp_path, "qa")
    rows = [applied(i, run_id="old") for i in range(28)]
    rows += [{**applied(i, run_id="old"), "event": "application_result", "outcome": "sent"}
             for i in range(5)]
    rows += [{"event": "negotiation_status", "source": "hh", "vacancy_id": str(i % 28),
              "status_bucket": "pending", "created_at": "2026-10-20T13:00:00+03:00"} for i in range(140)]
    write(tmp_path / "analytics_events.jsonl", rows)
    summary = analytics.summarize(events_file=str(tmp_path / "analytics_events.jsonl"), all_time=True)
    assert summary["auto_applied"] == 28
    assert summary["events"] == 173
    assert len(analytics.history_events(str(tmp_path / "analytics_events.jsonl"))) == 173
    assert application_history.summarize(tmp_path)["count"] == 28


def test_b33_history_explains_102_applications_without_search_records(tmp_path, monkeypatch):
    make_direction(tmp_path)
    directions.set_active_direction(tmp_path, "qa")
    (tmp_path / "seen_vacancies.json").write_text(json.dumps(
        {str(i): {"action": "applied", "title": f"Вакансия {i}"} for i in range(102)}))
    bot = bot_for(tmp_path, monkeypatch)
    _, texts, _ = history._history_report(bot, {"role": "user"}, "synthetic", "/runs", "")
    text = "\n".join(texts)
    assert "Сохранено откликов за всё время: 102" in text
    assert "подробные записи поисков не сохранились" in text
    assert "История поисков пока пустая" not in text
    stats = ui.build_stats_text(**bot._stats_snapshot("synthetic"))
    assert "• Отправлено: 102" in stats
    assert "Сохранено откликов за всё время: 102" in stats


def test_b34_legacy_bucket_has_one_plain_name(tmp_path):
    two_directions(tmp_path)
    view = HistoryScope(tmp_path, UNSPECIFIED)
    assert view.title == "Отклики до появления направлений"
    assert "Отклики до появления направлений" in str(history.markup(tmp_path, "/stats"))


def test_hh_parser_does_not_cap_active_at_100():
    from hh_response_counter import parse_negotiations_page
    page = ('"total":102,"readOnlyInterval":0,"pageCount":6,"filterInUse":"active" '
            '"applicantNegotiationsCounters":{"total":{"deleted":2}}')
    assert parse_negotiations_page(page, expected_filter="active")["total"] == 102


def test_b34_100_active_vs_102_saved_is_explained_in_all_screens(tmp_path, monkeypatch):
    make_direction(tmp_path)
    directions.set_active_direction(tmp_path, "qa")
    write(tmp_path / "analytics_events.jsonl", [applied(i) for i in range(102)])
    write_rows(tmp_path, snapshot("a", "active", [111] * 100))
    bot = bot_for(tmp_path, monkeypatch)
    stats = ui.build_stats_text(**bot._stats_snapshot("synthetic"))
    _, texts, _ = history._history_report(bot, {"role": "user"}, "synthetic", "/runs", "")
    hh = ui.build_hh_response_count_text({"profile": "synthetic", "active": 100,
        "archived": 0, "deleted": 0, "total": 100}, home_dir=str(tmp_path))
    for text in (stats, "\n".join(texts), hh):
        assert "Сохранено откликов за всё время: 102" in text
    assert "• Активные: 100" in hh
    assert "текущий список" in hh and "архив и удалённые считаются отдельно" in hh
    assert "не больше 100" not in hh  # A synthetic fixture cannot establish a HH limit.


def test_legacy_split_uses_resume_evidence_and_keeps_ambiguity_in_profile_total(tmp_path):
    two_directions(tmp_path)
    value = directions.load_direction(tmp_path, "qa")
    digest = value["variants"][0]["text_sha256"]
    rows = [applied(1, resume_id="resume_111"), applied(2, resumeId=222),
            applied(3, matcher_resume_sha256=digest), applied(4),
            applied(5, direction="qa", resumeId=222)]
    write(tmp_path / "analytics_events.jsonl", rows)
    before = {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.rglob("*") if p.is_file()}
    qa = application_history.summarize(tmp_path)
    other = application_history.summarize(tmp_path, scope="electric")
    legacy = application_history.summarize(tmp_path, scope=UNSPECIFIED)
    all_rows = application_history.summarize(tmp_path, scope=ALL)
    assert (qa["count"], other["count"], legacy["count"], all_rows["count"]) == (2, 1, 2, 5)
    assert qa["profile_total"] == 5 and qa["legacy_total"] == 2
    assert qa["count"] + other["count"] + legacy["count"] == all_rows["count"]
    assert before == {p: (p.read_bytes(), p.stat().st_mtime_ns) for p in tmp_path.rglob("*") if p.is_file()}
    directions.set_active_direction(tmp_path, "electric")
    assert application_history.summarize(tmp_path)["count"] == 1
    assert application_history.summarize(tmp_path, scope=UNSPECIFIED)["count"] == 2


def test_sent_receipts_seen_marks_and_repeated_polls_count_once_but_retries_count(tmp_path):
    make_direction(tmp_path)
    rows = [applied(1, run_id="one"), applied(1, run_id="two"),
            {**applied(1, run_id="one"), "event": "application_result", "outcome": "sent"},
            {"event": "application_result", "outcome": "sent", "run_id": "three", "application_id": "receipt",
             "vacancy_id": "2", "source": "hh"},
            {"event": "application_result", "outcome": "sent", "run_id": "three", "application_id": "receipt",
             "source": "hh"},
            applied(3, run_id="dry", mode="dry-run"),
            applied(4, run_id="uncertain"),
            {**applied(4, run_id="uncertain"), "decision": "apply_uncertain"}]
    write(tmp_path / "analytics_events.jsonl", rows)
    (tmp_path / "seen_vacancies.json").write_text(json.dumps({"hh:1": {"action": "applied"},
        "hh:4": {"action": "applied"}, "hh:5": {"action": "manual_ai_applied"},
        "hh:6": {"action": "already_applied"}}))
    assert application_history.summarize(tmp_path)["count"] == 4


def test_reports_never_mix_profiles_even_with_same_direction_and_vacancy_ids(tmp_path, monkeypatch):
    homes = [tmp_path / "client", tmp_path / "owner"]
    profiles = {}
    for index, home in enumerate(homes):
        home.mkdir()
        make_direction(home)
        write(home / "analytics_events.jsonl", [applied(i, title=f"Профиль {index}") for i in range(index + 1)])
        profiles[str(index)] = SimpleNamespace(home_dir=str(home), analytics_events_file=str(home / "analytics_events.jsonl"),
            seen_file=str(home / "seen_vacancies.json"), run_history_file=str(home / "run_history.jsonl"))
    bot = telegram_bot.TelegramBot("synthetic")
    monkeypatch.setattr(bot, "_profile", profiles.__getitem__)
    monkeypatch.setattr(bot, "_profile_names", lambda: ["0", "1"])
    for index in range(2):
        _, texts, _ = history._history_report(bot, {"role": "admin"}, str(index), "/stats", "all")
        # the admin overview has one block per profile, each with only its own count
        blocks = re.split(r"\n(?:📁|✅) ", texts[0])[1:]
        assert len(blocks) == 2
        for block in blocks:
            own = 1 if block.startswith("Профиль 0") else 2
            assert f"Сохранено откликов за всё время: {own}" in block
            assert f"Сохранено откликов за всё время: {3 - own}" not in block
        assert application_history.summarize(homes[index])["profile_total"] == index + 1


def test_scoped_tester_does_not_see_other_direction_or_profile_totals(tmp_path, monkeypatch):
    two_directions(tmp_path)
    write(tmp_path / "analytics_events.jsonl", [applied(1, direction="qa"), applied(2, direction="electric"), applied(3)])
    monkeypatch.setenv("JOB_HUNTER_SCOPED_DIRECTION", "qa")
    summary = application_history.summarize(tmp_path)
    assert summary["count"] == summary["profile_total"] == 1
    assert summary["legacy_total"] == 0
    assert "Всего в профиле" not in application_history.totals_text(summary)
    with pytest.raises(PermissionError):
        application_history.summarize(tmp_path, scope=ALL)


def test_unreadable_seen_history_is_explicit_and_does_not_hide_hh_counter(tmp_path):
    (tmp_path / "seen_vacancies.json").write_text('{"truncated":')
    text = ui.build_hh_response_count_text({"active": 100, "total": 100}, home_dir=str(tmp_path))
    assert "Активные: 100" in text
    assert "Часть сохранённой истории не удалось прочитать" in text
    assert "Сохранено откликов за всё время: не меньше 0" in text


def test_offline_dispatch_smoke_stats_history_hh_agree(menu_bot, tmp_path, monkeypatch):
    from telegram_app import hh_counter_cache
    bot, _ = menu_bot
    make_direction(tmp_path)
    directions.set_active_direction(tmp_path, "qa")
    write(tmp_path / "analytics_events.jsonl", [applied(i) for i in range(102)])
    write_rows(tmp_path, snapshot("active", "active", [111] * 100))
    counter = AsyncMock(return_value=({"profile": "qa", "active": 100,
        "archived": 2, "deleted": 0, "total": 102}, 0, None))
    monkeypatch.setattr(hh_counter_cache, "snapshot", counter)
    principal = {"user_id": 42, "role": "user", "profile": "qa"}
    for label in (ui.BUTTON_STATS, ui.BUTTON_RUNS, ui.BUTTON_HH_RESPONSES):
        bot._send_text.reset_mock()
        command, arg = ui._resolve_message_command(label, "user")
        asyncio.run(bot._dispatch(42, principal, command, arg))
        text = "\n".join(call.args[1] for call in bot._send_text.call_args_list)
        assert "Сохранено откликов за всё время: 102" in text
        assert "История поисков пока пустая" not in text
    counter.assert_awaited_once()


def test_shared_resume_is_ambiguous_and_same_vacancy_can_belong_to_two_directions(tmp_path):
    two_directions(tmp_path)
    other = directions.load_direction(tmp_path, "electric")
    other["variants"] = [{"hh_resume_hash": "resume_111", "hh_resume_numeric_id": 111, "title": "Общее резюме"}]
    directions.save_direction(tmp_path, other, texts={"resume_111": "Synthetic shared resume"})
    write(tmp_path / "analytics_events.jsonl", [applied(1, resume_id="resume_111"),
        applied(2, direction="qa"), applied(2, direction="electric")])
    assert application_history.summarize(tmp_path)["count"] == 1
    assert application_history.summarize(tmp_path, scope="electric")["count"] == 1
    assert application_history.summarize(tmp_path, scope=UNSPECIFIED)["count"] == 1
    assert application_history.summarize(tmp_path, scope=ALL)["count"] == 3


def test_anonymous_seen_mark_does_not_duplicate_a_uniquely_identified_send(tmp_path):
    two_directions(tmp_path)
    write(tmp_path / "analytics_events.jsonl", [applied(1, resume_id="resume_111"), applied(2, resumeId=222)])
    (tmp_path / "seen_vacancies.json").write_text(json.dumps({"1": {"action": "applied"},
        "2": {"action": "applied"}, "3": {"action": "applied"}}))
    assert application_history.summarize(tmp_path, scope=ALL)["count"] == 3
    assert application_history.summarize(tmp_path, scope=UNSPECIFIED)["count"] == 1


def test_legacy_decision_and_owned_receipt_for_same_run_and_vacancy_are_one_send(tmp_path):
    two_directions(tmp_path)
    write(tmp_path / "analytics_events.jsonl", [applied(1, run_id="one"),
        {**applied(1, run_id="one"), "event": "application_result", "outcome": "sent", "resumeId": 111}])
    assert application_history.summarize(tmp_path)["count"] == 1
    assert application_history.summarize(tmp_path, scope=ALL)["count"] == 1
    assert application_history.summarize(tmp_path, scope=UNSPECIFIED)["count"] == 0
    assert analytics.summarize(events_file=str(tmp_path / "analytics_events.jsonl"), all_time=True)["auto_applied"] == 1
