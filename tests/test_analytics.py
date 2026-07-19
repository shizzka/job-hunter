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
            evaluation={
                "score": 70,
                "cluster": "api_qa",
                "cover_style": "api_qa",
                "cover_letter_hash": "abc123",
                "cover_letter_length": 120,
                "cover_letter_features": {"mentions_api": True},
                "fallback_cover_letter": True,
                "overclaim_guard": False,
            },
        )
        events = _read_events(isolated_analytics)
        assert len(events) == 1
        assert events[0]["event"] == "decision"
        assert events[0]["decision"] == "applied_auto"
        assert events[0]["vacancy_id"] == "hh:123"
        assert events[0]["cluster"] == "api_qa"
        assert events[0]["cover_style"] == "api_qa"
        assert events[0]["cover_letter_hash"] == "abc123"
        assert events[0]["cover_letter_features"] == {"mentions_api": True}
        assert events[0]["fallback_cover_letter"] is True

    def test_retry_decision_records_reason(self, isolated_analytics):
        vacancy = {
            "id": "hh:retry",
            "source": "hh",
            "title": "QA",
            "company": "Acme",
            "_hh_retry": True,
            "_hh_last_status": "Просмотрен",
            "_hh_retry_reason": "viewed_no_response",
            "_hh_retry_outcome": "viewed_no_response",
            "_hh_retry_after": "2026-01-04T10:00:00",
        }
        analytics.record_decision(
            run_id="test-run-1",
            vacancy=vacancy,
            decision="applied_auto",
        )

        event = _read_events(isolated_analytics)[0]
        assert event["is_retry"] is True
        assert event["last_known_status"] == "Просмотрен"
        assert event["retry_reason"] == "viewed_no_response"
        assert event["retry_outcome"] == "viewed_no_response"
        assert event["retry_after"] == "2026-01-04T10:00:00"

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
        created_at = analytics._now().isoformat(timespec="seconds")
        with open(events_file, "w") as f:
            for e in events:
                e.setdefault("created_at", created_at)
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
            {"event": "decision", "decision": "already_applied", "source": "hh"},
            {"event": "decision", "decision": "skipped_keyword_filter", "source": "hh"},
            {"event": "decision", "decision": "skipped_red_flags", "source": "habr"},
            {"event": "decision", "decision": "skipped_low_score", "source": "geekjob"},
            {"event": "decision", "decision": "dry_run_match", "source": "hh"},
            {"event": "invitation", "vacancy_id": "hh:1"},
        ])
        s = analytics.summarize(days=30)
        assert s["search_runs"] == 1
        assert s["decisions"] == 6
        assert s["auto_applied"] == 1
        assert s["keyword_filtered"] == 1
        assert s["red_flagged"] == 1
        assert s["low_score"] == 1
        assert s["dry_run_matched"] == 1
        assert s["invitations"] == 1
        assert s["by_source"]["hh"]["rejected"] == 1
        assert "hh" in s["by_source"]
        assert "QA" in s["by_query"]


    def test_filter_audit_replays_current_classifier(self, isolated_analytics):
        self._seed_events(isolated_analytics, [
            {
                "event": "decision",
                "decision": "applied_auto",
                "source": "hh",
                "title": "Сервисный инженер",
                "company": "RepairCo",
                "snippet": "Тестирование оборудования после ремонта",
                "details": "Диагностика плат, пайка и ремонт устройств.",
                "score": 72,
                "should_apply": True,
            },
            {
                "event": "decision",
                "decision": "skipped_low_score",
                "source": "hh",
                "title": "QA Manual Engineer",
                "company": "Acme",
                "snippet": "Manual QA, API, SQL",
                "score": 55,
                "should_apply": False,
            },
            {
                "event": "decision",
                "decision": "skipped_keyword_filter",
                "source": "hh",
                "title": "QA Engineer API",
                "company": "ApiCo",
                "snippet": "REST API, Postman, Swagger",
                "score": None,
                "should_apply": False,
                "note": "relevant_keywords",
            },
            {
                "event": "decision",
                "decision": "applied_auto",
                "source": "hh",
                "title": "QA Engineer API",
                "company": "GoodCo",
                "snippet": "REST API, Postman, Swagger",
                "score": 80,
                "should_apply": True,
            },
        ])

        audit = analytics.audit_filters(days=30)

        assert audit["decisions"] == 4
        assert ("reject_non_qa", 1) in audit["by_cluster"]
        assert audit["would_block_allowed_or_manual_count"] == 1
        assert audit["would_block_allowed_or_manual"][0]["title"] == "Сервисный инженер"
        assert audit["low_score_viable_count"] == 1
        assert audit["low_score_viable"][0]["title"] == "QA Manual Engineer"
        assert audit["keyword_filtered_viable_count"] == 1
        assert audit["keyword_filtered_viable"][0]["title"] == "QA Engineer API"

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
        assert s["interview_statuses"] == 1

    def test_summarize_detail_buckets(self, isolated_analytics):
        self._seed_events(isolated_analytics, [
            {"event": "negotiation_status", "vacancy_id": "hh:1", "source": "hh", "status": "Собеседование", "status_bucket": "positive"},
            {"event": "negotiation_status", "vacancy_id": "hh:2", "source": "hh", "status": "Оффер", "status_bucket": "positive"},
            {"event": "negotiation_status", "vacancy_id": "hh:3", "source": "hh", "status": "Тестовое задание", "status_bucket": "positive"},
            {"event": "negotiation_status", "vacancy_id": "hh:4", "source": "hh", "status": "Не просмотрен", "status_bucket": "pending"},
            {"event": "negotiation_status", "vacancy_id": "hh:5", "source": "hh", "status": "Просмотрен", "status_bucket": "pending"},
        ])
        s = analytics.summarize(days=30)
        assert s["interview_statuses"] == 1
        assert s["offer_statuses"] == 1
        assert s["test_task_statuses"] == 1
        assert s["pending_new_statuses"] == 1
        assert s["pending_viewed_statuses"] == 1

    def test_funnel_ignores_unlinked_statuses(self, isolated_analytics):
        self._seed_events(isolated_analytics, [
            {"event": "decision", "decision": "applied_auto", "source": "hh",
             "vacancy_id": "hh:1", "title": "QA", "company": "A", "url": "https://hh.ru/1"},
            {"event": "negotiation_status", "vacancy_id": "hh:1", "source": "hh",
             "status": "Собеседование", "status_bucket": "positive"},
            {"event": "negotiation_status", "vacancy_id": "hh:2", "source": "hh",
             "status": "Просмотрен", "status_bucket": "pending"},
        ])
        s = analytics.summarize(days=30)
        assert s["funnel"]["applied"] == 1
        assert s["funnel"]["viewed"] == 1
        assert s["funnel"]["positive"] == 1
        assert s["funnel"]["pending"] == 0
        assert s["funnel"]["response_rate"] == 100.0

    def test_summarize_retry_reason_conversion(self, isolated_analytics):
        self._seed_events(isolated_analytics, [
            {
                "event": "decision",
                "decision": "applied_auto",
                "source": "hh",
                "vacancy_id": "hh:retry",
                "title": "QA",
                "company": "A",
                "url": "https://hh.ru/retry",
                "retry_reason": "viewed_no_response",
                "retry_outcome": "viewed_no_response",
            },
            {
                "event": "negotiation_status",
                "vacancy_id": "hh:retry",
                "source": "hh",
                "title": "QA",
                "company": "A",
                "url": "https://hh.ru/retry",
                "status": "Приглашение на собеседование",
                "status_bucket": "positive",
            },
        ])

        s = analytics.summarize(days=30)
        bucket = s["by_retry_reason"]["viewed_no_response"]
        assert bucket["decisions"] == 1
        assert bucket["auto_applied"] == 1
        assert bucket["viewed"] == 1
        assert bucket["positive"] == 1
        assert bucket["response_rate"] == 100.0
        assert bucket["positive_rate"] == 100.0

    def test_summarize_clusters_cover_styles_and_not_viewed(self, isolated_analytics):
        self._seed_events(isolated_analytics, [
            {
                "event": "decision",
                "decision": "applied_auto",
                "source": "hh",
                "vacancy_id": "hh:10",
                "title": "QA API",
                "company": "A",
                "url": "https://hh.ru/10",
                "cluster": "api_qa",
                "cover_style": "api_qa",
                "response_probability_score": 76,
            },
            {
                "event": "negotiation_status",
                "vacancy_id": "hh:10",
                "source": "hh",
                "title": "QA API",
                "company": "A",
                "url": "https://hh.ru/10",
                "status": "Не просмотрен",
                "status_bucket": "pending",
                "status_detail_bucket": "pending_new",
            },
        ])

        s = analytics.summarize(days=30)

        assert s["funnel"]["applied"] == 1
        assert s["funnel"]["viewed"] == 0
        assert s["funnel"]["not_viewed"] == 1
        assert s["funnel"]["response_rate"] == 0.0
        assert s["by_cluster"]["api_qa"]["auto_applied"] == 1
        assert s["by_cluster"]["api_qa"]["not_viewed"] == 1
        assert s["by_cover_style"]["api_qa"]["auto_applied"] == 1
        assert s["by_cover_style"]["api_qa"]["not_viewed"] == 1


class TestQuestionnaireAnalytics:

    def test_record_questionnaire_and_summarize(self, isolated_analytics):
        analytics.record_questionnaire(
            run_id="run-questions",
            vacancy={
                "id": "hh:77",
                "source": "hh",
                "source_label": "hh.ru",
                "title": "QA",
                "company": "Acme",
                "url": "https://hh.ru/77",
            },
            question_answers=[
                {
                    "question": "Есть ли опыт API?",
                    "answer": "Да, REST API и Postman",
                    "control": "textarea",
                    "required": True,
                },
                {
                    "question": "Готовы к тестовому?",
                    "answer": "Да",
                    "control": "radio",
                    "best_guess": True,
                    "starred": True,
                },
            ],
            success=True,
            reason="Отклик отправлен",
        )

        events = _read_events(isolated_analytics)
        assert events[0]["event"] == "questionnaire"
        assert events[0]["success"] is True
        assert events[0]["questions_count"] == 2
        assert events[0]["required_count"] == 1
        assert events[0]["starred_count"] == 1
        assert events[0]["best_guess_count"] == 1
        assert events[0]["question_answers"][0]["question_text"] == "Есть ли опыт API?"

        s = analytics.summarize(days=30)
        assert s["questionnaires"] == 1
        assert s["questionnaire_successes"] == 1
        assert s["questionnaire_failures"] == 0
        assert s["questionnaire_questions"] == 2
        assert s["questionnaire_required"] == 1
        assert s["questionnaire_best_guess"] == 1
