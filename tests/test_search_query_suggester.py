import pytest

from search_query_suggester import (
    SearchQueryValidationError,
    normalize_queries,
    queries_to_env_value,
)


def test_normalize_queries_accepts_telegram_lines_and_removes_duplicates():
    assert normalize_queries("QA Engineer\n manual qa \nQA engineer\n") == [
        "QA Engineer",
        "manual qa",
    ]


def test_normalize_queries_rejects_empty_and_excessive_lists():
    with pytest.raises(SearchQueryValidationError, match="хотя бы один"):
        normalize_queries(" \n ")
    with pytest.raises(SearchQueryValidationError, match="не больше 12"):
        normalize_queries([f"role {index}" for index in range(13)])


def test_queries_to_env_value_uses_profile_separator():
    assert queries_to_env_value(["QA engineer", "тестировщик ПО"]) == "QA engineer||тестировщик ПО"
