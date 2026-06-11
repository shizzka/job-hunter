import asyncio
import json

import matcher


def test_cover_letter_style_block_is_stable_for_same_vacancy():
    vacancy = {
        "id": "hh-1",
        "title": "QA инженер",
        "company": "Acme",
        "snippet": "REST API, личный кабинет, регресс",
    }
    details = "Нужно тестировать API и пользовательские сценарии."

    first = matcher._build_cover_letter_style_block(vacancy, details)
    second = matcher._build_cover_letter_style_block(vacancy, details)

    assert first == second
    assert "## Вариант стиля" in first
    assert "Стратегия:" in first
    assert "НЕ начинай письмо" in first
    assert "Заметил" in first
    assert "В вашей вакансии" in first


def test_cover_letter_variant_changes_across_vacancies():
    vacancies = [
        {"id": f"hh-{i}", "title": f"QA инженер {i}", "company": f"Company {i}"}
        for i in range(12)
    ]

    indexes = {matcher._cover_letter_variant_index(vacancy, "") for vacancy in vacancies}

    assert len(indexes) > 1
    assert indexes <= set(range(len(matcher.COVER_LETTER_STYLE_VARIANTS)))


def test_cover_letter_style_variants_have_required_fields():
    for variant in matcher.COVER_LETTER_STYLE_VARIANTS:
        assert variant["name"]
        assert variant["opening"]
        assert variant["shape"]
        assert variant["ending"]

class _FakeMessage:
    def __init__(self, content: str):
        self.content = content


class _FakeChoice:
    def __init__(self, content: str):
        self.message = _FakeMessage(content)


class _FakeResponse:
    def __init__(self, content: str):
        self.choices = [_FakeChoice(content)]


class _FakeCompletions:
    def __init__(self, content: str):
        self.content = content
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        return _FakeResponse(self.content)


class _FakeChat:
    def __init__(self, completions: _FakeCompletions):
        self.completions = completions


class _FakeClient:
    def __init__(self, content: str):
        self.chat = _FakeChat(_FakeCompletions(content))


def test_evaluate_vacancy_blocks_overstated_llm_claim(monkeypatch):
    payload = json.dumps(
        {
            "score": 88,
            "reason": "Кандидат имеет более 3 лет опыта тестирования и опыт автотестов на Python.",
            "should_apply": True,
            "red_flags": [],
        },
        ensure_ascii=False,
    )
    client = _FakeClient(payload)
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    monkeypatch.setattr(matcher, "_load_resume", lambda: "Junior Manual QA, около 1 года практического тестирования.")
    monkeypatch.setattr(
        matcher.config,
        "HH_AUTO_ANSWER_PROFILE_NOTE",
        "Канон: Junior Manual QA, около 1 года, без production-автотестов.",
    )

    result = asyncio.run(
        matcher.evaluate_vacancy(
            {"id": "hh-safe", "title": "QA Engineer", "company": "Acme", "snippet": "web, API"},
            "Ручное тестирование web-продукта.",
        )
    )

    assert result["should_apply"] is False
    assert result["score"] <= 39
    assert "candidate_claim_overstatement" in result["guard_flags"]
    assert "Guard: LLM завысила профиль" in result["reason"]
    prompt = client.chat.completions.calls[0]["messages"][0]["content"]
    assert "Контрольные факты кандидата" in prompt
    assert "около 1 года" in prompt


def test_evaluate_vacancy_allows_confirmed_autotest_participation(monkeypatch):
    payload = json.dumps(
        {
            "score": 76,
            "reason": "Кандидат подходит для Manual/API QA: участвовал в разработке API-автотестов на Python/pytest, но основной профиль - ручное и API-тестирование.",
            "should_apply": True,
            "red_flags": [],
        },
        ensure_ascii=False,
    )
    client = _FakeClient(payload)
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    monkeypatch.setattr(
        matcher,
        "_load_resume",
        lambda: "Junior Manual QA, около 1 года; участвовал в разработке API-автотестов на Python/pytest.",
    )

    result = asyncio.run(
        matcher.evaluate_vacancy(
            {"id": "hh-api", "title": "QA Engineer API", "company": "Acme", "snippet": "manual, API"},
            "Manual QA, REST API, Postman. Опыт запуска API-автотестов будет плюсом.",
        )
    )

    assert result["should_apply"] is True
    assert result["score"] == 76
    assert "guard_flags" not in result


def test_overstatement_detector_allows_careful_autotest_wording():
    text = "Участвовал в разработке API-автотестов на Python/pytest: сценарии, покрытие, окружение и запуск готовых тестов."

    assert matcher._detect_candidate_claim_overstatements(text) == []


def test_evaluate_vacancy_blocks_nonjunior_automation_heavy(monkeypatch):
    payload = json.dumps(
        {
            "score": 90,
            "reason": "Вакансия выглядит подходящей по QA-задачам.",
            "should_apply": True,
            "red_flags": [],
        },
        ensure_ascii=False,
    )
    client = _FakeClient(payload)
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    monkeypatch.setattr(matcher, "_load_resume", lambda: "Junior Manual QA, около 1 года практического тестирования.")

    result = asyncio.run(
        matcher.evaluate_vacancy(
            {"id": "hh-auto", "title": "Тестировщик-автоматизатор / QA Auto", "company": "JEDai"},
            "Нужно писать автотесты на Python и Selenium, поддерживать регрессионный набор.",
        )
    )

    assert result["should_apply"] is False
    assert result["score"] <= 39
    assert "automation_heavy_mismatch" in result["guard_flags"]


def test_evaluate_vacancy_blocks_middle_below_challenge_threshold(monkeypatch):
    payload = json.dumps(
        {
            "score": 59,
            "reason": "Manual QA частично подходит по задачам.",
            "should_apply": True,
            "red_flags": [],
        },
        ensure_ascii=False,
    )
    client = _FakeClient(payload)
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    monkeypatch.setattr(matcher, "_load_resume", lambda: "Junior Manual QA, около 1 года практического тестирования.")

    result = asyncio.run(
        matcher.evaluate_vacancy(
            {"id": "hh-middle", "title": "Middle QA Engineer", "company": "Acme", "snippet": "Опыт от 2 лет"},
            "Middle QA, ручное и API тестирование, опыт от 2 лет.",
        )
    )

    assert result["should_apply"] is False
    assert result["score"] <= 59
    assert "middle_challenge_below_threshold" in result["guard_flags"]


def test_evaluate_vacancy_allows_optional_automation_as_plus(monkeypatch):
    payload = json.dumps(
        {
            "score": 61,
            "reason": "Manual/API QA подходит, автотесты указаны только как плюс.",
            "should_apply": True,
            "red_flags": [],
        },
        ensure_ascii=False,
    )
    client = _FakeClient(payload)
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    monkeypatch.setattr(matcher, "_load_resume", lambda: "Junior Manual QA, около 1 года практического тестирования.")

    result = asyncio.run(
        matcher.evaluate_vacancy(
            {"id": "hh-manual-plus", "title": "Тестировщик ПО", "company": "Acme", "snippet": "Manual, API, SQL"},
            "Ручное тестирование веб-продукта, REST API, SQL. Опыт запуска API-автотестов будет плюсом, автоматизация не обязательна.",
        )
    )

    assert result["should_apply"] is True
    assert result["score"] == 61
    assert "automation_heavy_mismatch" not in result.get("guard_flags", [])


def test_evaluate_vacancy_allows_strong_middle_manual_challenge(monkeypatch):
    payload = json.dumps(
        {
            "score": 84,
            "reason": "Сильное совпадение по manual/API QA, без требований к самостоятельной автоматизации.",
            "should_apply": True,
            "red_flags": [],
        },
        ensure_ascii=False,
    )
    client = _FakeClient(payload)
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    monkeypatch.setattr(matcher, "_load_resume", lambda: "Junior Manual QA, около 1 года практического тестирования.")

    result = asyncio.run(
        matcher.evaluate_vacancy(
            {"id": "hh-middle-strong", "title": "Middle Manual QA Engineer", "company": "Acme", "snippet": "Опыт от 2 лет, API, SQL"},
            "Ручное тестирование веб-продукта, REST API, SQL, тестовая документация. Автоматизация не обязательна.",
        )
    )

    assert result["should_apply"] is True
    assert result["score"] == 84
    assert "middle_challenge_allowed" in result["guard_flags"]
    prompt = client.chat.completions.calls[0]["messages"][0]["content"]
    assert "challenge-вакансия" in prompt
    assert "не придумывай Middle QA-стаж" in prompt


def test_evaluate_vacancy_applies_min_score_threshold(monkeypatch):
    payload = json.dumps(
        {
            "score": 57,
            "reason": "Есть часть совпадений по manual QA.",
            "should_apply": True,
            "red_flags": [],
        },
        ensure_ascii=False,
    )
    client = _FakeClient(payload)
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    monkeypatch.setattr(matcher, "_load_resume", lambda: "Junior Manual QA, около 1 года практического тестирования.")

    result = asyncio.run(
        matcher.evaluate_vacancy(
            {"id": "hh-threshold", "title": "QA Engineer API", "company": "Acme", "snippet": "API, SQL"},
            "API testing, SQL, junior friendly.",
        )
    )

    assert result["should_apply"] is False
    assert "below_auto_apply_threshold" in result["guard_flags"]


def test_evaluate_vacancy_softens_salary_red_flag(monkeypatch):
    payload = json.dumps(
        {
            "score": 80,
            "reason": "Хорошее совпадение по manual/API QA.",
            "should_apply": True,
            "red_flags": ["не указана зарплата"],
        },
        ensure_ascii=False,
    )
    client = _FakeClient(payload)
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    monkeypatch.setattr(matcher, "_load_resume", lambda: "Junior Manual QA, около 1 года практического тестирования.")

    result = asyncio.run(
        matcher.evaluate_vacancy(
            {"id": "hh-salary", "title": "Junior QA Engineer", "company": "Acme", "snippet": "API, SQL"},
            "Junior QA, API, SQL.",
        )
    )

    assert result["should_apply"] is True
    assert result["score"] == 75
    assert result["red_flags"] == []
    assert result["soft_flags"] == ["не указана зарплата"]
    assert "salary_red_flag_softened" in result["guard_flags"]


def test_generate_cover_letter_falls_back_on_overstated_claim(monkeypatch):
    client = _FakeClient("Я QA с более 3 лет опыта и автотестами на Python.")
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    monkeypatch.setattr(matcher, "_load_resume", lambda: "Junior Manual QA, около 1 года практического тестирования.")

    cover = asyncio.run(
        matcher.generate_cover_letter(
            {"id": "hh-cover", "title": "QA Engineer", "company": "Acme"},
            "Ручное тестирование web-продукта.",
        )
    )

    assert "Junior Manual QA" in cover
    assert "около 1 года" in cover
    assert "более 3" not in cover
    assert "автотестами на Python" not in cover

