"""
Тесты outcome.py (D-002) и hh_resume_pipeline.py (B-004).
"""
import json
import os
from unittest import mock
from datetime import datetime, timedelta

import pytest

import config


class TestStatusBucket:
    """D-002: нормализация статусов переговоров."""

    def test_positive_invitation(self):
        from outcome import status_bucket
        assert status_bucket("Приглашение на собеседование") == "positive"

    def test_positive_offer(self):
        from outcome import status_bucket
        assert status_bucket("Оффер") == "positive"

    def test_positive_test_task(self):
        from outcome import status_bucket
        assert status_bucket("Тестовое задание") == "positive"

    def test_rejected(self):
        from outcome import status_bucket
        assert status_bucket("Отказ") == "rejected"

    def test_rejected_declined(self):
        from outcome import status_bucket
        assert status_bucket("Отклонен работодателем") == "rejected"

    def test_pending_not_viewed(self):
        from outcome import status_bucket
        assert status_bucket("Не просмотрен") == "pending"

    def test_pending_viewed(self):
        from outcome import status_bucket
        assert status_bucket("Просмотрен") == "pending"

    def test_unknown(self):
        from outcome import status_bucket
        assert status_bucket("какой-то новый статус") == "unknown"

    def test_empty(self):
        from outcome import status_bucket
        assert status_bucket("") == "unknown"
        assert status_bucket(None) == "unknown"

    def test_case_insensitive(self):
        from outcome import status_bucket
        assert status_bucket("ПРИГЛАШЕНИЕ НА СОБЕСЕДОВАНИЕ") == "positive"
        assert status_bucket("отказ") == "rejected"


class TestStatusDetailBucket:
    def test_interview(self):
        from outcome import status_detail_bucket
        assert status_detail_bucket("Приглашение на собеседование") == "interview"

    def test_offer(self):
        from outcome import status_detail_bucket
        assert status_detail_bucket("Оффер") == "offer"

    def test_test_task(self):
        from outcome import status_detail_bucket
        assert status_detail_bucket("Тестовое задание") == "test_task"

    def test_pending_viewed(self):
        from outcome import status_detail_bucket
        assert status_detail_bucket("Просмотрен") == "pending_viewed"

    def test_pending_new(self):
        from outcome import status_detail_bucket
        assert status_detail_bucket("Не просмотрен") == "pending_new"


class TestDecisionConstants:
    """D-002: decision-константы импортируются и группируются."""

    def test_constants_exist(self):
        from outcome import (
            DECISION_APPLIED_AUTO,
            DECISION_ALREADY_APPLIED,
            DECISION_SKIPPED_KEYWORD,
            DECISION_SKIPPED_RED_FLAGS,
            DECISION_SKIPPED_LOW_SCORE,
            DECISION_APPLY_FAILED,
            DECISION_QUESTIONS_REQUIRED,
            DECISION_DRY_RUN_MATCH,
        )
        assert DECISION_APPLIED_AUTO == "applied_auto"
        assert DECISION_ALREADY_APPLIED == "already_applied"
        assert DECISION_SKIPPED_KEYWORD == "skipped_keyword_filter"

    def test_groups_no_overlap(self):
        from outcome import DECISIONS_AUTO_APPLIED, DECISIONS_MANUAL, DECISIONS_FILTERED
        assert not (DECISIONS_AUTO_APPLIED & DECISIONS_MANUAL)
        assert not (DECISIONS_AUTO_APPLIED & DECISIONS_FILTERED)
        assert not (DECISIONS_MANUAL & DECISIONS_FILTERED)


class TestResumePipeline:
    """B-004: regression для hh_resume_pipeline."""

    @pytest.fixture(autouse=True)
    def isolated_pipeline(self, tmp_path):
        pipeline_file = str(tmp_path / "hh_resume_pipeline.json")
        with mock.patch.object(config, "HH_RESUME_PIPELINE_FILE", pipeline_file), \
             mock.patch.object(config, "HH_RESUME_PIPELINE_ENABLED", True), \
             mock.patch.object(config, "HH_PRIMARY_RESUME_TITLE", "QA Engineer Resume"), \
             mock.patch.object(config, "HH_PRIMARY_RESUME_ID", "111"), \
             mock.patch.object(config, "HH_SECONDARY_RESUME_TITLE", "Fun Resume"), \
             mock.patch.object(config, "HH_SECONDARY_RESUME_ID", "222"), \
             mock.patch.object(config, "HH_TERTIARY_RESUME_TITLE", ""), \
             mock.patch.object(config, "HH_TERTIARY_RESUME_ID", ""):
            import hh_resume_pipeline
            hh_resume_pipeline._state = None
            yield tmp_path
            hh_resume_pipeline._state = None

    def test_get_variants(self):
        from hh_resume_pipeline import get_variants
        variants = get_variants()
        assert len(variants) == 2
        assert variants[0]["name"] == "normal"
        assert variants[1]["name"] == "fun"

    def test_get_next_variant_first(self):
        from hh_resume_pipeline import get_next_variant
        v = get_next_variant("hh:999")
        assert v is not None
        assert v["name"] == "normal"

    def test_record_and_advance(self):
        from hh_resume_pipeline import record_successful_apply, get_next_variant, get_attempt_count

        vacancy = {"id": "hh:999", "title": "QA", "company": "A", "url": "https://hh.ru/vacancy/999"}
        variant = {"name": "normal", "title": "QA Engineer Resume", "id": "111"}

        record_successful_apply(vacancy, variant)
        assert get_attempt_count("hh:999") == 1

        next_v = get_next_variant("hh:999")
        assert next_v is not None
        assert next_v["name"] == "fun"

    def test_pipeline_exhausted(self):
        from hh_resume_pipeline import record_successful_apply, get_next_variant

        vacancy = {"id": "hh:999", "title": "QA", "company": "A", "url": ""}
        record_successful_apply(vacancy, {"name": "normal", "title": "", "id": "111"})
        record_successful_apply(vacancy, {"name": "fun", "title": "", "id": "222"})

        assert get_next_variant("hh:999") is None

    def test_mark_terminal(self):
        from hh_resume_pipeline import _ensure_entry, mark_terminal, _entry

        vacancy = {"id": "hh:888", "title": "Dev", "company": "B", "url": ""}
        _ensure_entry(vacancy)
        mark_terminal("hh:888", "positive_response")

        entry = _entry("hh:888")
        assert entry["completed_reason"] == "positive_response"

    def test_sync_positive_status(self):
        from hh_resume_pipeline import _ensure_entry, record_successful_apply, sync_negotiation_statuses, _entry

        vacancy = {"id": "hh:777", "title": "QA", "company": "C", "url": ""}
        _ensure_entry(vacancy)
        record_successful_apply(vacancy, {"name": "normal", "title": "", "id": "111"})

        sync_negotiation_statuses([
            {"id": "hh:777", "title": "QA", "company": "C", "url": "", "status": "Приглашение на собеседование"},
        ])

        entry = _entry("hh:777")
        assert entry["completed_reason"] == "positive_response"

    def test_sync_rejected_opens_retry(self):
        from hh_resume_pipeline import _ensure_entry, record_successful_apply, sync_negotiation_statuses, _entry

        vacancy = {"id": "hh:666", "title": "QA", "company": "D", "url": ""}
        _ensure_entry(vacancy)
        record_successful_apply(vacancy, {"name": "normal", "title": "", "id": "111"})

        sync_negotiation_statuses([
            {"id": "hh:666", "title": "QA", "company": "D", "url": "", "status": "Отказ"},
        ])

        entry = _entry("hh:666")
        # Не terminal — есть ещё вариант "fun"
        assert entry["completed_reason"] == ""
        assert entry["next_retry_at"] != ""

    def test_resolve_variants_by_title(self):
        from hh_resume_pipeline import resolve_variants
        with mock.patch.object(config, "HH_PRIMARY_RESUME_ID", ""), \
             mock.patch.object(config, "HH_SECONDARY_RESUME_ID", ""):
            resumes = [
                {"id": "aaa", "title": "QA Engineer Resume"},
                {"id": "bbb", "title": "Fun Resume"},
            ]
            resolved = resolve_variants(resumes)
            assert resolved[0]["id"] == "aaa"
            assert resolved[1]["id"] == "bbb"

    def test_remember_resolved_variants_preserves_known_ids(self):
        from hh_resume_pipeline import remember_resolved_variants, get_resolved_variants

        remember_resolved_variants(
            [
                {"name": "normal", "title": "QA Engineer Resume", "id": "aaa"},
                {"name": "fun", "title": "Fun Resume", "id": "bbb"},
            ]
        )
        remember_resolved_variants(
            [
                {"name": "normal", "title": "QA Engineer Resume", "id": ""},
                {"name": "fun", "title": "Fun Resume", "id": ""},
            ]
        )

        resolved = get_resolved_variants()
        assert resolved[0]["id"] == "aaa"
        assert resolved[1]["id"] == "bbb"
