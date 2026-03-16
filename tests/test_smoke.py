"""
Smoke-тесты: импорт модулей, синтаксис run.sh, базовые хелперы.
B-001 из бэклога.
"""
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent

MODULES = [
    "agent",
    "apply_orchestrator",
    "invitation_sync",
    "outcome",
    "search_pipeline",
    "hh_client",
    "superjob_client",
    "habr_career_client",
    "geekjob_client",
    "matcher",
    "filters",
    "seen",
    "analytics",
    "reporting",
    "notifier",
    "office_bridge",
    "hh_resume_pipeline",
    "config",
]


@pytest.mark.parametrize("module", MODULES)
def test_import(module):
    """Все модули импортируются без ошибок."""
    __import__(module)


def test_run_sh_syntax():
    """run.sh проходит bash -n (синтаксическая проверка)."""
    result = subprocess.run(
        ["bash", "-n", str(PROJECT_ROOT / "run.sh")],
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, f"run.sh syntax error: {result.stderr.decode()}"


class TestSearchPipeline:
    """Smoke для search_pipeline.py."""

    def test_vacancy_dedupe_key(self):
        from search_pipeline import vacancy_dedupe_key

        v = {"title": "QA Engineer", "company": "Acme", "location": "Москва", "url": "https://hh.ru/vacancy/123"}
        key = vacancy_dedupe_key(v)
        assert "qa engineer" in key
        assert "acme" in key

    def test_vacancy_dedupe_key_empty(self):
        from search_pipeline import vacancy_dedupe_key

        assert vacancy_dedupe_key({}) == ""

    def test_deduplicate(self):
        from search_pipeline import deduplicate

        vacancies = [
            {"id": "1", "title": "QA", "company": "Acme", "location": "", "source": "habr", "url": ""},
            {"id": "2", "title": "QA", "company": "Acme", "location": "", "source": "hh", "url": ""},
        ]
        result = deduplicate(vacancies)
        assert len(result) == 1
        assert result[0]["source"] == "hh"  # hh предпочитается

    def test_deduplicate_unique(self):
        from search_pipeline import deduplicate

        vacancies = [
            {"id": "1", "title": "QA", "company": "A", "location": "", "url": ""},
            {"id": "2", "title": "Dev", "company": "B", "location": "", "url": ""},
        ]
        assert len(deduplicate(vacancies)) == 2


class TestApplyOrchestrator:
    """Smoke для apply_orchestrator.py."""

    def test_is_auto_apply_enabled_hh(self):
        from apply_orchestrator import is_auto_apply_enabled
        assert is_auto_apply_enabled("hh") is True

    def test_is_auto_apply_enabled_unknown(self):
        from apply_orchestrator import is_auto_apply_enabled
        assert is_auto_apply_enabled("unknown") is False

    def test_cover_letter_limit(self):
        from apply_orchestrator import get_cover_letter_limit
        assert get_cover_letter_limit("habr") == 1500
        assert get_cover_letter_limit("hh") == 1900


class TestFilters:
    """Smoke для filters.py."""

    def test_check_vacancy_relevant(self):
        from filters import check_vacancy

        v = {"title": "QA инженер", "company": "Test Co", "source": "hh"}
        result = check_vacancy(v)
        # Если None — прошёл фильтр
        # Если строка — причина отклонения
        assert result is None or isinstance(result, str)

    def test_check_vacancy_irrelevant(self):
        from filters import check_vacancy

        v = {"title": "Повар-кондитер", "company": "Ресторан", "source": "hh"}
        result = check_vacancy(v)
        assert result is not None  # Должен быть отклонён

    def test_check_vacancy_military_title(self):
        from filters import check_vacancy

        v = {"title": "Тестировщик ПО", "company": "Оборонный завод", "snippet": "работа в сфере ВПК", "source": "hh"}
        assert check_vacancy(v) == "military_redflag"

    def test_check_vacancy_military_svo(self):
        from filters import check_vacancy

        v = {"title": "QA Engineer", "snippet": "участие в СВО приветствуется", "source": "hh"}
        assert check_vacancy(v) == "military_redflag"

    def test_check_vacancy_military_contract(self):
        from filters import check_vacancy

        v = {"title": "Служба по контракту", "snippet": "военнослужащий", "source": "hh"}
        assert check_vacancy(v) == "military_redflag"

    def test_check_vacancy_not_military(self):
        from filters import check_vacancy

        v = {"title": "QA автоматизатор", "company": "Яндекс", "snippet": "автотесты python", "source": "hh"}
        assert check_vacancy(v) is None


class TestReporting:
    """Smoke для reporting.py."""

    def test_source_label(self):
        from reporting import source_label
        assert source_label("hh") != ""

    def test_format_compact_source_counts(self):
        from reporting import format_compact_source_counts
        result = format_compact_source_counts({"hh": 5, "habr": 3})
        assert isinstance(result, str)
        assert "5" in result
