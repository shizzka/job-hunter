"""Frozen public outcomes for split-20, captured before extracting helpers.

Compare serialized bytes as well as Python values: key/list order, unknowns,
rates and user-visible diagnostic texts are part of the characterization.
Snapshots are read-only; there is deliberately no update mode in these tests.
"""
import copy
from datetime import datetime, timezone
import json
from pathlib import Path
import time

import pytest

import analytics
import config
from run_summary import build_run_summary
from telegram_bot_ui import build_run_summary_text, build_stats_text
from tests.direction_view_fakes import two_directions


SNAPSHOTS = Path(__file__).with_name("fixtures") / "summary_characterization"
NOW = datetime(2026, 10, 25, 12, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def fixed_timezone(monkeypatch):
    # Text formatting uses the host's local zone; workers need the same clock.
    try:
        with monkeypatch.context() as patch:
            patch.setenv("TZ", "Europe/Moscow")
            time.tzset()
            yield
    finally:
        time.tzset()


def serialized(value):
    return (json.dumps(value, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def event(kind, **fields):
    return {"event": kind, "run_id": "pilot", "source": "hh",
            "created_at": "2026-10-24T12:00:00+00:00", **fields}


def decision(vid, name="applied_auto", **fields):
    return event("decision", vacancy_id=str(vid), decision=name, **fields)


def analytics_events():
    rows = [event("search_finished", funnel={"fetched": "30", "new": 24, "invalid": "bad"}),
            event("search_finished", funnel={"fetched": 2, "new": None}),
            event("invitation"), event("questionnaire", success=True, questions_count="3",
                best_guess_count=1, required_count=2),
            event("questionnaire", success=False, questions_count=None), event("ignored")]
    # Every decision category; missing/empty dimensions and explicit reasons.
    names = ["skipped_keyword_filter", "skipped_red_flags", "skipped_low_score", "dry_run_match",
             "deferred_unscored", "already_applied", "manual_questions", "questions_required",
             "apply_uncertain", "apply_failed", "apply_failed_exception", "guard_stop", "", "other"]
    rows += [decision(f"d{i}", name, direction="qa", search_query="QA Engineer",
                      cluster="api_qa", retry_outcome="retry", reason_code="explicit" if i == 0 else "",
                      reason_group="custom" if i == 1 else "") for i, name in enumerate(names)]
    statuses = [("positive", "interview"), ("positive", "offer"), ("positive", "test_task"),
                ("positive", "positive_other"), ("rejected", "rejected"),
                ("pending", "pending_new"), ("pending", "pending_viewed"), ("unknown", "unknown")]
    for i, (bucket, detail) in enumerate(statuses):
        rows += [decision(i, direction="qa", search_query="QA Engineer", resume_variant="A",
                          cluster="api_qa", cover_style="short", retry_reason="viewed_no_response"),
                 event("negotiation_status", vacancy_id=str(i), status_bucket=bucket,
                       status_detail_bucket=detail)]
    rows += [decision("bare", source="habr"),
             decision("latest", direction="qa", resume_variant="B"),
             event("negotiation_status", vacancy_id="latest", status_bucket="positive", status="Приглашение"),
             event("negotiation_status", vacancy_id="latest", status_bucket="pending", status="Не просмотрен"),
             event("negotiation_status", source="habr", vacancy_id="bare", status_bucket="pending",
                   status="Просмотрен"),
             event("negotiation_status", vacancy_id="unlinked", status_bucket="positive", status="Приглашение"),
             decision("electric", direction="electric", source="habr", resume_variant="B"),
             decision("sticky", direction="qa"), decision("sticky", "apply_failed", direction="qa"),
             decision("sticky", "apply_uncertain", direction="qa"),
             decision("sticky", "apply_uncertain", direction="qa"),
             decision("sticky", direction="qa"),
             decision("sticky", run_id="older", direction="qa"),
             decision("old", created_at="2026-10-01T12:00:00+00:00"),
             decision("end", created_at="2026-10-25T12:00:00+00:00"),
             decision("bad-date", created_at="invalid"), decision("no-date", created_at=None)]
    return rows


@pytest.mark.parametrize("case,options", [
    ("all", {"all_time": True, "scope": "*"}),
    ("active", {"all_time": True}),
    ("other", {"all_time": True, "scope": "electric"}),
    ("legacy", {"all_time": True, "scope": "?"}),
    ("recent", {"days": 1, "scope": "*"}),
    ("window", {"all_time": True, "scope": "*",
                "start_at": datetime(2026, 10, 24, 12, tzinfo=timezone.utc), "end_at": NOW}),
    ("empty", {"days": -1, "scope": "*", "end_at": NOW}),
])
def test_analytics_public_outcomes(case, options, tmp_path, monkeypatch):
    two_directions(tmp_path)
    monkeypatch.setattr(analytics, "_now", lambda: NOW)
    monkeypatch.setattr(config, "ANALYTICS_RECENT_DAYS", 7)
    path = tmp_path / "analytics_events.jsonl"
    journal = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in analytics_events()).encode("utf-8")
    path.write_bytes(journal)
    summary = analytics.summarize(events_file=str(path), **options)
    outcome = {"summary": summary, "stats_text": build_stats_text(profile_name="pilot",
               seen_stats={}, analytics_summary=summary, recent_runs=[], selected=True)}
    assert serialized(outcome) == (SNAPSHOTS / f"analytics_{case}.json").read_bytes()
    assert summary == analytics._summarize(events_file=str(path), **options)
    # Memoized public results must remain independent mutable values.
    original = serialized(summary)
    summary["by_source"].clear()
    assert serialized(analytics.summarize(events_file=str(path), **options)) == original
    assert path.read_bytes() == journal


def run_cases():
    base = {"run_id": "pilot", "mode": "search", "ok": True, "status": "finished",
            "started_at": "2026-10-24T12:00:00+00:00", "finished_at": "2026-10-24T12:00:42+00:00",
            "direction": "qa", "direction_title": "QA Engineer", "funnel": {"fetched": 3, "applied": 0}}
    root = event("stage_failed", failure_id="root", reason_code="SOURCE_TIMEOUT", stage="collection",
                 retryable=True, continued=False, collection={"vacancies_retained": 2})
    trace = event("debug_artifact", vacancy_id="nav", artifact_type="apply_trace", artifact_ref="synthetic/trace",
                  stage="VACANCY_NAVIGATION", outcome="error", trace_id="trace")
    isolation = event("hh_ui_isolation", vacancy_id="nav", recovered=False)
    guard = decision("nav", "guard_stop", note="hh:ui_hard_stop")
    uncertain = decision("u", "apply_uncertain", outcome="manual")
    pending = decision("pending", "not_processed", note="hh_recovery_breaker", outcome="not_processed")
    return {
        "success": (base, []),
        "unknown": ({}, []),
        "dry_run": ({**base, "mode": "dry-run"}, []),
        "limited": ({**base, "reason_breakdown": {"run_limit": 3}, "funnel": {"not_processed": 3}}, []),
        "unknown_failure": ({**base, "ok": False, "failure_stage": "matcher"}, []),
        "unknown_progress": ({**base, "ok": False, "funnel": {"evaluated": 2}}, []),
        "unlinked_problems": ({**base, "funnel": {"failed": 1}}, [decision("x", "apply_failed", outcome="failed")]),
        "incomplete": ({**base, "ok": None, "status": "incomplete", "finished_at": None, "funnel": {}},
                       [decision(str(i), name, outcome=name) for i, name in enumerate(
                           ["applied", "skipped", "deferred_unscored", "not_processed", "failed"])]),
        "finish_override": ({**base, "status": "incomplete", "ok": None, "finished_at": None},
                            [event("search_started", enabled_sources=["hh", "habr"], profile_id="saved"),
                             event("search_finished", ok=False, funnel={"evaluated": 1}),
                             event("search_finished", ok=True, status="", funnel={"applied": 1}),
                             event("stage_failed", run_id="foreign", failure_id="foreign")]),
        "start_fallback": ({"run_id": "pilot"}, [event("search_started", enabled_sources=["habr"], profile_id="saved")]),
        "negative_duration": ({**base, "finished_at": "2026-10-23T12:00:00+00:00"}, []),
        "mixed_timezones": ({**base, "finished_at": "2026-10-24T12:01:00"}, []),
        "invalid_time": ({**base, "started_at": "invalid", "finished_at": 123}, []),
        "collection_progress": ({**base, "source_stats": {"hh": {"collection": {"vacancies_retained": 2}}}}, [root]),
        "collection_no_progress": ({**base, "source_stats": {"hh": {"collection": {"vacancies_retained": 0}}}}, [root]),
        "checkpoint_aliases": ({**base, "stage_failures": [{**root, "stage": "checkpoint"},
                               {"failure_id": "saved", "stage": "matcher", "continued": True}]},
                               [root, {**root, "event": "failure_observed", "vacancy_id": "x"},
                                event("stage_failed", failure_id="child", parent_failure_id="root", vacancy_id="y"),
                                decision("z", "deferred_unscored", outcome="deferred_unscored", failure_id="child"),
                                trace, dict(trace), event("debug_artifact")]),
        "dangling_alias_cycle": (base, [event("stage_failed", failure_id="a", parent_failure_id="b"),
                                        event("stage_failed", failure_id="b", parent_failure_id="a"),
                                        event("stage_failed", stage="legacy"),
                                        decision("x", "not_processed", failure_id="missing")]),
        "failure_order": (base, [event("stage_failed", failure_id="recovered", continued=True,
                                      created_at="2026-10-23T00:00:00+00:00"),
                                event("stage_failed", failure_id="missing-time", created_at="bad"), root]),
        "late_uncertainty": ({**base, "funnel": {"applied": 1}},
                             [decision("u"), uncertain, decision("u"), pending, trace]),
        "interrupted_uncertainty": ({**base, "status": "incomplete", "funnel": {"applied": 1}},
                                    [event("application_result", vacancy_id="u", outcome="sent"), uncertain]),
        "recorded_uncertainty": ({**base, "funnel": {"applied": 1, "uncertain": 1}}, [decision("u"), uncertain]),
        "legacy_breaker": (base, [pending, uncertain, {**pending, "vacancy_id": "after"},
                                  {**pending, "vacancy_id": "other", "source": "habr"},
                                  decision("unrelated", "not_processed", note="unclassified")]),
        "navigation": (base, [trace, isolation, guard, pending, dict(trace)]),
        "navigation_recovered": (base, [trace, {**isolation, "recovered": True}, guard]),
        "navigation_missing_trace": (base, [isolation, guard]),
        "navigation_intervening": (base, [trace, decision("nav", "manual_review"), isolation, guard]),
        "navigation_wrong_order": (base, [isolation, trace, guard]),
        "navigation_wrong_stage": (base, [{**trace, "stage": "OTHER"}, isolation, guard]),
        "navigation_failed_outcome": (base, [{**trace, "outcome": "ok"}, isolation, guard]),
        "navigation_explicit_id": (base, [trace, isolation, {**guard, "failure_id": "explicit"}]),
    }


@pytest.mark.parametrize("case", run_cases())
def test_run_summary_public_outcomes(case):
    run, events = copy.deepcopy(run_cases()[case])
    before = serialized([run, events])
    summary = build_run_summary(run, events, profile="pilot")
    outcome = {"summary": summary, "text": build_run_summary_text(summary, profile_name="pilot")}
    assert serialized(outcome) == (SNAPSHOTS / f"run_{case}.json").read_bytes()
    assert serialized(build_run_summary(run, iter(events), profile="pilot")) == serialized(summary)
    assert serialized([run, events]) == before
