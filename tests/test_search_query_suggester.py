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


def test_pasted_numbered_markdown_and_emoji_list():
    assert normalize_queries('1. **Тестировщик ПО**\n2) «Manual QA»\n• QA API\n✅ QA API\n```\n🔎 [QA Engineer](https://example.org)\n```') == ['Тестировщик ПО', 'Manual QA', 'QA API', 'QA Engineer']


def test_meaningful_technical_symbols_and_numbers_survive():
    values = ['C++ Developer', 'C# QA', '.NET QA', '1C тестировщик', '3D QA', 'Junior+ QA', 'QA/QC', 'A/B testing', 'Node.js', 'QA (API)', 'L2 Support']
    assert normalize_queries(values) == values


def test_unicode_whitespace_invisible_chars_and_deduplication():
    assert normalize_queries(['1. QA\u00a0Engineer', 'qa engineer', 'QA\u200b API', '２）Manual QA']) == ['QA Engineer', 'QA API', 'Manual QA']


def test_decoration_only_does_not_become_empty_search():
    from query_normalization import clean_query_list
    assert clean_query_list(['✅', '•', '---', '1.']) == []
    with pytest.raises(SearchQueryValidationError):
        normalize_queries('✅\n•')


def test_existing_runtime_lists_keep_more_than_ui_limit():
    from query_normalization import clean_query_list
    assert len(clean_query_list([f'{i+1}. Role {i}' for i in range(20)])) == 20


def test_nested_formatting_and_number_symbol():
    assert normalize_queries(['🔥 **1. QA Engineer**', '№ 2: Manual QA', '`3) C++ QA`']) == ['QA Engineer', 'Manual QA', 'C++ QA']


def test_env_separator_in_list_item_is_rejected():
    with pytest.raises(SearchQueryValidationError):
        queries_to_env_value(['QA||developer'])


def test_decimal_technology_version_is_not_numbered_list():
    assert normalize_queries(['1.2 Developer']) == ['1.2 Developer']
