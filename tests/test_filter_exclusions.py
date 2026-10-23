import pytest

import config
import filters


@pytest.mark.parametrize("keyword,text,matches", [
    ("механик", "механик", True),
    ("механик", "Требуется механика", True),
    ("механик", "Помощник механику", True),
    ("механик", "электромеханик", False),
    ("механик", "электромеханика", False),
    ("оборудования", "ремонт оборудования", True),
    ("оборудования", "ремонт электрооборудования", False),
    (" МЕХАНИК ", "(МЕХАНИК)", True),
    ("механик", "слесарь-механик", True),
    ("механик", "механик-электрик", True),
    ("механик", "механик2", False),
    ("механик", "механик_tools", False),
    ("механик", "ёйлмеханик", False),
    ("HR", "HR-отдел", True),
    ("HR", "through chrome", False),
    ("HR", "HRы", False),
    ("бух", "бухгалтер", False),
    ("support", "Support engineer", True),
    ("support", "tech-support", True),
    ("support", "unsupported", False),
    ("support", "supporting", False),
    ("support", "support2", False),
    ("full-stack", "Senior full-stack developer", True),
    ("full-stack", "unfull-stack", False),
    ("full-stack", "full-stacker", False),
    ("инженер-механик", "инженера-механика", True),
    ("инженер-механик", "электроинженер-механик", False),
    ("менеджер по продажам", "менеджера по продажам", True),
    ("менеджер по продажам", "менеджер\nпо  продажам", True),
    ("менеджер по продажам", "менеджер по закупкам и продажам", False),
    ("менеджер по продажам", "продажам по менеджер", False),
    ("директор магазин", "директором магазина", True),
    ("директор магазин", "директор интернет-магазина", False),
    ("technical support", "technical support engineer", True),
    ("technical support", "technical customer support", False),
    ("technical support", "nontechnical support", False),
    ("technical support", "technical supporting", False),
    ("C++", "C++ developer", True),
    ("C++", "C++17 developer", False),
    ("", "механик", False),
    ("   ", "механик", False),
    (None, "механик", False),
])
def test_exclusions_match_words_and_contiguous_phrases(keyword, text, matches):
    assert filters._excluded(keyword, text.casefold()) is matches


@pytest.mark.parametrize("title,snippet,rejected", [
    ("Электромеханик", "Обслуживание электрооборудования", False),
    ("Электрик", "Обслуживание электрооборудования", False),
    ("Механик", "Ремонт", True),
    ("Электрик", "Помощь механику", True),
    ("Электрик", "Ремонт оборудования", True),
])
def test_electrician_direction_keeps_existing_exclusion_list(monkeypatch, title, snippet, rejected):
    monkeypatch.setattr(config, "VACANCY_FILTER_POLICY", "generic")
    monkeypatch.setattr(config, "VACANCY_RELEVANT_KEYWORDS", [])
    monkeypatch.setattr(config, "VACANCY_EXCLUDE_KEYWORDS", ["механик", "оборудования"])

    result = filters.check_vacancy({"title": title, "snippet": snippet})

    assert result == ("exclude_keywords" if rejected else None)


def test_default_qa_exclusion_stems_and_phrases_still_work(monkeypatch):
    monkeypatch.setattr(config, "VACANCY_FILTER_POLICY", "qa")
    monkeypatch.setattr(config, "VACANCY_RELEVANT_KEYWORDS", [])
    monkeypatch.setattr(config, "VACANCY_EXCLUDE_KEYWORDS", [])

    for title in ("Уборщица", "Директор магазина", "Менеджер по продажам"):
        assert filters.check_vacancy({"title": title, "snippet": "QA"}) == "exclude_keywords"
