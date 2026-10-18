import json
from types import SimpleNamespace

import pytest

import market_research as m


def page(vacancies, total):
    state = {"vacancySearchResult": {"vacancies": vacancies, "totalResults": total}}
    return SimpleNamespace(status_code=200, text='<template id="HH-Lux-InitialState">' + json.dumps(state) + '</template>')


def vacancy(i, name, comp=None, exp="between3And6", fmt="REMOTE", area="Москва", responses=100):
    return {"vacancyId": i, "name": name, "compensation": comp or {"noCompensation": {}},
            "workExperience": exp, "workFormats": [{"workFormatsElement": [fmt]}],
            "area": {"name": area}, "company": {"name": f"C{i}"}, "totalResponsesCount": responses,
            "publicationTime": {"$": "2026-10-17T10:00:00+03:00"}}


class FakeClient:
    def __init__(self, pages):
        self.pages, self.calls = pages, []

    def get(self, url, params):
        self.calls.append((url, dict(params)))
        return self.pages[params["text"]][params["page"]]


def test_collect_reads_only_search_pages_and_summarizes():
    pages = {"QA lead": [page([
        vacancy(1, "QA Lead", {"from": 200000, "to": 300000, "currencyCode": "RUR", "gross": True, "mode": "MONTH"}),
        vacancy(2, "Lead QA engineer", {"from": 250000, "currencyCode": "RUR", "gross": False, "mode": "MONTH"}, exp="moreThan6"),
        vacancy(3, "Senior QA", {"to": 999999, "currencyCode": "RUR", "gross": False, "mode": "MONTH"}),
        vacancy(4, "QA Lead", {"from": 3000, "currencyCode": "USD", "gross": False, "mode": "MONTH"}, fmt="ON_SITE"),
    ], 4)]}
    client = FakeClient(pages)
    snapshot = m.collect(client, ["QA lead"], sleep=lambda s: None)
    assert all(url == "https://hh.ru/search/vacancy" and params["search_field"] == "name" for url, params in client.calls)
    item, = m.summarize(snapshot)
    assert item["hh_total"] == 4 and item["vacancies"] == 3  # "Senior QA" is not a strict match
    assert item["with_salary"] == 2  # USD is not mixed into rouble statistics
    assert item["from"]["median"] == (174000 + 250000) // 2
    assert "удалёнка" in m.summary_text([item]) or "удалёнка:" in m.summary_text([item])
    assert b"QA Lead" in m.export_csv(snapshot)


def test_parse_queries_limits():
    assert m.parse_queries("QA lead\n\n QA lead \nHR") == ["QA lead", "HR"]
    with pytest.raises(m.MarketResearchError):
        m.parse_queries("")
    with pytest.raises(m.MarketResearchError):
        m.parse_queries("\n".join(f"q{i}" for i in range(21)))


def test_strict_match_uses_word_stems():
    assert m.strict_match("руководитель отдела тестирования", "Руководитель отдела тестирования ПО")
    assert not m.strict_match("QA lead", "Senior QA engineer")


def test_markdown_report_has_summary_and_salary_table():
    pages = {"QA lead": [page([vacancy(1, "QA Lead", {"from": 200000, "to": 300000, "currencyCode": "RUR",
                                                      "gross": False, "mode": "MONTH"})], 1)]}
    snapshot = m.collect(FakeClient(pages), ["QA lead"], sleep=lambda s: None)
    text = m.export_markdown(snapshot, m.summarize(snapshot)).decode("utf-8")
    assert text.startswith("# Рынок вакансий hh.ru")
    assert "## QA lead" in text and "| [QA Lead](https://hh.ru/vacancy/1) |" in text


def test_explicit_tax_period_source_and_title_level():
    row = m._row("QA lead", vacancy(1, "Lead QA Engineer", {"from": 1000, "currencyCode": "USD", "gross": True, "mode": "MONTH"}))
    assert (row["salary_tax"], row["salary_period"], row["salary_source"], row["level"]) == (
        "gross (до вычета налогов)", "в месяц", "hh: зарплата в вакансии", "lead")
    assert row["salary_from_net"] is None and row["salary_currency"] == "USD"
    plain = m._row("QA", vacancy(2, "QA Engineer", {"to": 100000, "currencyCode": "RUR", "mode": "MONTH"}))
    assert plain["salary_tax"] == "не указано" and plain["level"] == ""
    none = m._row("QA", vacancy(3, "Head of QA"))
    assert none["salary_source"] == "не указана" and none["level"] == "head/director"


def test_strict_match_ignores_prepositions():
    assert m.strict_match("Инженер по электроснабжению", "Инженер электроснабжения")
    assert m.strict_match("Инженер по тестированию", "Ведущий инженер по тестированию")
    assert not m.strict_match("Инженер по электроснабжению", "Инженер-конструктор")
