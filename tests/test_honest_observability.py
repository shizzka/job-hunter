"""C13: retained work and target actions must be visible independently of execution."""
import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import agent
import analytics
import apply_orchestrator
import config
import matcher
import prompt_blocks
import search_pipeline
from debug_trace import ApplyTrace
from run_summary import build_run_summary
from state_store.private_journal import read_json_records
from telegram_bot_ui import build_run_summary_text, format_run_summary
from tests.test_cover_grounding import client_for, isolated_candidate
from tests.test_observability import search_home


def test_discarded_source_reports_lost_work_and_unattempted_combinations(monkeypatch, caplog):
    monkeypatch.setattr(config, "HH_ENABLED", True)
    for name in ("SUPERJOB_ENABLED", "HABR_ENABLED", "GEEKJOB_ENABLED", "HH_SEARCH_REMOTE_ONLY"):
        monkeypatch.setattr(config, name, False)
    monkeypatch.setattr(config, "SEARCH_PROFILES", [{"area": str(i)} for i in range(5)])
    monkeypatch.setattr(config, "SEARCH_QUERIES", [f"query {i}" for i in range(17)])
    monkeypatch.setattr(config, "SEARCH_PAGES", 3)
    monkeypatch.setattr(search_pipeline.hh_guard, "can_collect", lambda: (True, ""))
    monkeypatch.setattr(search_pipeline.seen, "is_seen", lambda vid: False)
    monkeypatch.setattr(search_pipeline, "office_log", AsyncMock())
    client = SimpleNamespace(is_logged_in=AsyncMock(return_value=True),
        search_vacancies=AsyncMock(return_value=[{"id": str(i)} for i in range(11)]),
        consume_antibot_signal=Mock(side_effect=[None, ValueError("PRIVATE_BODY")]))
    stats = {}
    with caplog.at_level(logging.WARNING), analytics.observe_search("discard", "search") as observation:
        observation.result["source_stats"] = stats
        result = asyncio.run(search_pipeline.collect_all(client, None, None, None, source_stats=stats))
        failure, = observation.failures
    assert result == [] and client.search_vacancies.await_count == 2
    assert failure["continued"] is False and failure["source_stopped"] is True
    assert failure["reason_code"] == "SOURCE_DISCARDED"
    progress = stats["hh"]["collection"]
    assert progress["combinations_unattempted"] == 84
    assert progress["pages_attempted"] == 2
    assert progress["vacancies_discarded"] == 11 and progress["vacancies_retained"] == 0
    assert "source_discarded=true" in caplog.text and "combinations_unattempted=84" in caplog.text
    assert "continued=true" not in caplog.text and "PRIVATE_BODY" not in caplog.text
    observation.ok = True
    summary = build_run_summary(observation.entry())
    assert summary["status"] == "failed"  # Raw fetched data was discarded, not progress.
    assert "комбинаций не опрошено: 84" in summary["zero_apply_diagnosis"]
    assert "завершён с потерями" in format_run_summary(observation.entry())


@pytest.mark.parametrize("limit", [12000, None])
def test_kb_logs_only_sections_surviving_final_cut(monkeypatch, caplog, limit):
    monkeypatch.setenv("KB_RETRIEVAL_ENABLED", "0")
    sections = [{"num": n, "title": f"PRIVATE_TITLE_{n}", "content": "PRIVATE_CONTENT"} for n in range(1, 7)]
    monkeypatch.setattr(prompt_blocks, "_load_kb_filterable", lambda: ("x" * 17344, sections))
    monkeypatch.setattr(prompt_blocks, "select_kb_sections", AsyncMock(return_value=list(range(1, 7))))
    with caplog.at_level(logging.INFO):
        block = asyncio.run(prompt_blocks.build_filtered_kb_block("QA", None, max_sections=6, limit_chars=limit))
    record, = [r for r in caplog.records if "KB prompt" in r.message]
    assert "sections_full=[1, 2, 3, 4, 5, 6]" in record.message
    assert "sections_dropped=0" in record.message
    assert f"chars_included={len(block)}" in record.message
    assert (record.levelno == logging.WARNING) is bool(limit)
    assert "PRIVATE_" not in record.message and "picked sections" not in caplog.text


def test_kb_partial_section_is_not_reported_as_fully_included(caplog):
    section = {"num": 1, "title": "QA", "content": "x" * 100}
    full = prompt_blocks._format_kb_block("", [section], limit_chars=None)
    with caplog.at_level(logging.WARNING):
        prompt_blocks._format_kb_block("", [section], limit_chars=len(full) - 20)
    assert "sections_full=[]" in caplog.text and "sections_partial=[1]" in caplog.text
    assert "sections_dropped=0" in caplog.text


@pytest.mark.parametrize("result,expected", [
    ({"ok": True, "already_applied": True}, "already_applied"),
    ({"ok": True}, "sent"),
    ({"ok": False, "uncertain": True}, "uncertain"),
    ({"ok": False}, "failed"),
])
def test_trace_separates_processing_from_application(tmp_path, monkeypatch, result, expected):
    monkeypatch.setattr(apply_orchestrator.company_blacklist, "is_blocked", lambda company: False)
    trace = ApplyTrace.create(home_dir=str(tmp_path), source="hh", vacancy_id="42", profile="qa", mode="test")
    client = SimpleNamespace(_page=None, apply_to_vacancy=AsyncMock(return_value=result))
    actual = asyncio.run(apply_orchestrator.dispatch_apply({"id": "42", "source": "hh", "url": "https://hh.ru/vacancy/42"},
        "cover", hh_client=client, trace=trace, preferred_resume_id="synthetic"))
    assert actual["ok"] == result["ok"]
    text = trace.summary_path.read_text()
    assert f"Processing result: {'OK' if result['ok'] else 'FAIL'}" in text
    assert f"Application outcome: {expected}" in text
    terminal = read_json_records(trace.jsonl_path)[-1]
    assert terminal["application_outcome"] == expected


def test_trace_summary_reflects_uncertainty_arriving_during_finish(tmp_path, monkeypatch):
    monkeypatch.setattr(apply_orchestrator.company_blacklist, "is_blocked", lambda company: False)
    trace = ApplyTrace.create(home_dir=str(tmp_path), source="hh", vacancy_id="42", profile="qa", mode="test")
    original_finish = trace.finish
    late = False
    def finish(**fields):
        nonlocal late
        original_finish(**fields)
        late = True
    monkeypatch.setattr(trace, "finish", finish)
    monkeypatch.setattr(apply_orchestrator, "apply_result_with_current_uncertainty",
        lambda vacancy, client, result, **kwargs: {**result, "ok": False, "uncertain": True} if late else result)
    client = SimpleNamespace(_page=None, apply_to_vacancy=AsyncMock(return_value={"ok": True}))
    result = asyncio.run(apply_orchestrator.dispatch_apply({"id": "42", "source": "hh", "url": "https://hh.ru/vacancy/42"},
        "cover", hh_client=client, trace=trace, preferred_resume_id="synthetic"))
    assert result["uncertain"] is True and client.apply_to_vacancy.await_count == 1
    text = trace.summary_path.read_text()
    assert "Application outcome: uncertain" in text and "Application outcome: sent" not in text
    assert read_json_records(trace.jsonl_path)[-1]["application_outcome"] == "uncertain"


@pytest.mark.parametrize("verdict,guard,check_failed", [
    (RuntimeError("PRIVATE_TRANSPORT"), False, True),
    ('{"verdict":"unsupported","sentences":[]}', True, False),
    ('{"verdict":"supported","sentences":[]}', False, True),
])
def test_grounding_failure_is_not_evidence_of_overclaim(isolated_candidate, monkeypatch, caplog, verdict, guard, check_failed):
    monkeypatch.setattr(config, "ANALYTICS_ENABLED", True)
    client = client_for("Проверяю REST API в Postman. Обсудим детали?", verdict)
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    with caplog.at_level(logging.WARNING):
        result = asyncio.run(matcher.generate_cover_letter({"title": "QA", "id": "synthetic"}))
    meta = matcher.analyze_cover_letter(result)
    assert meta["overclaim_guard"] is guard
    assert meta["grounding_check_failed"] is check_failed
    assert result == matcher._fallback_cover_letter({})
    assert f"check_failed={str(check_failed).lower()}" in caplog.text
    assert "PRIVATE_TRANSPORT" not in caplog.text
    analytics.record_decision(run_id="grounding", vacancy={"id": "1", "source": "hh"},
                              decision="manual_review", evaluation=meta)
    event = read_json_records(config.ANALYTICS_EVENTS_FILE)[-1]
    assert event["overclaim_guard"] is guard and event["grounding_check_failed"] is check_failed
    assert event["grounding_status"] == meta["grounding_status"]


def test_history_checkpoint_is_unknown_and_zero_final_explains_why(search_home):
    @agent._observe_search
    async def search(dry_run=False):
        observation = analytics.current_search()
        vacancy = {"id": "1", "source": "hh"}
        observation.result = {"found": 1, "source_stats": {"hh": {"new": 1, "fetched": 1}}}
        analytics.register_candidates([vacancy])
        analytics.record_decision(run_id=observation.run_id, vacancy=vacancy, decision="skipped_low_score")
        agent._record_search_run(observation.result, dry_run=False, ok=True)
    asyncio.run(search())
    start, final = read_json_records(config.RUN_HISTORY_FILE)
    assert start["ok"] is None and start["execution_status"] == "incomplete"
    assert start["application_outcome"] == "unknown" and start["applied"] is None
    assert final["ok"] is True and final["execution_status"] == "completed"
    assert final["application_outcome"] == "no_applications"
    assert "matcher: low score: 1" in final["zero_apply_diagnosis"]
    summary = build_run_summary(final)
    assert summary["application_outcome"] == "no_applications"
    assert "matcher: low score: 1" in build_run_summary_text(summary, profile_name="qa")
    assert "откликов нет" in format_run_summary(final)
    assert "успешно" not in format_run_summary(final)
    assert "Итог: ✅ успешно" not in build_run_summary_text(summary, profile_name="qa")


@pytest.mark.parametrize("run,expected", [
    ({"found": 0, "new": 0}, "новых кандидатов нет"),
    ({"found": 0, "stage_failures": [{"stage": "collection", "source": "hh", "reason_code": "SOURCE_DISCARDED"}]}, "SOURCE_DISCARDED"),
    ({"found": 0, "source_stats": {"hh": {"stop_reason": "PRIVATE_DETAILS"}}}, "источник hh пропущен/остановлен"),
    ({"found": 1, "reason_breakdown": {"guard_stop": 1}}, "guard stop: 1"),
    ({"found": 1, "mode": "dry-run"}, "dry-run"),
    ({"found": 0, "reason_breakdown": {"apply_uncertain": 1}}, "требуется ручная сверка: 1"),
    ({"found": 1}, "причина не классифицирована"),
])
def test_zero_apply_diagnosis_covers_empty_collection_and_dry_run(run, expected):
    text = analytics.zero_apply_diagnosis({"applied": 0, **run})
    assert expected in text and "PRIVATE_DETAILS" not in text
