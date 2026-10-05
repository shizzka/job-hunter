"""Offline soak diagnostics: observe the original decisions, never send/retry."""
import asyncio
import json
import logging
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import agent
import analytics
import config
import reporting
import search_pipeline
import telegram_bot_ui as ui
from private_logging import PrivateFileHandler, chat_log_path
from state_store.hh_ui import HHUIWarnings, MAX_FINGERPRINTS, MAX_RECENT_RUNS
from state_store.private_journal import read_json_records
from telegram_bot import TelegramBot

PRIVATE = "SENTINEL_RESUME_COVER_HR_ANSWER_FORM_PROMPT_COOKIE_SECRET"


@pytest.fixture
def search_home(tmp_path, monkeypatch):
    for key, value in {
        "JOB_HUNTER_HOME": str(tmp_path), "RUN_HISTORY_FILE": str(tmp_path / "runs.jsonl"),
        "RUNTIME_STATUS_FILE": str(tmp_path / "runtime.json"),
        "SEEN_VACANCIES_FILE": str(tmp_path / "seen.json"),
        "HH_STATE_DIR": str(tmp_path / "hh"), "HH_GUARD_STATE_FILE": str(tmp_path / "guard.json"),
    }.items():
        monkeypatch.setattr(config, key, value)
    return tmp_path


def test_log_channels_context_privacy_concurrency_and_rotation(tmp_path):
    search, chat = tmp_path / "job-hunter.log", tmp_path / "job-hunter-chat.log"
    handlers = [PrivateFileHandler(search, channel="search"), PrivateFileHandler(chat, channel="chat")]
    logger = logging.getLogger("shared-offline-client")
    old_level = logger.level
    logger.setLevel(logging.INFO)
    for handler in handlers:
        logger.addHandler(handler)
    async def write(channel, run_id):
        with analytics.event_context(channel=channel, run_id=run_id, source="hh", vacancy_id="123", stage="details"):
            await asyncio.sleep(0)
            logger.info("workflow channel=%s", channel)
            try:
                raise ValueError(PRIVATE)
            except ValueError as exc:
                logger.error("request failed: %s", exc, exc_info=True, stack_info=True)
            logger.warning("request https://auth.invalid/" + PRIVATE)
    async def run():
        await asyncio.gather(write("search", "search-one"), write("chat", "search-two"))
    try:
        asyncio.run(run())
        assert "channel=chat" not in search.read_text()
        assert "channel=search" not in chat.read_text()
        assert "run=search-one source=hh vacancy=123 stage=details" in search.read_text()
        assert "run=search-two source=hh vacancy=123 stage=details" in chat.read_text()
        for path in (search, chat):
            assert PRIVATE not in path.read_text()
            assert "error_kind=ValueError" in path.read_text()
            assert path.stat().st_mode & 0o777 == 0o600
        chat.rename(tmp_path / "chat.rotated")
        with analytics.event_context(channel="chat"):
            logger.info("after rotation")
        assert "after rotation" in chat.read_text()
        assert "after rotation" not in search.read_text()
        assert analytics.current_context() == {}
    finally:
        for handler in handlers:
            logger.removeHandler(handler)
            handler.close()
        logger.setLevel(old_level)


def test_actual_chat_logger_and_agent_route_without_propagation_duplicates(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "LOG_FILE", str(tmp_path / "job-hunter.log"))
    monkeypatch.setattr(config, "ERROR_LOG_FILE", "")
    monkeypatch.setenv("JOB_HUNTER_BACKGROUND", "1")
    handlers = agent._build_logging_handlers()
    root = logging.getLogger()
    for handler in handlers:
        root.addHandler(handler)
    try:
        logging.getLogger("agent").warning("search-marker")
        logging.getLogger("chat_responder").warning("chat-marker")
        assert (tmp_path / "job-hunter.log").read_text().count("search-marker") == 1
        assert "chat-marker" not in (tmp_path / "job-hunter.log").read_text()
        assert (tmp_path / "job-hunter-chat.log").read_text().count("chat-marker") == 1
        assert "search-marker" not in (tmp_path / "job-hunter-chat.log").read_text()
    finally:
        for handler in handlers:
            root.removeHandler(handler)
            handler.close()


def test_log_failure_does_not_change_a_completed_action(tmp_path, monkeypatch):
    handler = PrivateFileHandler(tmp_path / "chat.log", channel="chat")
    def fail(*args, **kwargs):
        raise OSError(PRIVATE)
    monkeypatch.setattr("private_logging.append_text", fail)
    record = logging.LogRecord("chat_responder", logging.ERROR, "", 1, "operational error", (), None)
    handler.handle(record)  # no exception/retry/control input
    handler.close()


def test_telegram_log_paths_are_selected_profile_only(monkeypatch, tmp_path):
    bot = TelegramBot(profile_name="default")
    monkeypatch.setattr(bot, "_profile", lambda name: SimpleNamespace(log_file=str(tmp_path / name / "job-hunter.log")))
    monkeypatch.setenv("JOB_HUNTER_CHAT_LOG_FILE", str(tmp_path / "foreign" / "secret.log"))
    assert bot._log_path_candidates("qa", kind="log") == [str(tmp_path / "qa" / "job-hunter.log")]
    assert bot._log_path_candidates("qa", kind="chat_log") == [str(tmp_path / "qa" / "job-hunter-chat.log")]


def test_observe_every_trigger_notification_dedup_and_legacy_state(tmp_path):
    warnings = HHUIWarnings(tmp_path, clock=lambda: 1000)
    warnings.store.save({"alerts": {}, "metadata": {"keep": True}})
    fingerprint = "a" * 64
    for stage, run_id in (("search", "run-1"), ("apply", "run-1"), ("chat", "run-2")):
        warnings.observe(fingerprint, stage, run_id)
        claim = warnings.claim_notification(fingerprint)
        if claim:
            warnings.finish(fingerprint, claim, "sent")
    state = warnings.store.load()
    item = state["observations"][fingerprint]
    assert item["occurrences"] == 3 and item["affected_runs"] == 2
    assert item["by_stage"] == {"search": 1, "apply": 1, "chat": 1, "other": 0}
    assert item["first_seen_at"] == item["last_seen_at"] == 1000
    assert state["alerts"][fingerprint]["status"] == "sent"
    assert state["metadata"] == {"keep": True}


def test_observation_storage_and_run_id_window_are_bounded(tmp_path):
    warnings = HHUIWarnings(tmp_path, clock=lambda: 1000)
    for index in range(MAX_FINGERPRINTS + 2):
        warnings.observe(f"{index:064x}", "other")
    assert len(warnings.store.load()["observations"]) == MAX_FINGERPRINTS
    for index in range(MAX_RECENT_RUNS + 2):
        warnings.observe("f" * 64, "search", f"run-{index}")
    item = warnings.store.load()["observations"]["f" * 64]
    assert len(item["recent_run_ids"]) == MAX_RECENT_RUNS
    assert item["affected_runs"] == MAX_RECENT_RUNS + 2
    warnings.observe("f" * 64, "search", f"run-{MAX_RECENT_RUNS + 1}")
    assert warnings.store.load()["observations"]["f" * 64]["affected_runs"] == MAX_RECENT_RUNS + 2


def test_observation_corruption_retains_protected_state(tmp_path):
    warnings = HHUIWarnings(tmp_path)
    content = b'{"alerts":{},"observations":[]}'
    warnings.store.path.write_bytes(content)
    with pytest.raises(RuntimeError, match="restore"):
        warnings.observe("a" * 64, "search", "run-1")
    assert warnings.store.path.read_bytes() == content


def test_funnel_exact_counts_normalized_reasons_and_legacy_reader(search_home, monkeypatch):
    vacancies = [{"id": str(index), "source": "hh"} for index in range(3)]
    monkeypatch.setattr(search_pipeline.filters, "check_vacancy", lambda v: PRIVATE if v["id"] == "0" else None)
    monkeypatch.setattr(search_pipeline.seen, "mark_seen", lambda *args: None)
    with analytics.observe_search("run-funnel", "search") as observation:
        observation.result = {"source_stats": {"hh": {"new": 3, "fetched": 5, "already_seen": 2, "relevant": 0}}}
        analytics.register_candidates(vacancies)
        selected = search_pipeline.keyword_filter(vacancies, observation.result["source_stats"], "run-funnel")
        assert [v["id"] for v in selected] == ["1", "2"]
        analytics.record_decision(run_id="run-funnel", vacancy=vacancies[1], decision="skipped_low_score",
                                  evaluation={"reason": PRIVATE, "error_kind": "TimeoutError", "score": 12})
        analytics.count_stage("matcher_pass", vacancies[2])
        analytics.count_stage("apply_attempt", vacancies[2])
        analytics.record_decision(run_id="run-funnel", vacancy=vacancies[2], decision="applied_auto")
        # Duplicate diagnostic records cannot double-count one terminal outcome in run history.
        analytics.record_decision(run_id="run-funnel", vacancy=vacancies[2], decision="applied_auto")
        observation.ok = True
        entry = observation.entry()
        assert entry["funnel"]["keyword_pass"] == 2
        assert entry["funnel"]["matcher_pass"] == entry["funnel"]["apply_attempt"] == entry["applied"] == 1
        assert entry["funnel"]["skipped"] == 2
        assert entry["reason_breakdown"] == {"keyword_filter": 1, "low_score": 1, "applied": 1}
    events = read_json_records(config.ANALYTICS_EVENTS_FILE)
    assert all(event["schema_version"] == 2 and event["run_id"] == "run-funnel" for event in events)
    assert events[1]["reason"] == PRIVATE and events[1]["error_kind"] == "TimeoutError"  # preserve existing precise journal data
    # Old decision records need neither migration nor new schema fields.
    old_path = search_home / "legacy.jsonl"
    old_path.write_text(json.dumps({"event": "decision", "created_at": "2026-10-05T00:00:00", "decision": "skipped_red_flags"}) + "\n")
    before = old_path.read_bytes()
    assert analytics.summarize(events_file=str(old_path), all_time=True)["reason_breakdown"] == {"red_flags": 1}
    assert analytics.summarize(events_file=str(old_path), all_time=True)["reason_groups"] == {"matcher_reject": 1}
    assert old_path.read_bytes() == before


@pytest.mark.parametrize("failure", ["startup", "cancel", "cleanup"])
def test_run_lifecycle_records_incomplete_and_failed_preserving_successful_actions(search_home, failure):
    calls = []
    @agent._observe_search
    async def fake(dry_run=False):
        observation = analytics.current_search()
        if failure == "startup":
            raise ValueError(PRIVATE)
        vacancy = {"id": "1", "source": "hh"}
        observation.result = {"found": 1, "source_stats": {"hh": {"new": 1}}}
        analytics.register_candidates([vacancy])
        analytics.count_stage("apply_attempt", vacancy)
        calls.append("sent-once")
        analytics.record_decision(run_id=observation.run_id, vacancy=vacancy, decision="applied_auto")
        agent._record_search_run(observation.result, dry_run=False, ok=True)
        analytics.search_stage("cleanup")
        if failure == "cancel":
            raise asyncio.CancelledError()
        raise OSError(PRIVATE)
    with pytest.raises(BaseException):
        asyncio.run(fake())
    records = read_json_records(config.RUN_HISTORY_FILE)
    assert len(records) == 2 and records[0]["status"] == "incomplete"
    final = records[-1]
    assert final["ok"] is False and final["run_id"] == records[0]["run_id"]
    assert PRIVATE not in json.dumps(records)
    assert final["applied"] == (0 if failure == "startup" else 1)
    assert calls == ([] if failure == "startup" else ["sent-once"])
    events = read_json_records(config.ANALYTICS_EVENTS_FILE)
    assert sum(event["event"] == "search_started" for event in events) == 1
    assert sum(event["event"] == "search_finished" for event in events) == 1
    assert analytics.current_search() is None and analytics.current_context() == {}
    assert analytics.latest_run_records(records) == [final]


def test_run_history_partial_and_legacy_records_remain_readable(search_home):
    records = [{"created_at": "old", "ok": True}, {"run_id": "run-1", "status": "incomplete"},
               {"run_id": "run-1", "status": "finished", "ok": False},
               {"run_id": "run-2", "status": "incomplete"}]
    assert analytics.latest_run_records(records) == [records[0], records[2], records[3]]
    text = ui.build_runs_text(analytics.latest_run_records(records))
    assert "run-2" in text and "не завершён" in text and "с ошибкой" in text


def test_zero_apply_diagnosis_does_not_guess_or_parse_human_logs():
    run = {"run_id": "run-1", "ok": True, "funnel": {"new": 8, "keyword_pass": 5, "matcher_pass": 2, "applied": 0},
           "reason_breakdown": {"keyword_filter": 3, "low_score": 3, "manual_required": 1, "guard_stop": 1}}
    text = ui.format_run_summary(run)
    assert "run-1" in text and "Keyword pass: 5" in text and "Matcher pass: 2" in text
    assert "keyword filter: 3" in text and "matcher: low score: 3" in text
    assert "manual: 1" in text and "guard stop: 1" in text and "не классифицирована" not in text
    assert "причина не классифицирована" in analytics.zero_apply_diagnosis({"found": 2, "applied": 0})
    run["funnel"]["applied"] = 1
    assert analytics.zero_apply_diagnosis(run) == ""
    assert "релевантных" not in ui.format_run_summary({"source_stats": {"hh": {"relevant": 1}}})


def test_telegram_freshness_and_run_result_are_independent(search_home, monkeypatch):
    bot = TelegramBot(profile_name="default")
    path = search_home / "job-hunter.log"
    path.write_text("fresh")
    monkeypatch.setattr(bot, "_profile", lambda name: SimpleNamespace(hh=SimpleNamespace(cookies_file=str(search_home / "missing"))))
    monkeypatch.setattr(bot, "_profile_schedule", lambda name: (30, 30))
    monkeypatch.setattr(bot, "_daemon_state", lambda name: {"running": False})
    monkeypatch.setattr(bot, "_bot_state", lambda: {"running": True, "pid": 1})
    monkeypatch.setattr(bot, "_api_request", AsyncMock(return_value={"username": "synthetic"}))
    monkeypatch.setattr(bot, "_profile_recipient_ids", lambda name: [])
    monkeypatch.setattr(bot, "_log_tail", lambda *a, **k: (str(path), ""))
    monkeypatch.setattr(bot, "_latest_run", lambda name: {"created_at": analytics._now().isoformat(), "ok": False})
    checks = asyncio.run(bot._collect_diagnostics("default"))
    by_name = {item["name"]: item for item in checks}
    assert by_name["Daemon"]["ok"] is False
    assert by_name["Данные прогона"]["ok"] is True
    assert by_name["Последний прогон"]["ok"] is False
    text = ui.build_diagnostics_text(profile_name="default", generated_at="now", checks=checks)
    assert "❌ Daemon: остановлен" in text and "❌ Последний прогон: ошибка" in text
    assert "📝 Последняя запись в логе" in text and "ok=false" not in text


def configure_synthetic_search(monkeypatch, home):
    """Golden trace captured on v0.8.0: these actual decisions must stay fixed."""
    for key, value in {"HH_ENABLED": True, "SUPERJOB_ENABLED": False, "HABR_ENABLED": False,
        "GEEKJOB_ENABLED": False, "MAX_APPLICATIONS_PER_RUN": 0, "MAX_AUTO_APPLICATIONS_PER_SOURCE": 0,
        "HH_MIN_SECONDS_BETWEEN_APPLICATIONS": 0, "HH_CHAT_RESPONDER_ENABLED": False,
        "TELEGRAM_NOTIFY_AUTO_DIGEST": False, "HH_APPLICATION_MODE": "auto"}.items():
        monkeypatch.setattr(config, key, value)
    fake = SimpleNamespace(start=AsyncMock(), stop=AsyncMock(), is_logged_in=AsyncMock(return_value=True),
                           get_negotiation_statuses=AsyncMock(return_value=[]), _page=None)
    monkeypatch.setattr(agent, "HHClient", lambda: fake)
    monkeypatch.setattr(agent.hh_pipeline, "enabled", lambda: False)
    monkeypatch.setattr(agent.hh_guard, "can_auto_apply", lambda: (True, ""))
    monkeypatch.setattr(agent.hh_guard, "record_apply_success", lambda: None)
    monkeypatch.setattr(agent.company_blacklist, "is_blocked", lambda company: False)
    monkeypatch.setattr(agent, "ShadowVerifier", lambda: SimpleNamespace(check=AsyncMock()))
    monkeypatch.setattr(agent, "create_task", lambda *a: "task")
    for name in ("office_log", "notify_search_started", "notify_summary", "notify_application", "notify_digest", "notify_needs_manual"):
        monkeypatch.setattr(agent, name, AsyncMock())
    monkeypatch.setattr(agent.notifier, "notify_stale_cookies", AsyncMock())
    monkeypatch.setattr(agent.notifier, "notify_llm_issue", AsyncMock())
    monkeypatch.setattr(agent.manual_apply_queue, "create_candidate", lambda *a, **k: {"token": "synthetic"})
    monkeypatch.setattr(agent.profile_mod, "active", lambda: SimpleNamespace(name="synthetic"))
    original_sleep = asyncio.sleep
    async def no_delay(*a):
        await original_sleep(0)
    monkeypatch.setattr(agent.asyncio, "sleep", no_delay)
    vacancies = [{"id": str(i), "source": "hh", "source_label": "hh.ru", "title": f"QA {i}", "company": f"Company {i}"} for i in range(1, 7)]
    async def collect(*args, source_stats=None, **kwargs):
        search_pipeline.get_source_bucket(source_stats, vacancies[0])["fetched"] = len(vacancies)
        return vacancies
    monkeypatch.setattr(search_pipeline, "collect_all", collect)
    monkeypatch.setattr(search_pipeline.filters, "check_vacancy", lambda v: "keyword_reject" if v["id"] == "1" else None)
    monkeypatch.setattr(agent.apply_orchestrator, "fetch_vacancy_details", AsyncMock(return_value="ordinary description"))
    trace = []
    async def evaluate(v, details):
        trace.append(["matcher", v["id"]])
        return {"score": 90 if int(v["id"]) >= 4 else 10, "should_apply": int(v["id"]) >= 4,
                "red_flags": [PRIVATE] if v["id"] == "2" else [], "reason": PRIVATE}
    monkeypatch.setattr(agent, "evaluate_vacancy", evaluate)
    monkeypatch.setattr(agent, "is_manual_review_candidate", lambda e: False)
    monkeypatch.setattr(agent, "generate_cover_letter", AsyncMock(return_value=PRIVATE))
    monkeypatch.setattr(agent.apply_orchestrator, "is_auto_apply_enabled", lambda source: True)
    async def apply(v, cover, *a, **k):
        trace.append(["apply", v["id"], cover])
        if v["id"] == "5":
            return {"ok": False, "already_applied": True}
        if v["id"] == "6":
            raise ValueError(PRIVATE)
        return {"ok": True}
    monkeypatch.setattr(agent.apply_orchestrator, "dispatch_apply", apply)
    return trace


@pytest.mark.parametrize("analytics_enabled,writer_fails", [(True, False), (False, False), (True, True)])
def test_actual_search_decisions_unchanged_with_failed_analytics(search_home, monkeypatch, analytics_enabled, writer_fails):
    trace = configure_synthetic_search(monkeypatch, search_home)
    monkeypatch.setattr(config, "ANALYTICS_ENABLED", analytics_enabled)
    if writer_fails:
        def fail(*a, **k):
            raise OSError(PRIVATE)
        monkeypatch.setattr(analytics, "_append_event", fail)
    result = asyncio.run(agent.do_search())
    assert trace == [["matcher", "2"], ["matcher", "3"], ["matcher", "4"], ["apply", "4", PRIVATE],
                     ["matcher", "5"], ["apply", "5", PRIVATE], ["matcher", "6"], ["apply", "6", PRIVATE]]
    assert result["applied"] == 1 and result["skipped"] == 4
    assert result["funnel"]["new"] == 6 and result["funnel"]["keyword_pass"] == 5
    assert result["funnel"]["matcher_pass"] == result["funnel"]["apply_attempt"] == 3
    assert result["funnel"]["applied"] == result["funnel"]["manual"] == result["funnel"]["failed"] == 1
    assert result["reason_breakdown"] == {"keyword_filter": 1, "red_flags": 1, "low_score": 1,
                                          "applied": 1, "already_applied": 1, "apply_failed": 1}
    records = read_json_records(config.RUN_HISTORY_FILE)
    assert records[-1]["ok"] is True and records[-1]["manual"] == 1
    assert PRIVATE not in json.dumps(records)
    assert records[-1]["stage_failures"][-1] == {"source": "hh", "stage": "apply", "error_kind": "ValueError", "continued": True}
    assert set(record["run_id"] for record in records) == {result["run_id"]}


@pytest.mark.parametrize("case", ["dry_run", "guard", "manual", "limit"])
def test_actual_search_alternate_decisions_and_counter_points(search_home, monkeypatch, case):
    trace = configure_synthetic_search(monkeypatch, search_home)
    if case == "guard":
        monkeypatch.setattr(agent.hh_guard, "can_auto_apply", lambda: (False, "private guard " + PRIVATE))
    elif case == "manual":
        monkeypatch.setattr(agent, "is_manual_review_candidate", lambda e: True)
    elif case == "limit":
        monkeypatch.setattr(config, "MAX_APPLICATIONS_PER_RUN", 1)
    result = asyncio.run(agent.do_search(dry_run=case == "dry_run"))
    final = read_json_records(config.RUN_HISTORY_FILE)[-1]
    assert PRIVATE not in json.dumps(final)
    if case == "dry_run":
        assert result["applied"] == 3  # legacy business result retained
        assert final["applied"] == final["funnel"]["apply_attempt"] == 0
        assert final["funnel"]["dry_run_match"] == 3
        assert result["source_stats"]["hh"]["applied"] == 0
        assert "Dry-run matches: 3" in ui.format_run_summary(final)
    elif case == "guard":
        assert final["funnel"]["matcher_pass"] == final["funnel"]["guard_stop"] == 3
        assert final["funnel"]["apply_attempt"] == final["manual"] == final["applied"] == 0
        assert final["reason_breakdown"]["guard_stop"] == 3
    elif case == "manual":
        assert final["manual"] == 2 and final["reason_breakdown"]["manual_required"] == 1
        assert "low_score" not in final["reason_breakdown"]
    else:
        assert final["funnel"]["apply_attempt"] == final["applied"] == 1
        assert final["reason_breakdown"]["run_limit"] == final["funnel"]["not_processed"] == 2
        assert sum(1 for item in trace if item[0] == "apply") == 1


def test_source_failure_journal_is_safe_correlated_and_records_continuation(search_home, monkeypatch):
    async def bad(*a, **k):
        raise TimeoutError(PRIVATE)
    monkeypatch.setattr(search_pipeline, "collect_hh_vacancies", bad)
    for name in ("collect_superjob_vacancies", "collect_habr_vacancies", "collect_geekjob_vacancies"):
        monkeypatch.setattr(search_pipeline, name, AsyncMock(return_value=[]))
    monkeypatch.setattr(search_pipeline, "office_log", AsyncMock())
    with analytics.observe_search("run-failed-source", "search") as observation:
        assert asyncio.run(search_pipeline.collect_all(None, None, None, None)) == []
        observation.ok = True
        final = observation.entry()
        analytics.record_search_finished(run_id=observation.run_id, mode="search", result=final)
    events = read_json_records(config.ANALYTICS_EVENTS_FILE)
    failure = next(event for event in events if event["event"] == "stage_failed")
    assert failure["run_id"] == "run-failed-source" and failure["source"] == "hh"
    assert failure["stage"] == "collection" and failure["error_kind"] == "TimeoutError"
    assert failure["continued"] is True and events[-1]["ok"] is True
    assert PRIVATE not in json.dumps(events)


def test_keyword_rejects_are_visible_even_when_legacy_found_is_zero(search_home, monkeypatch):
    configure_synthetic_search(monkeypatch, search_home)
    monkeypatch.setattr(search_pipeline.filters, "check_vacancy", lambda v: "keyword_filter")
    result = asyncio.run(agent.do_search())
    assert result["found"] == result["applied"] == 0
    assert result["new"] == result["funnel"]["skipped"] == result["reason_breakdown"]["keyword_filter"] == 6
    assert "keyword filter: 6" in ui.format_run_summary(read_json_records(config.RUN_HISTORY_FILE)[-1])


def test_correlated_run_ids_are_isolated_under_concurrent_runs(search_home, monkeypatch):
    ids = iter(("run-a", "run-b"))
    monkeypatch.setattr(analytics, "new_run_id", lambda mode: next(ids))
    @agent._observe_search
    async def fake(dry_run=False):
        observation = analytics.current_search()
        vacancy = {"source": "hh", "id": observation.run_id}
        observation.result = {"source_stats": {"hh": {"new": 1}}}
        analytics.register_candidates([vacancy])
        await asyncio.sleep(0)
        assert analytics.current_context()["run_id"] == observation.run_id
        analytics.record_decision(run_id=observation.run_id, vacancy=vacancy, decision="guard_stop")
        agent._record_search_run(observation.result, dry_run=False, ok=True)
        return observation.result
    async def run():
        return await asyncio.gather(fake(), fake())
    results = asyncio.run(run())
    assert {result["run_id"] for result in results} == {"run-a", "run-b"}
    assert all(result["reason_breakdown"] == {"guard_stop": 1} for result in results)
    assert len(analytics.latest_run_records(read_json_records(config.RUN_HISTORY_FILE))) == 2
    assert analytics.current_context() == {} and analytics.current_search() is None


def test_real_offline_chromium_ui_observation_survives_notification_cooldown(tmp_path):
    from hh.ui import HHUIGuard, HHUnexpectedUI
    from tests.test_hh_ui_safety import browser_case
    notify = AsyncMock(return_value=True)
    html = '<div role="dialog"><h2>Unexpected fixture</h2><textarea>' + PRIVATE + '</textarea></div>'
    async def check(page):
        for stage, run_id in (("search", "run-1"), ("apply_before_navigation", "run-1"), ("chat_send", "run-2")):
            with analytics.event_context(run_id=run_id):
                guard = HHUIGuard(tmp_path, notify=notify, clock=lambda: 1000)
                with pytest.raises(HHUnexpectedUI):
                    await guard.ensure(page, stage)
                # Cached blocked raises are not another actual UI observation.
                with pytest.raises(HHUnexpectedUI):
                    await guard.ensure(page, stage)
        notify.assert_awaited_once()
        state = json.loads((tmp_path / "hh_ui_warnings.json").read_text())
        item = next(iter(state["observations"].values()))
        assert item["occurrences"] == 3 and item["affected_runs"] == 2
        assert item["by_stage"] == {"search": 1, "apply": 1, "chat": 1, "other": 0}
        assert PRIVATE not in json.dumps(state)
        events = [event for event in read_json_records(config.ANALYTICS_EVENTS_FILE) if event["event"] == "unexpected_ui"]
        assert [event["run_id"] for event in events] == ["run-1", "run-1", "run-2"]
        assert PRIVATE not in json.dumps(events)
    asyncio.run(browser_case(html, check))


def test_run_ids_are_unique_even_in_same_second(monkeypatch):
    frozen = analytics._now()
    monkeypatch.setattr(analytics, "_now", lambda: frozen)
    assert len({analytics.new_run_id("search") for _ in range(100)}) == 100


def test_actual_search_logs_do_not_contain_generated_private_payloads(search_home, monkeypatch):
    configure_synthetic_search(monkeypatch, search_home)
    path = search_home / "job-hunter.log"
    handler = PrivateFileHandler(path, channel="search")
    root = logging.getLogger()
    root.addHandler(handler)
    try:
        asyncio.run(agent.do_search())
        assert PRIVATE not in path.read_text()
        assert "error_kind=ValueError" in path.read_text() or "ValueError" in path.read_text()
    finally:
        root.removeHandler(handler)
        handler.close()


def test_operational_credentials_redacted_without_exception_payload(tmp_path):
    path = tmp_path / "log"
    handler = PrivateFileHandler(path)
    for message in ("Authorization: Bearer " + PRIVATE, "Cookie: hhtoken=" + PRIVATE,
                    "token='" + PRIVATE + "'", "password=" + PRIVATE,
                    "URL https://auth.invalid/path?credential=" + PRIVATE):
        handler.handle(logging.LogRecord("synthetic", logging.WARNING, "", 1, message, (), None))
    handler.close()
    assert PRIVATE not in path.read_text()


@pytest.mark.parametrize("action_fails", [False, True])
def test_real_dispatch_wrapper_journal_failure_does_not_repeat_or_replace_action(search_home, monkeypatch, action_fails):
    import apply_orchestrator
    error = ValueError(PRIVATE)
    result = {"ok": True, "receipt": "synthetic"}
    dispatch = AsyncMock(side_effect=error) if action_fails else AsyncMock(return_value=result)
    monkeypatch.setattr(apply_orchestrator, "_dispatch_apply", dispatch)
    monkeypatch.setattr(apply_orchestrator.company_blacklist, "is_blocked", lambda company: False)
    def fail(*a, **k):
        raise OSError(PRIVATE)
    monkeypatch.setattr(analytics, "_append_event", fail)
    async def run():
        return await apply_orchestrator.dispatch_apply({"id": "1", "source": "habr"}, PRIVATE)
    if action_fails:
        with pytest.raises(ValueError) as observed:
            asyncio.run(run())
        assert observed.value is error
    else:
        assert asyncio.run(run()) is result
    dispatch.assert_awaited_once()


def test_run_history_destination_is_frozen_before_profile_config_changes(search_home, monkeypatch):
    original = config.RUN_HISTORY_FILE
    foreign = str(search_home / "other-profile" / "history.jsonl")
    @agent._observe_search
    async def fake(dry_run=False):
        monkeypatch.setattr(config, "RUN_HISTORY_FILE", foreign)
        observation = analytics.current_search()
        agent._record_search_run(observation.result, dry_run=False, ok=True)
        return observation.result
    asyncio.run(fake())
    records = read_json_records(original)
    assert len(records) == 2 and records[-1]["ok"] is True
    assert not Path(foreign).exists()


@pytest.mark.parametrize("dry_run", [False, True])
def test_existing_search_notification_has_truthful_keyword_and_dry_run_labels(monkeypatch, dry_run):
    import notifier
    send = AsyncMock()
    monkeypatch.setattr(notifier, "send_message", send)
    monkeypatch.setattr(notifier, "_telegram_flag", lambda *a: True)
    asyncio.run(notifier.notify_summary(5, 2, 3, {"hh": {"keyword_pass": 5, "new": 8}}, dry_run=dry_run))
    text = send.call_args.args[0]
    assert "Keyword pass: 5" in text and "релевант" not in text.casefold()
    assert ("Dry-run matches: 2" if dry_run else "Откликов: 2") in text
