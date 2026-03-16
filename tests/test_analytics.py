"""
Тесты аналитического контура (D-001 частично).

Проверяем: запись событий, summarize, event taxonomy.
"""
import json
import os
import tempfile
from unittest import mock

import pytest

import analytics
import config


@pytest.fixture(autouse=True)
def isolated_analytics(tmp_path):
    """Изолируем аналитику от боевых файлов."""
    events_file = str(tmp_path / "analytics_events.jsonl")
    state_file = str(tmp_path / "analytics_state.json")

    with mock.patch.object(config, "ANALYTICS_EVENTS_FILE", events_file), \
         mock.patch.object(config, "ANALYTICS_STATE_FILE", state_file), \
         mock.patch.object(config, "ANALYTICS_ENABLED", True):
        analytics._state = None  # сбросить кэш
        yield tmp_path
        analytics._state = None


def _read_events(tmp_path) -> list[dict]:
    events_file = str(tmp_path / "analytics_events.jsonl")
    if not os.path.exists(events_file):
        return []
    with open(events_file) as f:
        return [json.loads(line) for line in f if line.strip()]


class TestRecordDecision:

    def test_basic_decision(self, isolated_analytics):
        vacancy = {"id": "hh:123", "source": "hh", "title": "QA", "company": "Acme", "url": "https://hh.ru/vacancy/123"}
        analytics.record_decision(
            run_id="test-run-1",
            vacancy=vacancy,
            decision="applied_auto",
        )
        events = _read_events(isolated_analytics)
        assert len(events) == 1
        assert events[0]["event"] == "decision"
        assert events[0]["decision"] == "applied_auto"
        assert events[0]["vacancy_id"] == "hh:123"

    def test_keyword_filter_decision(self, isolated_analytics):
        vacancy = {"id": "hh:456", "source": "hh", "title": "Повар", "company": "Кафе"}
        analytics.record_decision(
            run_id="test-run-1",
            vacancy=vacancy,
            decision="skipped_keyword_filter",
            note="exclude_keywords",
        )
        events = _read_events(isolated_analytics)
        assert events[0]["decision"] == "skipped_keyword_filter"
        assert events[0]["note"] == "exclude_keywords"

    def test_military_filter_decision(self, isolated_analytics):
        vacancy = {"id": "hh:789", "source": "hh", "title": "Тестировщик ВПК", "company": "Оборонка"}
        analytics.record_decision(
            run_id="test-run-1",
            vacancy=vacancy,
            decision="skipped_keyword_filter",
            note="military_redflag",
        )
        events = _read_events(isolated_analytics)
        assert events[0]["note"] == "military_redflag"


class TestSearchEvents:

    def test_search_started(self, isolated_analytics):
        analytics.record_search_started(
            run_id="test-run-1",
            mode="search",
            enabled_sources=["hh.ru", "GeekJob"],
        )
        events = _read_events(isolated_analytics)
        assert len(events) == 1
        assert events[0]["event"] == "search_started"
        assert events[0]["enabled_sources"] == ["hh.ru", "GeekJob"]

    def test_search_finished(self, isolated_analytics):
        analytics.record_search_finished(
            run_id="test-run-1",
            mode="search",
            result={"found": 10, "applied": 3, "manual": 1, "ok": True},
        )
        events = _read_events(isolated_analytics)
        assert len(events) == 1
        assert events[0]["event"] == "search_finished"
        assert events[0]["found"] == 10
        assert events[0]["applied"] == 3


class TestSummarize:

    def _seed_events(self, tmp_path, events: list[dict]):
        events_file = str(tmp_path / "analytics_events.jsonl")
        with open(events_file, "w") as f:
            for e in events:
                e.setdefault("created_at", "2026-03-16T12:00:00")
                f.write(json.dumps(e) + "\n")

    def test_empty_summarize(self, isolated_analytics):
        s = analytics.summarize(days=30)
        assert s["events"] == 0
        assert s["decisions"] == 0
        assert s["search_runs"] == 0

    def test_summarize_counts(self, isolated_analytics):
        self._seed_events(isolated_analytics, [
            {"event": "search_finished", "run_id": "r1", "mode": "search"},
            {"event": "decision", "decision": "applied_auto", "source": "hh", "search_query": "QA"},
            {"event": "decision", "decision": "skipped_keyword_filter", "source": "hh"},
            {"event": "decision", "decision": "skipped_red_flags", "source": "habr"},
            {"event": "decision", "decision": "skipped_low_score", "source": "geekjob"},
            {"event": "decision", "decision": "dry_run_match", "source": "hh"},
            {"event": "invitation", "vacancy_id": "hh:1"},
        ])
        s = analytics.summarize(days=30)
        assert s["search_runs"] == 1
        assert s["decisions"] == 5
        assert s["auto_applied"] == 1
        assert s["keyword_filtered"] == 1
        assert s["red_flagged"] == 1
        assert s["low_score"] == 1
        assert s["dry_run_matched"] == 1
        assert s["invitations"] == 1
        assert "hh" in s["by_source"]
        assert "QA" in s["by_query"]

    def test_summarize_funnel(self, isolated_analytics):
        self._seed_events(isolated_analytics, [
            {"event": "decision", "decision": "applied_auto", "source": "hh",
             "vacancy_id": "hh:1", "title": "QA", "company": "A", "url": "https://hh.ru/1"},
            {"event": "negotiation_status", "vacancy_id": "hh:1", "source": "hh",
             "title": "QA", "company": "A", "url": "https://hh.ru/1",
             "status": "Приглашение на собеседование", "status_bucket": "positive", "prev_status": ""},
        ])
        s = analytics.summarize(days=30)
        assert s["funnel"]["applied"] == 1
        assert s["funnel"]["positive"] == 1
        assert s["funnel"]["positive_rate"] == 100.0
