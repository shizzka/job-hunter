"""
Тесты reporting.py (B-005).

Проверяем форматирование статистики, source breakdown, воронку.
"""
import pytest
from reporting import (
    source_label,
    format_compact_source_counts,
    format_source_progress,
    _format_stats_source_breakdown,
    _format_analytics_source_breakdown,
    _format_top_query_breakdown,
    _format_resume_variant_breakdown,
    _format_run_source_stats,
    SOURCE_ORDER,
    SOURCE_LABELS,
)


class TestSourceLabels:

    def test_all_sources_have_labels(self):
        for src in SOURCE_ORDER:
            assert source_label(src) != ""
            assert source_label(src, short=True) != ""

    def test_unknown_source_passthrough(self):
        assert source_label("newplatform") == "newplatform"

    def test_compact_counts_ordered(self):
        counts = {"superjob": 2, "hh": 10, "habr": 5, "geekjob": 3}
        result = format_compact_source_counts(counts)
        # hh comes first in SOURCE_ORDER
        assert result.index("hh") < result.index("Хабр")
        assert "10" in result
        assert "5" in result

    def test_compact_counts_empty(self):
        assert format_compact_source_counts({}) == "0"

    def test_source_progress(self):
        result = format_source_progress("Проверяю", "hh", 3, 10)
        assert "hh" in result
        assert "3/10" in result


class TestStatsBreakdown:

    def test_source_breakdown(self):
        by_source = {
            "hh": {"total": 50, "applied": 10, "manual": 5, "skipped": 35},
            "habr": {"total": 20, "applied": 3, "manual": 2, "skipped": 15},
        }
        lines = _format_stats_source_breakdown(by_source)
        assert len(lines) == 2
        assert "hh.ru" in lines[0]
        assert "50" in lines[0]

    def test_analytics_breakdown(self):
        by_source = {
            "hh": {"decisions": 100, "auto_applied": 20, "manual": 10, "positive": 3, "rejected": 5},
        }
        lines = _format_analytics_source_breakdown(by_source)
        assert len(lines) == 1
        assert "100" in lines[0]
        assert "20" in lines[0]

    def test_run_source_stats(self):
        stats = {
            "hh": {"new": 15, "relevant": 10, "applied": 5, "manual": 2},
        }
        result = _format_run_source_stats(stats)
        assert "hh" in result
        assert "15" in result

    def test_run_source_stats_empty(self):
        assert _format_run_source_stats({}) == "-"


class TestQueryBreakdown:

    def test_top_queries(self):
        by_query = {
            "QA": {"decisions": 50, "auto_applied": 10, "positive": 2, "rejected": 3},
            "тестировщик": {"decisions": 30, "auto_applied": 5, "positive": 1, "rejected": 1},
            "qa engineer": {"decisions": 20, "auto_applied": 3, "positive": 0, "rejected": 0},
        }
        lines = _format_top_query_breakdown(by_query, limit=2)
        assert len(lines) == 2
        # Sorted by positive desc
        assert "QA" in lines[0]

    def test_top_queries_empty(self):
        assert _format_top_query_breakdown({}) == []


class TestResumeVariantBreakdown:

    def test_variant_breakdown(self):
        by_variant = {
            "normal": {
                "applications": 30,
                "viewed": 20,
                "positive": 3,
                "rejected": 5,
                "response_rate": 66.7,
                "positive_rate": 10.0,
            },
            "fun": {
                "applications": 10,
                "viewed": 8,
                "positive": 2,
                "rejected": 1,
                "response_rate": 80.0,
                "positive_rate": 20.0,
            },
        }
        lines = _format_resume_variant_breakdown(by_variant)
        assert len(lines) == 2
        # "fun" has higher positive_rate, should come first
        assert "fun" in lines[0]

    def test_variant_breakdown_empty(self):
        assert _format_resume_variant_breakdown({}) == []
