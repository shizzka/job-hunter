import pytest

import config
import filters

DIRECTION = {"goal": ["Контролер данных", "Оператор обработки данных", "Координатор контроля качества данных"],
             "allowed_wording": ["специалист по работе с данными"]}


@pytest.fixture
def direction(monkeypatch):
    monkeypatch.setattr(config, "VACANCY_FILTER_POLICY", "generic")
    monkeypatch.setattr(config, "ACTIVE_DIRECTION", DIRECTION, raising=False)
    monkeypatch.setattr(config, "VACANCY_RELEVANT_KEYWORDS", ["контроль качества данных", "сверка данных"])
    monkeypatch.setattr(config, "VACANCY_EXCLUDE_KEYWORDS", ["разработка", "HR"])


@pytest.mark.parametrize("title,snippet,passes", [
    ("Оператор базы данных", "", True),                       # target role by meaningful words
    ("Специалист отдела контроля качества", "", True),          # «контроля» ~ «контроль»
    ("Ассистент", "Контролем качества данных и сверкой данных", True),  # phrase in another word form
    ("Менеджер по контрактному обеспечению", "", False),       # «контракт» is not «контроль»
    ("Ведущий специалист по закупкам", "", False),             # generic role words only
    ("Разработчик на PHP", "", False),
    ("Аналитик", "Through chrome", False),                     # «HR» is a whole word, not inside «through»
])
def test_direction_filter(direction, title, snippet, passes):
    assert (filters.check_vacancy({"title": title, "snippet": snippet}) is None) is passes


def test_short_exclude_is_a_whole_word(direction):
    assert filters.check_vacancy({"title": "Контролер данных", "snippet": "Работа с HR-отделом"}) == "exclude_keywords"
    assert filters.check_vacancy({"title": "Контролер данных", "snippet": "through the process"}) is None
