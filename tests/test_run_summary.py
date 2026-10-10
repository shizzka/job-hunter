"""Offline regression coverage for correlated, profile-owned run diagnostics."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import agent
import analytics
import config
import llm_client
import matcher
import telegram_bot
from run_summary import build_run_summary
from state_store.private_journal import append_json, read_json_records
from telegram_bot_ui import build_run_summary_text


def finished_run(**changes):
    return {"kind": "search", "run_id": "run-a", "ok": True,
            "started_at": "2026-10-10T12:00:00+00:00",
            "finished_at": "2026-10-10T12:00:42+00:00",
            "source_stats": {"hh": {}}, "funnel": {"fetched": 23, "evaluated": 0,
                "applied": 0, "deferred_unscored": 3, "not_processed": 20}, **changes}


def correlated_events():
    root = {"event": "stage_failed", "run_id": "run-a", "failure_id": "failure-a",
            "stage": "matcher", "source": "hh", "provider": "primary,backup",
            "reason_code": "LLM_PROVIDER_EXHAUSTED", "retryable": True,
            "fallback_status": "failed", "created_at": "2026-10-10T12:00:02+00:00"}
    events = [root]
    for index in range(23):
        outcome = "deferred_unscored" if index < 3 else "not_processed"
        events.append({"event": "decision", "run_id": "run-a", "source": "hh",
                       "vacancy_id": str(index), "failure_id": "failure-a",
                       "decision": outcome, "outcome": outcome, "score": None})
    events.append({**root, "event": "failure_observed", "vacancy_id": "2"})
    events.append({**root, "failure_id": "terminal-a", "parent_failure_id": "failure-a"})
    return events


def test_23_consequences_are_one_root_cause():
    run, events = finished_run(ok=False), correlated_events()
    before = json.dumps([run, events], sort_keys=True)
    summary = build_run_summary(run, events, profile="qa")
    assert summary == build_run_summary(run, list(events), profile="qa")
    assert json.dumps([run, events], sort_keys=True) == before
    assert summary["status"] == "failed"
    assert summary["failure_count"] == 1
    root = summary["primary_failure"]
    assert root["failure_id"] == "failure-a"
    assert root["reason_code"] == "LLM_PROVIDER_EXHAUSTED"
    assert root["affected_count"] == 23
    assert root["downstream_events"] == {"deferred_unscored": 3, "not_processed": 20}
    assert root["retryable"] is True
    assert root["fallback_status"] == "failed"
    assert summary["duration_seconds"] == 42
    assert summary["counters"]["evaluated"] == 0


@pytest.mark.parametrize("progress,status", [(0, "failed"), (2, "partial")])
def test_failure_status_depends_on_real_progress(progress, status):
    run = finished_run(ok=False)
    run["funnel"]["evaluated"] = progress
    assert build_run_summary(run, correlated_events())["status"] == status


def test_success_without_errors_has_no_invented_failure():
    run = finished_run(funnel={"fetched": 2, "evaluated": 2, "applied": 1,
                              "deferred_unscored": 0, "not_processed": 0})
    summary = build_run_summary(run)
    assert summary["status"] == "success"
    assert summary["failure_count"] == 0
    assert summary["primary_failure"] is None


def test_interrupted_run_uses_journal_lower_bounds_and_unknowns():
    run = finished_run(ok=None, status="incomplete", finished_at=None, funnel={})
    summary = build_run_summary(run, correlated_events())
    assert summary["status"] == "incomplete"
    assert summary["counter_status"] == "minimum_or_unknown"
    assert summary["counters"]["deferred_unscored"] == 3
    assert summary["counters"]["not_processed"] == 20
    assert summary["counters"]["evaluated"] is None
    assert summary["duration_seconds"] is None
    text = build_run_summary_text(summary, profile_name="qa")
    assert "≥3" in text and "≥20" in text
    assert "неизвестно" in text


def test_legacy_failure_stays_unknown_without_log_inference():
    summary = build_run_summary({"kind": "search", "mode": "search", "found": 23,
                                 "applied": 0, "ok": False,
                                 "error": "secret text says timeout"})
    assert summary["primary_failure"]["reason_code"] == "UNKNOWN_ROOT_CAUSE"
    assert summary["primary_failure"]["failure_id"] is None
    assert summary["primary_failure"]["affected_count"] is None
    assert summary["counters"]["evaluated"] is None
    assert "secret text" not in build_run_summary_text(summary, profile_name="qa")


def test_other_run_events_cannot_change_summary():
    events = [{**event, "run_id": "foreign-run"} for event in correlated_events()]
    run = finished_run(funnel={"applied": 1})
    summary = build_run_summary(run, events)
    assert summary["status"] == "success"
    assert summary["failure_count"] == 0


def test_repeated_decisions_count_each_vacancy_once():
    events = correlated_events()
    events.extend([dict(event) for event in events if event.get("event") == "decision"])
    summary = build_run_summary(finished_run(ok=False), events)
    assert summary["failure_count"] == 1
    assert summary["primary_failure"]["affected_count"] == 23
    assert summary["primary_failure"]["downstream_events"] == {
        "deferred_unscored": 3, "not_processed": 20}


def test_latest_checkpoint_and_explicit_run_selection(tmp_path):
    history, events = tmp_path / "history.jsonl", tmp_path / "events.jsonl"
    append_json(history, finished_run(run_id="older", funnel={"applied": 1}))
    append_json(history, finished_run(run_id="newer", status="incomplete", ok=None,
                                      finished_at=None, funnel={}))
    append_json(history, finished_run(run_id="newer", funnel={"applied": 2}))
    paths = {"history_file": str(history), "events_file": str(events)}
    latest = analytics.get_run_summary(**paths)
    assert latest["run_id"] == "newer" and latest["status"] == "success"
    assert latest["counters"]["applied"] == 2
    assert analytics.get_run_summary("older", **paths)["counters"]["applied"] == 1


def test_start_only_journal_recovers_incomplete_run(tmp_path):
    events = tmp_path / "events.jsonl"
    append_json(events, {"event": "search_started", "run_id": "interrupted",
                         "created_at": "2026-10-10T12:00:00+00:00", "enabled_sources": ["hh"]})
    summary = analytics.get_run_summary("interrupted", events_file=str(events),
                                       history_file=str(tmp_path / "missing-history.jsonl"))
    assert summary["run_id"] == "interrupted"
    assert summary["status"] == "incomplete"
    assert summary["sources"] == ["hh"]
    assert summary["counters"]["evaluated"] is None
    assert summary["primary_failure"] is None


def test_profile_storage_selection_and_missing_id_are_isolated(tmp_path):
    paths = []
    for profile, run_id in (("a", "run-a"), ("b", "run-b")):
        history, events = tmp_path / f"{profile}-history.jsonl", tmp_path / f"{profile}-events.jsonl"
        append_json(history, finished_run(run_id=run_id, funnel={"applied": 1}))
        append_json(events, {"event": "search_started", "run_id": run_id, "profile_id": profile})
        paths.append((history, events))
    history, events = paths[0]
    summary = analytics.get_run_summary(events_file=str(events), history_file=str(history), profile="a")
    assert summary["run_id"] == "run-a"
    assert summary["profile_id"] == "a"
    assert summary["profile"] == "a"
    assert analytics.get_run_summary("run-b", events_file=str(events), history_file=str(history)) is None


def test_latest_journal_start_survives_missing_history_checkpoint(tmp_path):
    history, events = tmp_path / 'history.jsonl', tmp_path / 'events.jsonl'
    append_json(history, finished_run(funnel={'applied': 1}))
    append_json(events, {'event': 'search_started', 'run_id': 'new-run',
                        'created_at': '2026-10-10T12:01:00+00:00'})
    summary = analytics.get_run_summary(events_file=str(events), history_file=str(history))
    assert summary['run_id'] == 'new-run'
    assert summary['status'] == 'incomplete'
    assert summary['counters']['applied'] is None


def test_finish_only_journal_has_no_invented_start_time(tmp_path):
    events = tmp_path / 'events.jsonl'
    append_json(events, {'event': 'search_finished', 'run_id': 'legacy-finish',
                        'created_at': '2026-10-10T12:01:00+00:00', 'ok': True})
    summary = analytics.get_run_summary('legacy-finish', events_file=str(events),
                                        history_file=str(tmp_path / 'missing.jsonl'))
    assert summary['status'] == 'success'
    assert summary['started_at'] is None
    assert summary['duration_seconds'] is None


@pytest.mark.parametrize('journal', [False, True])
def test_legacy_uncertainty_is_never_success(journal):
    run = finished_run(funnel={}, applied=0, reason_breakdown={'apply_uncertain': 1})
    events = [{'event': 'decision', 'run_id': 'run-a', 'source': 'hh', 'vacancy_id': '1',
               'decision': 'apply_uncertain', 'outcome': 'manual'}] if journal else []
    summary = build_run_summary(run, events)
    assert summary['status'] != 'success'
    assert summary['counters']['uncertain'] == 1
    text = build_run_summary_text(summary, profile_name='qa')
    assert 'требуют сверки: 1' in text
    assert 'Итог: ✅ успешно' not in text


def test_interrupted_uncertainty_supersedes_positive_receipt_minimum():
    run = finished_run(status='incomplete', finished_at=None, funnel={'applied': 1})
    events = [{'event': 'decision', 'run_id': 'run-a', 'source': 'hh', 'vacancy_id': '1',
               'decision': 'applied_auto', 'outcome': 'applied'},
              {'event': 'decision', 'run_id': 'run-a', 'source': 'hh', 'vacancy_id': '1',
               'decision': 'apply_uncertain', 'outcome': 'manual'}]
    summary = build_run_summary(run, events)
    assert summary['status'] == 'incomplete'
    assert summary['counters']['applied'] is None
    assert summary['counters']['uncertain'] == 1


def test_primary_failure_uses_first_timestamp_across_both_stores():
    older = {'event': 'stage_failed', 'run_id': 'run-a', 'failure_id': 'first',
             'stage': 'matcher', 'reason_code': 'LLM_PROVIDER_EXHAUSTED',
             'created_at': '2026-10-10T12:00:02+00:00'}
    newer = {**older, 'failure_id': 'second', 'created_at': '2026-10-10T12:01:02+00:00'}
    run = finished_run(stage_failures=[newer], funnel={'evaluated': 1})
    summary = build_run_summary(run, [older, newer])
    assert summary['primary_failure']['failure_id'] == 'first'
    assert summary['failure_count'] == 2


def test_foreign_run_failure_cannot_mutate_active_search(runtime):
    vacancy = {'id': '1', 'source': 'hh'}
    with analytics.observe_search('owner-run', 'search') as observation:
        with analytics.event_context(run_id='foreign-run', source='hh', vacancy_id='1'):
            analytics.record_failure('matcher', TimeoutError(), reason_code='LLM_TIMEOUT', continued=True)
        analytics.record_decision(run_id='foreign-run', vacancy=vacancy, decision='deferred_unscored')
        assert observation.failures == []
        assert observation.vacancy_failures == {}
        assert observation.decided == set()
    rows = read_json_records(runtime.analytics_events_file)
    assert 'failure_id' not in rows[-1]


def test_artifacts_are_references_and_html_escaped():
    event = {"event": "debug_artifact", "run_id": "run-a", "artifact_type": "screenshot",
             "artifact_ref": "trace/<synthetic>.png", "source": "hh", "vacancy_id": "1",
             "raw_html": "private page content", "cookie": "secret-cookie"}
    summary = build_run_summary(finished_run(funnel={"applied": 1}), [event, event])
    assert len(summary["artifacts"]) == 1
    assert "raw_html" not in summary["artifacts"][0]
    text = build_run_summary_text(summary, profile_name="<qa>")
    assert "trace/&lt;synthetic&gt;.png" in text
    assert "private page content" not in text and "secret-cookie" not in text


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    paths = SimpleNamespace(analytics_events_file=str(tmp_path / "events.jsonl"),
                            run_history_file=str(tmp_path / "history.jsonl"))
    monkeypatch.setattr(config, "ANALYTICS_ENABLED", True)
    monkeypatch.setattr(config, "ANALYTICS_EVENTS_FILE", paths.analytics_events_file)
    monkeypatch.setattr(config, "RUN_HISTORY_FILE", paths.run_history_file)
    monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))
    monkeypatch.setattr(matcher, "_load_resume", lambda: "Synthetic candidate resume")
    monkeypatch.setattr(matcher, "_build_matcher_truth_block", lambda: "")
    monkeypatch.setattr(matcher.resume_versions, "record_input", lambda *args: None)
    monkeypatch.setattr(matcher.resume_versions, "payload", lambda vacancy: {})
    monkeypatch.setattr(llm_client, "_record_usage", lambda *args, **kwargs: None)
    return paths


def fake_client(monkeypatch, error):
    specs = [llm_client.ProviderSpec(name=name, base_url="https://invalid.example/v1",
                                    api_key="synthetic") for name in ("primary", "backup")]
    client = llm_client.FallbackLLMClient(specs)
    calls = []

    async def create(index, **kwargs):
        calls.append(index)
        raise error

    transports = [SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda index=index, **kwargs: create(index, **kwargs)))) for index in range(2)]
    monkeypatch.setattr(client, "_client_for", lambda index: transports[index])
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    return calls


def bot_for_profile(monkeypatch, paths):
    bot = telegram_bot.TelegramBot(profile_name="qa")
    monkeypatch.setattr(bot, "_selected_profile", lambda principal: "qa")
    monkeypatch.setattr(bot, "_active_command", lambda profile: None)
    monkeypatch.setattr(bot, "_profile", lambda profile: paths)
    monkeypatch.setattr(bot, "_menu_reply_markup", lambda principal: {})
    monkeypatch.setattr(bot, "_load_state", lambda: {})
    bot._send_text = AsyncMock()
    return bot


def test_offline_real_search_matcher_journal_and_telegram_dispatch(runtime, monkeypatch):
    calls = fake_client(monkeypatch, TimeoutError("synthetic timeout"))
    vacancies = [{"id": str(index), "source": "hh", "title": "Synthetic vacancy"} for index in range(23)]
    evaluations = []

    @agent._observe_search
    async def search(dry_run=False):
        observation = analytics.current_search()
        analytics.register_candidates(vacancies)
        observation.result.update(found=23, source_stats={"hh": {"fetched": 23, "new": 23}})
        for vacancy in vacancies:
            evaluation = await analytics.tracked_call("matcher", observation.run_id, vacancy,
                                                       matcher.evaluate_vacancy, vacancy, "Synthetic details")
            evaluations.append(evaluation)
            analytics.record_decision(run_id=observation.run_id, vacancy=vacancy,
                                      decision="deferred_unscored", evaluation=evaluation)
        observation.ok = True

    asyncio.run(search())
    assert calls == [0, 1] * 23
    assert all(e["score"] is None and e["should_apply"] is False for e in evaluations)
    assert all(e["evaluation_status"] == "deferred_unscored" for e in evaluations)
    records = read_json_records(runtime.run_history_file)
    assert len(records) == 2
    assert records[0]["status"] == "incomplete"
    summary = analytics.get_run_summary(events_file=runtime.analytics_events_file,
                                       history_file=runtime.run_history_file, profile="qa")
    root = summary["primary_failure"]
    assert summary["failure_count"] == 1
    assert root["reason_code"] == "LLM_PROVIDER_EXHAUSTED"
    assert root["affected_count"] == 23
    assert root["downstream_events"] == {"deferred_unscored": 23}
    assert root["retryable"] is True and root["fallback_status"] == "failed"
    decisions = [e for e in read_json_records(runtime.analytics_events_file) if e["event"] == "decision"]
    assert len(decisions) == 23
    assert {e["failure_id"] for e in decisions} == {root["failure_id"]}
    assert all(e["score"] is None for e in decisions)
    bot = bot_for_profile(monkeypatch, runtime)
    monkeypatch.setattr(bot, "_log_tail", lambda *args, **kwargs: ("synthetic.log", "RAW-ONLY-MARKER"))
    principal = {"user_id": 1, "role": telegram_bot.ROLE_ADMIN}
    asyncio.run(bot._dispatch(1, principal, "/log", summary["run_id"]))
    rendered = bot._send_text.call_args.args[1]
    assert "LLM_PROVIDER_EXHAUSTED" in rendered
    assert root["failure_id"] in rendered
    assert "Затронуто вакансий: 23" in rendered
    assert "RAW-ONLY-MARKER" not in rendered
    asyncio.run(bot._dispatch(1, principal, "/raw_log", ""))
    assert "RAW-ONLY-MARKER" in bot._send_text.call_args.args[1]
    assert "LLM_PROVIDER_EXHAUSTED" not in bot._send_text.call_args.args[1]


def test_nonretryable_auth_failure_does_not_try_backup(runtime, monkeypatch):
    error = RuntimeError("synthetic authentication rejected")
    error.status_code = 401
    calls = fake_client(monkeypatch, error)

    @agent._observe_search
    async def search(dry_run=False):
        observation = analytics.current_search()
        vacancy = {"id": "1", "source": "hh", "title": "Synthetic vacancy"}
        analytics.register_candidates([vacancy])
        evaluation = await analytics.tracked_call("matcher", observation.run_id, vacancy,
                                                   matcher.evaluate_vacancy, vacancy)
        assert evaluation["score"] is None
        analytics.record_decision(run_id=observation.run_id, vacancy=vacancy,
                                  decision="deferred_unscored", evaluation=evaluation)
        observation.ok = True

    asyncio.run(search())
    assert calls == [0]
    summary = analytics.get_run_summary(events_file=runtime.analytics_events_file,
                                       history_file=runtime.run_history_file)
    root = summary["primary_failure"]
    assert summary["failure_count"] == 1
    assert root["reason_code"] == "LLM_AUTH_FAILED"
    assert root["retryable"] is False
    assert root["fallback_status"] == "not_attempted"
    assert root["affected_count"] == 1
    assert root["downstream_events"] == {"deferred_unscored": 1}


def test_explicit_propagated_provider_failure_is_one_root(runtime, monkeypatch):
    calls = fake_client(monkeypatch, TimeoutError("synthetic timeout"))

    @agent._observe_search
    async def search(dry_run=False):
        observation = analytics.current_search()
        vacancies = [{"id": str(i), "source": "hh"} for i in range(23)]
        analytics.register_candidates(vacancies)

        async def provider_request():
            return await matcher._get_client().chat.completions.create(model="synthetic-model")

        await analytics.tracked_call("matcher", observation.run_id, vacancies[0], provider_request)

    with pytest.raises(llm_client.LLMProvidersExhaustedError) as caught:
        asyncio.run(search())
    assert calls == [0, 1]
    summary = analytics.get_run_summary(events_file=runtime.analytics_events_file,
                                       history_file=runtime.run_history_file)
    root = summary["primary_failure"]
    assert summary["failure_count"] == 1
    assert root["failure_id"] == caught.value.jh_failure_id
    assert root["reason_code"] == "LLM_PROVIDER_EXHAUSTED"
    assert root["affected_count"] == 23
    assert root["downstream_events"] == {"not_processed": 23}
    assert root["retryable"] is True


def test_unrelated_terminal_error_does_not_inherit_matcher_cause(runtime, monkeypatch):
    fake_client(monkeypatch, TimeoutError("synthetic timeout"))

    @agent._observe_search
    async def search(dry_run=False):
        observation = analytics.current_search()
        vacancy = {"id": "1", "source": "hh", "title": "Synthetic vacancy"}
        analytics.register_candidates([vacancy, {"id": "2", "source": "hh"}])
        evaluation = await analytics.tracked_call("matcher", observation.run_id, vacancy,
                                                   matcher.evaluate_vacancy, vacancy)
        analytics.record_decision(run_id=observation.run_id, vacancy=vacancy,
                                  decision="deferred_unscored", evaluation=evaluation)
        raise RuntimeError("unrelated failure")

    with pytest.raises(RuntimeError, match="unrelated failure"):
        asyncio.run(search())
    events = read_json_records(runtime.analytics_events_file)
    provider = next(e for e in events if e.get("reason_code") == "LLM_PROVIDER_EXHAUSTED")
    terminal = next(e for e in events if e.get("event") == "stage_failed"
                    and e.get("reason_code") == "UNKNOWN_ROOT_CAUSE")
    assert terminal["failure_id"] != provider["failure_id"]
    assert "parent_failure_id" not in terminal
    pending = next(e for e in events if e.get("decision") == "not_processed")
    assert pending["failure_id"] == terminal["failure_id"]
    summary = analytics.get_run_summary(events_file=runtime.analytics_events_file,
                                       history_file=runtime.run_history_file)
    assert summary["failure_count"] == 2


def test_legitimate_run_limit_has_no_invented_failure(runtime):
    @agent._observe_search
    async def search(dry_run=False):
        observation = analytics.current_search()
        analytics.register_candidates([{"id": str(i), "source": "hh"} for i in range(3)])
        observation.stop_reason = "run_limit"
        observation.ok = True

    asyncio.run(search())
    summary = analytics.get_run_summary(events_file=runtime.analytics_events_file,
                                       history_file=runtime.run_history_file)
    assert summary["status"] == "partial"
    assert summary["stop_reason"] == "RUN_LIMIT"
    assert summary["failure_count"] == 0
    assert summary["primary_failure"] is None
    assert summary["counters"]["not_processed"] == 3


def test_successful_provider_fallback_has_no_failure_root(runtime, monkeypatch):
    specs = [llm_client.ProviderSpec(name=name, base_url="https://invalid.example/v1",
                                    api_key="synthetic") for name in ("primary", "backup")]
    client = llm_client.FallbackLLMClient(specs)
    calls = []

    async def create(index, **kwargs):
        calls.append(index)
        if index == 0:
            raise TimeoutError("synthetic timeout")
        return SimpleNamespace(choices=[])

    transports = [SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(
        create=lambda index=index, **kwargs: create(index, **kwargs)))) for index in range(2)]
    monkeypatch.setattr(client, "_client_for", lambda index: transports[index])

    @agent._observe_search
    async def search(dry_run=False):
        observation = analytics.current_search()
        vacancy = {"id": "1", "source": "hh"}
        analytics.register_candidates([vacancy])
        await analytics.tracked_call("matcher", observation.run_id, vacancy,
                                      client.create_chat_completion, model="synthetic-model")
        analytics.record_decision(run_id=observation.run_id, vacancy=vacancy,
                                  decision="skipped_low_score", evaluation={"score": 20})
        observation.ok = True

    asyncio.run(search())
    assert calls == [0, 1]
    summary = analytics.get_run_summary(events_file=runtime.analytics_events_file,
                                       history_file=runtime.run_history_file)
    assert summary["status"] == "success"
    assert summary["failure_count"] == 0
    assert summary["primary_failure"] is None


def test_terminal_source_failure_correlates_pending_candidates(runtime):
    @agent._observe_search
    async def search(dry_run=False):
        analytics.register_candidates([{"id": str(i), "source": "hh"} for i in range(4)])
        analytics.search_stage("source_collect")
        with analytics.event_context(source="hh"):
            analytics.record_failure("source_collect", TimeoutError(), source="hh", continued=False,
                                     reason_code="SOURCE_TIMEOUT", retryable=True)

    asyncio.run(search())
    summary = analytics.get_run_summary(events_file=runtime.analytics_events_file,
                                       history_file=runtime.run_history_file)
    assert summary["failure_count"] == 1
    root = summary["primary_failure"]
    assert root["reason_code"] == "SOURCE_TIMEOUT"
    assert root["source"] == "hh"
    assert root["affected_count"] == 4
    assert root["downstream_events"] == {"not_processed": 4}


def test_event_destination_is_pinned_to_selected_run(runtime, monkeypatch, tmp_path):
    other = tmp_path / "other-events.jsonl"
    with analytics.observe_search("run-a", "search"):
        monkeypatch.setattr(config, "ANALYTICS_EVENTS_FILE", str(other))
        analytics.record_event({"event": "synthetic_test_event"})
    assert not other.exists()
    event = read_json_records(runtime.analytics_events_file)[0]
    assert event["event"] == "synthetic_test_event" and event["run_id"] == "run-a"
