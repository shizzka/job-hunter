"""
Fixture-based parser tests (B-002).

Тестируют парсеры и нормализацию вакансий без сети.
Фикстуры: tests/fixtures/
"""
import json
from pathlib import Path

import pytest

FIXTURES = Path(__file__).parent / "fixtures"

# ── Общая схема нормализованной вакансии ──

REQUIRED_KEYS = {
    "id", "source", "source_label", "title", "company",
    "salary", "url", "snippet", "details", "location", "apply_mode",
}


def _assert_normalized(vacancy: dict, source: str):
    """Проверяет, что вакансия содержит все обязательные поля."""
    missing = REQUIRED_KEYS - set(vacancy.keys())
    assert not missing, f"Missing keys: {missing}"
    assert vacancy["source"] == source
    assert vacancy["id"].startswith(f"{source}:")
    assert vacancy["title"]
    assert vacancy["apply_mode"] in ("auto", "manual")


# ── GeekJob ──

class TestGeekJobParser:

    @pytest.fixture()
    def client(self):
        from geekjob_client import GeekJobClient
        return GeekJobClient()

    @pytest.fixture()
    def list_html(self):
        return (FIXTURES / "geekjob_list.html").read_text()

    @pytest.fixture()
    def detail_html(self):
        return (FIXTURES / "geekjob_detail.html").read_text()

    def test_extract_total_pages(self, client, list_html):
        assert client._extract_total_pages(list_html) == 3

    def test_extract_total_pages_missing(self, client):
        assert client._extract_total_pages("<html></html>") == 1

    def test_extract_list_items(self, client, list_html):
        items = client._extract_list_items(list_html)
        assert len(items) == 2

    def test_normalize_vacancy(self, client, list_html):
        items = client._extract_list_items(list_html)
        v = client._normalize_vacancy(items[0])
        assert v is not None
        _assert_normalized(v, "geekjob")
        assert v["external_id"] == "qa-engineer-12345"
        assert "QA Engineer" in v["title"]
        assert "Рога и Копыта" in v["company"]
        assert "150 000" in v["salary"]

    def test_normalize_vacancy_second(self, client, list_html):
        items = client._extract_list_items(list_html)
        v = client._normalize_vacancy(items[1])
        assert v is not None
        _assert_normalized(v, "geekjob")
        assert v["external_id"] == "manual-tester-67890"
        assert "Тестировщик" in v["title"]

    def test_normalize_vacancy_bad_html(self, client):
        assert client._normalize_vacancy("<div>broken</div>") is None

    def test_extract_vacancy_meta(self, client, detail_html):
        meta = client._extract_vacancy_meta(detail_html)
        assert meta["id"] == 12345
        assert meta["title"] == "QA Engineer"

    def test_extract_vacancy_meta_missing(self, client):
        with pytest.raises(RuntimeError):
            client._extract_vacancy_meta("<html></html>")


# ── SuperJob ──

class TestSuperJobParser:

    @pytest.fixture()
    def client(self):
        from superjob_client import SuperJobClient
        return SuperJobClient()

    @pytest.fixture()
    def raw_vacancy(self):
        return json.loads((FIXTURES / "superjob_vacancy.json").read_text())

    def test_normalize_vacancy(self, client, raw_vacancy):
        v = client._normalize_vacancy(raw_vacancy)
        _assert_normalized(v, "superjob")
        assert v["external_id"] == "55001"
        assert "QA Engineer" in v["title"]
        assert "ТестКорп" in v["company"]
        assert "superjob.ru" in v["url"]
        assert "published_at" in v

    def test_normalize_vacancy_salary(self, client, raw_vacancy):
        v = client._normalize_vacancy(raw_vacancy)
        assert "120" in v["salary"]
        assert "200" in v["salary"]

    def test_normalize_vacancy_location(self, client, raw_vacancy):
        v = client._normalize_vacancy(raw_vacancy)
        assert "Москва" in v["location"]
        assert "Удаленная" in v["location"]

    def test_normalize_vacancy_details(self, client, raw_vacancy):
        v = client._normalize_vacancy(raw_vacancy)
        assert "автоматизация" in v["details"].lower() or "Selenium" in v["details"]

    def test_normalize_vacancy_minimal(self, client):
        v = client._normalize_vacancy({"id": 1})
        _assert_normalized(v, "superjob")
        assert v["title"] == "Без названия"
        assert v["company"] == "—"

    def test_format_salary_agreement(self):
        from superjob_client import _format_salary
        assert _format_salary({"agreement": True}) == "по договоренности"

    def test_format_salary_range(self):
        from superjob_client import _format_salary
        result = _format_salary({
            "payment_from": 100000,
            "payment_to": 200000,
            "currency": "rub",
            "agreement": False,
        })
        assert "100" in result
        assert "200" in result

    def test_clean_text(self):
        from superjob_client import _clean_text
        assert _clean_text("Hello &amp; <b>world</b>") == "Hello & world"
        assert _clean_text(None) == ""


# ── Habr Career ──

class TestHabrCareerParser:

    @pytest.fixture()
    def client(self):
        from habr_career_client import HabrCareerClient
        return HabrCareerClient()

    @pytest.fixture()
    def raw_vacancy(self):
        return json.loads((FIXTURES / "habr_vacancy.json").read_text())

    @pytest.fixture()
    def ssr_page(self):
        return (FIXTURES / "habr_ssr_page.html").read_text()

    def test_normalize_vacancy(self, client, raw_vacancy):
        v = client._normalize_vacancy(raw_vacancy)
        _assert_normalized(v, "habr")
        assert v["external_id"] == "1099001"
        assert "QA Automation" in v["title"]
        assert "Яндекс" in v["company"]
        assert "200 000" in v["salary"]

    def test_normalize_vacancy_location_remote(self, client, raw_vacancy):
        v = client._normalize_vacancy(raw_vacancy)
        assert "Удаленно" in v["location"]
        assert "Москва" in v["location"]

    def test_normalize_vacancy_skills_in_snippet(self, client, raw_vacancy):
        v = client._normalize_vacancy(raw_vacancy)
        assert "Python" in v["snippet"]
        assert "Selenium" in v["snippet"]

    def test_normalize_vacancy_minimal(self, client):
        v = client._normalize_vacancy({"id": 999})
        _assert_normalized(v, "habr")
        assert v["title"] == "Без названия"
        assert v["company"] == "—"
        assert v["salary"] == "не указана"

    def test_extract_ssr_state(self, client, ssr_page):
        state = client._extract_ssr_state(ssr_page)
        assert isinstance(state, dict)
        vacancies = state.get("vacanciesSearch", {}).get("vacancies", {})
        assert len(vacancies.get("list", [])) == 2
        assert vacancies["meta"]["totalPages"] == 5

    def test_extract_ssr_state_missing(self, client):
        with pytest.raises(RuntimeError):
            client._extract_ssr_state("<html></html>")

    def test_clean_html(self):
        from habr_career_client import _clean_html
        assert _clean_html("Hello &amp; <b>world</b>") == "Hello & world"
        assert _clean_html(None) == ""

    def test_find_value_nested(self):
        from habr_career_client import _find_value
        data = {"a": {"b": {"target": 42}}}
        assert _find_value(data, "target") == 42

    def test_find_value_in_list(self):
        from habr_career_client import _find_value
        data = [{"x": 1}, {"target": "found"}]
        assert _find_value(data, "target") == "found"

    def test_find_value_missing(self):
        from habr_career_client import _find_value
        assert _find_value({"a": 1}, "nope") is None
