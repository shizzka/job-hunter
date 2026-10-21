"""F15 synthetic histories: two professions share an account and vacancy ids."""
import asyncio
import json
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import analytics
import config
import direction_runtime
import direction_stats
import directions
import evaluation_cache
import hh_resume_pipeline
import hiring_research
import manual_apply_queue
import seen
import seen_reset
import telegram_bot
from chat_observation import ChatJournal
from direction_history import ALL, UNSPECIFIED, HistoryScope
from state_store.matcher_deferred import MatcherDeferredQueue
from state_store.private_journal import read_json_records
from telegram_app import history
from tests.direction_view_fakes import make_direction, two_directions, write_rows


def write(path, rows):
    path.write_text("\n".join(json.dumps(row, ensure_ascii=False) for row in rows) + "\n")


@pytest.fixture
def account(tmp_path, monkeypatch):
    two_directions(tmp_path)
    monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))
    for key, file in (("SEEN_VACANCIES_FILE", "seen_vacancies.json"),
                      ("ANALYTICS_EVENTS_FILE", "analytics_events.jsonl"),
                      ("ANALYTICS_STATE_FILE", "analytics_state.json"),
                      ("RUN_HISTORY_FILE", "run_history.jsonl"),
                      ("HH_RESUME_PIPELINE_FILE", "hh_resume_pipeline.json")):
        monkeypatch.setattr(config, key, str(tmp_path / file))
    monkeypatch.setattr(config, "ANALYTICS_ENABLED", True)
    monkeypatch.setattr(manual_apply_queue, "_queue_path", lambda profile_name=None: tmp_path / "manual_apply_queue.json")
    def select(name):
        directions.set_active_direction(tmp_path, name)
        monkeypatch.setattr(config, "ACTIVE_DIRECTION", directions.active_direction(tmp_path), raising=False)
    select("qa")
    return tmp_path, select


def decision(name, vid="1", **extra):
    return {"event": "decision", "source": "hh", "vacancy_id": vid, "direction": name,
            "decision": "applied_auto", "outcome": "applied", "created_at": datetime.now().isoformat(), **extra}


def test_legacy_evidence_is_unique_and_never_profession_guessing(account):
    home, _ = account
    view = HistoryScope(home)
    qa = directions.load_direction(home, "qa")
    rows = [{"resume_id": "resume_111"}, {"resumeId": 222},
            {"matcher_resume_sha256": qa["variants"][0]["text_sha256"]},
            {"search_query": " qa ENGINEER "}, {"query": "Электромонтажник"},
            {"title": "QA Engineer"}, {"direction": "qa", "resume_id": "resume_222"}]
    assert [view.owner(row) for row in rows] == ["qa", "electric", "qa", "qa", "electric", None, None]
    assert view.metadata({"resume_id": "222"})["hh_resume_hash"] == "resume_222"
    assert view.metadata(rows[2])["hh_resume_hash"] == "resume_111"
    value = directions.load_direction(home, "electric")
    value["search_queries"].append("QA Engineer")
    directions.save_direction(home, value)
    assert HistoryScope(home).owner({"search_query": "QA Engineer"}) is None
    assert HistoryScope(home, UNSPECIFIED).records([rows[0], rows[-2]]) == [rows[-2]]


def test_seen_verdicts_are_direction_local_but_delivery_guards_are_shared(account):
    home, select = account
    vacancy = {"id": "9", "source": "hh", "title": "Synthetic"}
    seen.mark_seen("9", vacancy, "skipped_low_score")
    assert seen.is_seen("9")
    select("electric")
    assert not seen.is_seen("9")
    seen.mark_seen("9", vacancy, "applied")
    select("qa")
    assert seen.is_seen("9")
    assert seen.stats()["applied"] == 0  # the electrician's application is not QA history
    raw = seen.raw_entries()
    assert len(raw) == 2 and {r["direction"] for r in raw.values()} == {"qa", "electric"}
    assert all(r["hh_resume_hash"] and r["hh_resume_numeric_id"] for r in raw.values())
    seen.mark_seen("10", vacancy, "apply_uncertain")
    select("electric")
    assert seen.is_seen("10")
    with pytest.raises(RuntimeError):
        seen.mark_seen("10", vacancy, "skipped_low_score")
    assert seen.stats_from_file(str(home / "seen_vacancies.json"), scope=ALL)["total"] == 3


def test_legacy_seen_query_recovers_owner_and_unknown_does_not_suppress(account):
    home, select = account
    (home / "seen_vacancies.json").write_text(json.dumps({
        "1": {"action": "skipped_low_score", "search_query": "QA Engineer"},
        "2": {"action": "skipped_low_score", "title": "QA Engineer"},
        "3": {"action": "applied"}}))
    assert seen.is_seen("1") and not seen.is_seen("2") and seen.is_seen("3")
    select("electric")
    assert not seen.is_seen("1") and not seen.is_seen("2") and seen.is_seen("3")
    assert seen.stats_from_file(str(home / "seen_vacancies.json"), scope=UNSPECIFIED)["total"] == 2


def test_evaluation_and_deferred_caches_do_not_cross_directions_or_resume_versions(account):
    home, select = account
    vacancy = {"id": "5", "source": "hh", "title": "Synthetic"}
    qa_queue = MatcherDeferredQueue(home, clock=lambda: 100)
    qa_queue.defer(vacancy, "details", "llm_error")
    evaluation_cache.store(vacancy, {"score": 5})
    assert evaluation_cache.lookup(vacancy)["score"] == 5
    select("electric")
    assert evaluation_cache.lookup(vacancy) is None
    electric_queue = MatcherDeferredQueue(home, clock=lambda: 101)
    assert electric_queue.merge_ready([vacancy], {"hh"}) == [vacancy]
    evaluation_cache.store(vacancy, {"score": 95})
    select("qa")
    assert evaluation_cache.lookup(vacancy)["score"] == 5
    value = directions.active_direction(home)
    value = directions.save_direction(home, value, texts={"resume_111": "Changed synthetic resume"})
    select("qa")
    assert evaluation_cache.lookup(vacancy) is None
    assert qa_queue.path != electric_queue.path
    data = json.loads((home / evaluation_cache.CACHE_FILE).read_text())
    assert {r["direction"] for r in data.values()} == {"qa", "electric"}
    assert all(r["hh_resume_hash"] for r in data.values())


def test_legacy_deferred_work_is_recovered_only_for_its_known_direction(account):
    home, select = account
    directions.set_active_direction(home, None)
    legacy = MatcherDeferredQueue(home, clock=lambda: 100)
    legacy.defer({"id": "1", "source": "hh", "_search_query": "QA Engineer"}, "details", "llm_error")
    legacy.defer({"id": "2", "source": "hh", "title": "QA Engineer"}, "details", "llm_error")
    select("electric")
    assert MatcherDeferredQueue(home, clock=lambda: 500).merge_ready([], {"hh"}) == []
    select("qa")
    queue = MatcherDeferredQueue(home, clock=lambda: 500)
    vacancy, = queue.merge_ready([], {"hh"})
    assert vacancy["id"] == "1"
    queue.resolve(vacancy)
    assert queue.merge_ready([], {"hh"}) == []
    assert len(legacy.store.load()["items"]) == 1  # the unknown record is preserved


def test_decision_evaluation_feedback_and_backfill_keep_their_owner(account, monkeypatch):
    home, select = account
    vacancy = {"id": "6", "source": "hh", "_search_query": "QA Engineer"}
    analytics.record_decision(run_id="qa-run", vacancy=vacancy, decision="skipped_keyword_filter")
    record = read_json_records(config.ANALYTICS_EVENTS_FILE)[-1]
    assert (record["direction"], record["hh_resume_hash"], record["hh_resume_numeric_id"]) == ("qa", "resume_111", 111)
    direction_runtime.prepare(vacancy)
    item = manual_apply_queue.create_candidate(vacancy, {"score": 5})
    select("electric")
    manual_apply_queue.record_feedback(item["token"], "bad")
    record = read_json_records(home / "feedback_log.jsonl")[-1]
    assert (record["direction"], record["hh_resume_hash"], record["hh_resume_numeric_id"]) == ("qa", "resume_111", 111)
    entries = {"old-qa": {"vacancy_id": "7", "action": "applied", "resume_id": "resume_111"},
               "old-unknown": {"vacancy_id": "8", "action": "applied"}}
    assert analytics.backfill_seen_decisions(entries)["added"] == 2
    assert analytics.backfill_seen_decisions(entries)["added"] == 0
    rows = [r for r in read_json_records(config.ANALYTICS_EVENTS_FILE) if r.get("historical")]
    assert [r["direction"] for r in rows] == ["qa", None]
    assert analytics.summarize(all_time=True)["auto_applied"] == 0


def test_pre_match_cards_use_recorded_direction_even_with_shared_queries(account):
    from direction_views import DirectionView
    home, select = account
    for name in ("qa", "electric"):
        value = directions.load_direction(home, name)
        value["search_queries"] = ["Общий запрос"]
        directions.save_direction(home, value)
    select("qa")
    item = manual_apply_queue.create_candidate({"id": "early", "_search_query": "Общий запрос"}, {"score": None})
    assert DirectionView(home).review_items([item]) == [item]
    select("electric")
    assert DirectionView(home).review_items([item]) == []


def test_summaries_and_legacy_status_join_use_application_resume(account):
    home, select = account
    rows = [decision("qa", run_id="q"), decision("electric", run_id="e"),
            {**decision(None, "2"), "resume_id": "resume_111"}, decision(None, "3"),
            {"event": "negotiation_status", "source": "hh", "vacancy_id": "1", "direction": "qa",
             "status_bucket": "rejected", "created_at": datetime.now().isoformat()},
            {"event": "negotiation_status", "source": "hh", "vacancy_id": "1", "direction": "electric",
             "status_bucket": "positive", "created_at": datetime.now().isoformat()}]
    write(home / "analytics_events.jsonl", rows)
    qa = analytics.summarize(all_time=True)
    assert qa["auto_applied"] == 2 and qa["rejected_statuses"] == 1 and qa["positive_statuses"] == 0
    assert qa["direction_title"] == "QA Engineer"
    research = hiring_research.summarize(str(home / "analytics_events.jsonl"))
    assert research["total"] == 2 and research["outcomes"]["rejected"] == 1
    select("electric")
    electric = analytics.summarize(all_time=True)
    assert electric["auto_applied"] == 1 and electric["positive_statuses"] == 1 and electric["rejected_statuses"] == 0
    total = analytics.summarize(all_time=True, scope=ALL)
    assert total["auto_applied"] == 4 and total["by_direction"]["направление не указано"] == 1
    assert hiring_research.summarize(str(home / "analytics_events.jsonl"), scope=ALL)["total"] == 4
    assert analytics.summarize(all_time=True, scope=UNSPECIFIED)["auto_applied"] == 1
    write_rows(home, [{"id": 1, "vacancyId": "1", "resumeId": 222}])
    analytics.record_negotiation_statuses([{"id": "hh:1", "status": "Приглашение"}])
    observed = read_json_records(config.ANALYTICS_EVENTS_FILE)[-1]
    assert observed["direction"] == "electric" and observed["hh_resume_numeric_id"] == 222
    assert observed["hh_resume_hash"] == "resume_222"
    assert observed["direction_title"] == "Электромонтажник"
    select("qa")
    analytics.record_negotiation_statuses([{"id": "hh:1", "status": "Приглашение"}])
    assert read_json_records(config.ANALYTICS_EVENTS_FILE)[-1]["direction"] == "electric"


def test_runs_do_not_borrow_another_direction_or_ambiguous_aggregate(account):
    home, select = account
    runs = []
    events = []
    for index, name in enumerate(("qa", "electric", None)):
        run = {"run_id": f"run-{index}", "kind": "search", "started_at": f"2026-10-20T1{index}:00:00",
               "finished_at": f"2026-10-20T1{index}:01:00", "ok": True, "applied": 1}
        if name:
            run["direction"] = name
        runs.append(run)
        events.append({**run, "event": "search_finished", "created_at": run["finished_at"]})
    # Old aggregate with just one known and one unknown decision stays unspecified.
    events += [decision("qa", "11", run_id="run-2"), decision(None, "12", run_id="run-2")]
    write(home / "run_history.jsonl", runs)
    write(home / "analytics_events.jsonl", events)
    assert [r["run_id"] for r in analytics.scoped_runs(runs)] == ["run-0"]
    assert analytics.get_run_summary()["run_id"] == "run-0"
    assert analytics.get_run_summary("run-1") is None
    select("electric")
    assert analytics.get_run_summary()["run_id"] == "run-1"
    assert analytics.get_run_summary(scope=UNSPECIFIED)["run_id"] == "run-2"
    assert len(analytics.scoped_runs(runs, scope=ALL)) == 3


@pytest.mark.parametrize("scope,expected", [("active", ["qa"]), (ALL, ["unknown", "electric", "qa"]),
                                           (UNSPECIFIED, ["unknown"])])
def test_run_readers_skip_malformed_records_without_rewriting_history_or_journals(account, monkeypatch, scope, expected):
    import reporting

    home, _ = account
    malformed = [None, "old", [], [1], 7, 2.5, True]
    runs = [{"run_id": name or "unknown", "direction": name, "kind": "search", "ok": True, "applied": 1}
            for name in ("qa", "electric", None)]
    rows = malformed + runs + malformed
    paths = [home / "run_history.jsonl", home / "analytics_events.jsonl", home / "negotiation_snapshots.jsonl"]
    write(paths[0], rows)
    write(paths[1], malformed + [{**r, "event": "search_finished"} for r in runs] + malformed)
    write(paths[2], malformed)
    for path in paths:
        with path.open("a") as stream:
            stream.write('{"truncated":\n')
    original = {path: path.read_bytes() for path in paths}
    bot = telegram_bot.TelegramBot.__new__(telegram_bot.TelegramBot)
    monkeypatch.setattr(bot, "_profile", lambda name: SimpleNamespace(
        run_history_file=str(paths[0]), analytics_events_file=str(paths[1])))

    assert [r["run_id"] for r in reversed(analytics.scoped_runs(rows, scope=scope))] == expected
    assert [r["run_id"] for r in bot._recent_runs("synthetic", scope=scope)] == expected
    assert analytics.get_run_summary(scope=scope)["run_id"] == expected[0]
    assert [r["run_id"] for r in reporting.load_recent_run_history()] == ["qa"]
    assert {path: path.read_bytes() for path in paths} == original


def test_direction_reset_and_hh_attempts_are_separate(account):
    home, select = account
    for name in ("qa", "electric"):
        select(name)
        vacancy = {"id": "9", "source": "hh"}
        seen.mark_seen("9", vacancy, "skipped_low_score")
        evaluation_cache.store(vacancy, {"score": 5})
        hh_resume_pipeline.record_successful_apply(vacancy, {"name": "one", "id": f"resume_{111 if name == 'qa' else 222}"})
    assert hh_resume_pipeline.all_entries()["9"]["direction"] == "electric"
    assert {r["direction"] for r in hh_resume_pipeline.history_entries()} == {"qa", "electric"}
    select("qa")
    assert hh_resume_pipeline.all_entries()["9"]["direction"] == "qa"
    assert len(seen_reset.plan(str(home), "all")) == 1
    assert seen_reset.apply(str(home), "all") == 1
    assert evaluation_cache.lookup(vacancy) is None
    select("electric")
    assert seen.is_seen("9") and evaluation_cache.lookup(vacancy)["score"] == 5


def test_last_run_reset_uses_this_directions_last_run(account):
    from datetime import timedelta
    home, select = account
    now = datetime.now()
    seen.mark_seen("1", {"id": "1"}, "skipped_low_score")
    data = seen.raw_entries()
    next(iter(data.values()))["date"] = (now - timedelta(hours=2)).isoformat()
    (home / "seen_vacancies.json").write_text(json.dumps(data))
    write(home / "run_history.jsonl", [
        {"direction": "qa", "started_at": (now - timedelta(hours=3)).isoformat(), "finished_at": now.isoformat()},
        {"direction": "electric", "started_at": (now - timedelta(hours=1)).isoformat(), "finished_at": now.isoformat()}])
    assert len(seen_reset.plan(str(home), "last_run")) == 1


def test_chat_journal_uses_b22_owner_even_when_another_direction_is_active(account):
    home, select = account
    write_rows(home, [{"id": 1, "chatId": 101, "resumeId": 111}, {"id": 2, "chatId": 202, "resumeId": 222}])
    journal = ChatJournal(home, "synthetic")
    journal.write("chat_decision", chat_id="101", decision="waiting")
    journal.write("chat_decision", chat_id="202", decision="waiting")
    journal.write("chat_decision", chat_id="303", decision="waiting")
    rows = read_json_records(journal.path)
    assert [r["direction"] for r in rows] == ["qa", "electric", None]
    text = history.chat_text(home, HistoryScope(home))
    assert "чат 101" in text and "чат 202" not in text and "чат 303" not in text
    text = history.chat_text(home, HistoryScope(home, UNSPECIFIED))
    assert "чат 303" in text and "чат 101" not in text


@pytest.mark.parametrize("command", ["/stats", "/digest", "/research", "/runs", "/log", "/chat_log", "/direction_stats", "/seen_reset"])
def test_history_screens_name_active_direction_and_offer_totals(account, monkeypatch, command):
    home, select = account
    profile = SimpleNamespace(home_dir=str(home), analytics_events_file=config.ANALYTICS_EVENTS_FILE,
        run_history_file=config.RUN_HISTORY_FILE, seen_file=config.SEEN_VACANCIES_FILE, log_file=str(home / "job-hunter.log"))
    bot = telegram_bot.TelegramBot("synthetic")
    monkeypatch.setattr(bot, "_profile", lambda _: profile)
    monkeypatch.setattr(bot, "_profile_names", lambda: ["synthetic"])
    monkeypatch.setattr(bot, "_selected_profile", lambda _: "synthetic")
    monkeypatch.setattr(bot, "_set_selected_menu", lambda *args: None)
    monkeypatch.setattr(bot, "_send_text", AsyncMock())
    monkeypatch.setattr(bot, "_menu_reply_markup", lambda *args, **kwargs: {})
    principal = {"user_id": 1, "role": "admin"}
    asyncio.run(bot._dispatch(1, principal, command, ""))
    text = "\n".join(c.args[1] for c in bot._send_text.await_args_list)
    buttons = str([c.kwargs for c in bot._send_text.await_args_list])
    assert "QA Engineer" in text and "Электромонтажник" not in text
    assert "Все направления" in buttons and "Направление не указано" in buttons
    if command != "/seen_reset":
        bot._send_text.reset_mock()
        asyncio.run(bot._dispatch(1, principal, command, "all"))
        assert "все направления" in "\n".join(c.args[1] for c in bot._send_text.await_args_list)


def test_single_direction_with_identified_history_keeps_counts(tmp_path):
    make_direction(tmp_path)
    directions.set_active_direction(tmp_path, "qa")
    write(tmp_path / "analytics_events.jsonl", [decision(None, resume_id="resume_111")])
    summary = analytics.summarize(events_file=str(tmp_path / "analytics_events.jsonl"), all_time=True)
    assert summary["auto_applied"] == 1 and summary["direction_title"] == "QA Engineer"
    result = direction_stats.scoped(direction_stats.summarize(tmp_path, [{"id": 1, "resumeId": 111, "lastState": "RESPONSE"}]), tmp_path)
    assert result["total_topics"] == 1 and result["rows"][0]["applied"] == 1


def test_uncertain_decision_does_not_hide_another_directions_application(account):
    home, select = account
    write(home / "analytics_events.jsonl", [
        decision("qa", run_id="legacy", decision="apply_uncertain"),
        decision("electric", run_id="legacy")])
    report = analytics.summarize(all_time=True, scope=ALL)
    assert report["auto_applied"] == 1 and report["uncertain"] == 1


def test_recovered_legacy_application_joins_new_owned_status(account):
    home, _ = account
    write(home / "analytics_events.jsonl", [
        decision(None, resume_id="resume_111"),
        {"event": "negotiation_status", "source": "hh", "vacancy_id": "1", "direction": "qa",
         "created_at": datetime.now().isoformat(), "status_bucket": "positive"}])
    report = analytics.summarize(all_time=True)
    assert report["funnel"]["positive"] == 1
    research = hiring_research.summarize(str(home / "analytics_events.jsonl"))
    assert research["total"] == 1 and research["outcomes"]["positive"] == 1


def test_old_chat_text_is_grouped_by_b22_or_left_unspecified(account):
    home, _ = account
    write_rows(home, [{"id": 1, "chatId": 101, "resumeId": 111}, {"id": 2, "chatId": 202, "resumeId": 222}])
    text = "chat=101 QA_MARKER\nchat=202 ELECTRIC_MARKER\nNO_OWNER_MARKER"
    selected = history.chat_text(home, HistoryScope(home), text)
    unknown = history.chat_text(home, HistoryScope(home, UNSPECIFIED), text)
    assert "QA_MARKER" in selected and "ELECTRIC_MARKER" not in selected and "NO_OWNER_MARKER" not in selected
    assert "NO_OWNER_MARKER" in unknown and "QA_MARKER" not in unknown


def test_history_and_reset_buttons_cannot_follow_a_direction_switch(account, monkeypatch):
    from tests.test_telegram_callbacks import _callback, _configured_bot
    home, select = account
    bot, calls = _configured_bot(monkeypatch)
    monkeypatch.setattr(bot, "_profile", lambda _: SimpleNamespace(home_dir=str(home)))
    dispatch = AsyncMock()
    monkeypatch.setattr(bot, "_dispatch", dispatch)
    old = history.markup(home, "/stats")["inline_keyboard"][1][0]["callback_data"]
    select("electric")
    asyncio.run(bot._handle_callback_query(_callback(old)))
    assert dispatch.await_count == 0 and "Направление изменилось" in calls["messages"][-1][1]
    fresh = history.markup(home, "/stats")["inline_keyboard"][1][0]["callback_data"]
    asyncio.run(bot._handle_callback_query(_callback(fresh)))
    assert dispatch.await_args.args[2:] == ("/stats", "all")
    # Reset's final confirmation is checked by dispatch, not by the reader router.
    bot = telegram_bot.TelegramBot("synthetic")
    monkeypatch.setattr(bot, "_profile", lambda _: SimpleNamespace(home_dir=str(home)))
    monkeypatch.setattr(bot, "_selected_profile", lambda _: "synthetic")
    monkeypatch.setattr(bot, "_send_text", AsyncMock())
    bound = history.token(home)
    select("qa")
    seen.mark_seen("1", {"id": "1"}, "skipped_low_score")
    asyncio.run(bot._dispatch(1, {"user_id": 1, "role": "admin"}, "/seen_reset", f"do all active {bound}"))
    assert "Направление изменилось" in bot._send_text.await_args.args[1]
    assert len(seen.raw_entries()) == 1


def test_reset_period_keeps_older_evaluations(account, monkeypatch):
    import time
    home, _ = account
    now = time.time()
    monkeypatch.setattr(evaluation_cache.time, "time", lambda: now - 2 * 86400)
    evaluation_cache.store({"id": "old"}, {"score": 10})
    monkeypatch.setattr(evaluation_cache.time, "time", lambda: now)
    evaluation_cache.store({"id": "new"}, {"score": 20})
    seen_reset.apply(str(home), "day")
    assert evaluation_cache.lookup({"id": "new"}) is None
    assert evaluation_cache.lookup({"id": "old"})["score"] == 10
