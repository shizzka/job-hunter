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


def test_cover_letter_style_block_includes_managed_cover_style():
    block = matcher._build_cover_letter_style_block(
        {"title": "QA Engineer API", "snippet": "REST API, Postman, Swagger"},
        "Нужно тестировать REST API.",
    )

    assert "## Управляемый стиль сопроводительного" in block
    assert "cover_style: api_qa" in block
    assert "REST API" in block
    assert "production automation" in block


def test_analyze_cover_letter_returns_hash_features_and_guard_flags():
    cover = "Проверяю REST API в Postman, руками гоняю сценарии и оформляю баг-репорты."

    meta = matcher.analyze_cover_letter(
        cover,
        cover_style="api_qa",
        fallback=True,
        overclaim_guard=True,
    )

    assert meta["cover_style"] == "api_qa"
    assert len(meta["cover_letter_hash"]) == 16
    assert meta["cover_letter_length"] == len(cover)
    assert meta["cover_letter_features"]["mentions_api"] is True
    assert meta["cover_letter_features"]["mentions_manual"] is True
    assert meta["fallback_cover_letter"] is True
    assert meta["overclaim_guard"] is True


def test_cover_letter_positioning_block_for_middle_one_year():
    block = matcher._build_cover_letter_positioning_block(
        {"title": "Middle Manual QA Engineer", "snippet": "Опыт от 1 года, REST API, SQL"},
        "Ручное и API тестирование, опыт от 1 года.",
    )

    assert "junior+ challenge" in block
    assert "не называй себя middle" in block
    assert "REST API" in block


def test_cover_letter_positioning_block_for_middle_challenge():
    block = matcher._build_cover_letter_positioning_block(
        {"title": "Middle QA Engineer", "snippet": "Опыт от 2 лет, API"},
        "Manual QA, API, опыт от 2 лет.",
    )

    assert "Middle/challenge" in block
    assert "не завышай стаж" in block


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
    def __init__(self, content: str | list[str]):
        self.contents = list(content) if isinstance(content, list) else [content]
        self.calls = []

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        idx = min(len(self.calls) - 1, len(self.contents) - 1)
        return _FakeResponse(self.contents[idx])


class _FakeChat:
    def __init__(self, completions: _FakeCompletions):
        self.completions = completions


class _FakeClient:
    def __init__(self, content: str | list[str]):
        self.chat = _FakeChat(_FakeCompletions(content))


def test_evaluate_vacancy_repairs_malformed_json(monkeypatch):
    repaired = json.dumps(
        {
            "score": 82,
            "reason": "Manual/API QA хорошо совпадает с вакансией.",
            "should_apply": True,
            "red_flags": [],
        },
        ensure_ascii=False,
    )
    client = _FakeClient(["{\n  \"score\": ,\n  \"reason\": \"сломано\"", repaired])
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    monkeypatch.setattr(matcher, "_load_resume", lambda: "Junior Manual QA, около 1 года практического тестирования.")

    result = asyncio.run(
        matcher.evaluate_vacancy(
            {"id": "hh-json", "title": "QA Engineer", "company": "Acme", "snippet": "Manual, API"},
            "Manual QA, API, Postman.",
        )
    )

    assert result["score"] == 82
    assert result["should_apply"] is True
    assert len(client.chat.completions.calls) == 2
    repair_prompt = client.chat.completions.calls[1]["messages"][1]["content"]
    assert "Преобразуй ответ модели в валидный JSON" in repair_prompt


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


def test_evaluate_vacancy_treats_middle_one_year_as_junior_plus_challenge(monkeypatch):
    payload = json.dumps(
        {
            "score": 58,
            "reason": "Manual/API QA подходит, требования опыта около года.",
            "should_apply": True,
            "red_flags": [],
        },
        ensure_ascii=False,
    )
    client = _FakeClient(payload)
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    monkeypatch.setattr(matcher, "_load_resume", lambda: "Junior Manual QA, около 1 года практического тестирования.")
    monkeypatch.setattr(matcher.config, "HH_MATCHER_AUTO_APPLY_MIN_SCORE", 58, raising=False)
    monkeypatch.setattr(matcher.config, "HH_MATCHER_MIDDLE_CHALLENGE_MIN_SCORE", 60, raising=False)

    result = asyncio.run(
        matcher.evaluate_vacancy(
            {"id": "hh-middle-one-year", "title": "Middle Manual QA Engineer", "company": "Acme", "snippet": "Опыт от 1 года, API, SQL"},
            "Middle Manual QA, ручное и API тестирование, опыт от 1 года.",
        )
    )

    assert result["should_apply"] is True
    assert result["score"] == 58
    assert "middle_one_year_challenge" in result["guard_flags"]
    assert "middle_challenge_below_threshold" not in result["guard_flags"]
    prompt = client.chat.completions.calls[0]["messages"][0]["content"]
    assert "junior+ challenge" in prompt
    assert "не обычный Middle-фильтр" in prompt


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


def test_manual_review_candidate_allows_yellow_zone(monkeypatch):
    monkeypatch.setattr(matcher.config, "HH_MATCHER_MANUAL_REVIEW_ENABLED", True, raising=False)
    monkeypatch.setattr(matcher.config, "HH_MATCHER_MANUAL_REVIEW_MIN_SCORE", 50, raising=False)
    monkeypatch.setattr(matcher.config, "HH_MATCHER_MANUAL_REVIEW_MAX_SCORE", 0, raising=False)
    monkeypatch.setattr(matcher.config, "HH_MATCHER_AUTO_APPLY_MIN_SCORE", 58, raising=False)
    monkeypatch.setattr(matcher.config, "HH_MATCHER_MIDDLE_CHALLENGE_MIN_SCORE", 60, raising=False)

    assert matcher.is_manual_review_candidate(
        {
            "score": 57,
            "should_apply": False,
            "red_flags": [],
            "guard_flags": ["below_auto_apply_threshold"],
        }
    ) is True
    assert matcher.is_manual_review_candidate(
        {
            "score": 59,
            "should_apply": False,
            "red_flags": [],
            "guard_flags": ["middle_challenge_below_threshold"],
        }
    ) is True


def test_manual_review_candidate_blocks_fatal_guards(monkeypatch):
    monkeypatch.setattr(matcher.config, "HH_MATCHER_MANUAL_REVIEW_ENABLED", True, raising=False)
    monkeypatch.setattr(matcher.config, "HH_MATCHER_MANUAL_REVIEW_MIN_SCORE", 50, raising=False)
    monkeypatch.setattr(matcher.config, "HH_MATCHER_MANUAL_REVIEW_MAX_SCORE", 0, raising=False)

    assert matcher.is_manual_review_candidate(
        {
            "score": 57,
            "should_apply": False,
            "red_flags": [],
            "guard_flags": ["automation_heavy_mismatch"],
        }
    ) is False
    assert matcher.is_manual_review_candidate(
        {
            "score": 57,
            "should_apply": False,
            "red_flags": ["closed_or_archived"],
            "guard_flags": [],
        }
    ) is False


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


class _FailingCompletions:
    async def create(self, **kwargs):
        raise TimeoutError("cover timeout")


class _FailingClient:
    def __init__(self):
        self.chat = _FakeChat(_FailingCompletions())


def test_generate_cover_letter_falls_back_on_empty_response(monkeypatch):
    client = _FakeClient("")
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    monkeypatch.setattr(matcher, "_load_resume", lambda: "Junior Manual QA, около 1 года практического тестирования.")

    cover = asyncio.run(
        matcher.generate_cover_letter(
            {"id": "hh-empty-cover", "title": "QA Engineer", "company": "Acme"},
            "Ручное тестирование web-продукта.",
        )
    )

    assert "Junior Manual QA" in cover
    assert "около 1 года" in cover
    meta = matcher.analyze_cover_letter(cover)
    assert meta["fallback_cover_letter"] is True
    assert meta["cover_letter_hash"]


def test_generate_cover_letter_falls_back_on_client_error(monkeypatch):
    monkeypatch.setattr(matcher, "_get_client", lambda: _FailingClient())
    monkeypatch.setattr(matcher, "_load_resume", lambda: "Junior Manual QA, около 1 года практического тестирования.")

    cover = asyncio.run(
        matcher.generate_cover_letter(
            {"id": "hh-error-cover", "title": "QA Engineer", "company": "Acme"},
            "Ручное тестирование web-продукта.",
        )
    )

    assert "Junior Manual QA" in cover
    assert "около 1 года" in cover


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
    meta = matcher.analyze_cover_letter(cover)
    assert meta["fallback_cover_letter"] is True
    assert meta["overclaim_guard"] is True



def test_classify_vacancy_cluster_regressions():
    assert matcher.classify_vacancy_cluster(
        {"title": "Manual QA Engineer", "snippet": "web, UI, regression, checklists"},
        "ручное тестирование web-продукта",
    ) == "manual_web_qa"
    assert matcher.classify_vacancy_cluster(
        {"title": "QA Engineer API", "snippet": "REST API, Postman, Swagger, SQL"},
        "",
    ) == "api_qa"
    assert matcher.classify_vacancy_cluster(
        {"title": "Mobile QA", "snippet": "Android, iOS, Charles, push"},
        "",
    ) == "mobile_qa"
    assert matcher.classify_vacancy_cluster(
        {"title": "Java developer", "snippet": "Spring, backend"},
        "",
    ) == "reject_non_qa"
    assert matcher.classify_vacancy_cluster(
        {"title": "Сервисный инженер", "snippet": "ремонт оборудования"},
        "",
    ) == "reject_non_qa"
    assert matcher.classify_vacancy_cluster(
        {"title": "Support engineer", "snippet": "API, логи, баги, диагностика, тестирование"},
        "",
    ) == "qa_support_adjacent"


def test_classify_vacancy_cluster_rejects_misleading_non_qa_roles():
    assert matcher.classify_vacancy_cluster(
        {
            "title": "Сервисный инженер",
            "snippet": "Тестирование оборудования после ремонта, контроль качества, выезды к клиентам",
        },
        "Диагностика плат, пайка, ремонт и проверка работоспособности устройств.",
    ) == "reject_non_qa"
    assert matcher.classify_vacancy_cluster(
        {
            "title": "Java Developer",
            "snippet": "Spring Boot, REST API, unit tests, интеграционное тестирование кода",
        },
        "Разработка backend-сервисов и покрытие кода тестами.",
    ) == "reject_non_qa"
    assert matcher.classify_vacancy_cluster(
        {
            "title": "Специалист технической поддержки",
            "snippet": "Консультации пользователей, чат, звонки, CRM, SLA",
        },
        "Первая линия поддержки и ответы по шаблонам.",
    ) == "reject_non_qa"


def test_classify_vacancy_cluster_allows_qa_adjacent_and_junior_plus_cases():
    assert matcher.classify_vacancy_cluster(
        {"title": "QA Support Engineer", "snippet": "API, логи, баги, диагностика, Postman"},
        "Разбор дефектов, проверка REST API и оформление баг-репортов.",
    ) == "qa_support_adjacent"
    assert matcher.classify_vacancy_cluster(
        {"title": "Специалист технической поддержки", "snippet": "Логи, баги, REST API, DevTools"},
        "Работать с логами и багами системы, разбирать REST API и передавать дефекты в разработку.",
    ) == "qa_support_adjacent"
    assert matcher.classify_vacancy_cluster(
        {"title": "Middle Manual QA Engineer", "snippet": "Опыт от 1 года, API, SQL"},
        "Ручное тестирование web-продукта, REST API, тест-кейсы, без самостоятельной автоматизации.",
    ) == "api_qa"
    assert matcher.classify_vacancy_cluster(
        {"title": "Junior AQA Python", "snippet": "Python, pytest, обучение, manual/API база"},
        "Junior/trainee формат, развитие в API-автотестах под наставником.",
    ) == "junior_aqa_python"


def test_classify_vacancy_cluster_rejects_pure_support_noise():
    assert matcher.classify_vacancy_cluster(
        {"title": "Product Marketing Manager", "company": "QA.Guru", "snippet": "Запуск и оптимизация рекламных кампаний"},
        "Маркетинг, лидогенерация, рекламные площадки и публичные выступления.",
    ) == "reject_non_qa"
    assert matcher.classify_vacancy_cluster(
        {"title": "Специалист по внедрению и настройке Битрикс24", "snippet": "Инструкция или чек-лист для пользователей"},
        "Настроить Битрикс24 под цикл обработки клиентских запросов.",
    ) == "reject_non_qa"
    assert matcher.classify_vacancy_cluster(
        {"title": "Оператор службы поддержки", "snippet": "Звонки, консультация по скрипту, помощь клиентам"},
        "Принимать входящие звонки и отвечать по базе знаний.",
    ) == "reject_non_qa"
    assert matcher.classify_vacancy_cluster(
        {"title": "Специалист по поддержке информационных систем (SQL)", "snippet": "SQL-запросы для внесения данных в БД"},
        "Сопровождение системы и изменение данных по заявкам пользователей.",
    ) == "reject_non_qa"
    assert matcher.classify_vacancy_cluster(
        {"title": "Hospitality Software IT Specialist", "snippet": "SQL, software installation, remote support"},
        "Provide telephonic and onsite support, install software, business travel.",
    ) == "reject_non_qa"


def test_response_probability_score_uses_freshness_and_applicants():
    fresh = matcher.estimate_response_probability_score(
        {
            "title": "Junior QA Engineer",
            "snippet": "без опыта, обучение, API",
            "salary": "100 000 ₽",
            "number_of_applicants": 5,
            "published_at": "2026-07-09T10:00:00",
        },
        match_score=70,
        cluster="api_qa",
    )
    crowded = matcher.estimate_response_probability_score(
        {
            "title": "QA Engineer",
            "snippet": "manual web testing",
            "salary": "не указана",
            "number_of_applicants": 500,
            "published_at": "2026-05-01T10:00:00",
        },
        match_score=70,
        cluster="manual_web_qa",
    )

    assert fresh > crowded
    assert fresh >= 80
    assert crowded < 35


def test_evaluate_vacancy_blocks_non_qa_cluster_even_if_llm_allows(monkeypatch):
    payload = json.dumps(
        {
            "score": 91,
            "reason": "LLM ошибочно решила, что тестирование оборудования подходит под QA.",
            "should_apply": True,
            "red_flags": [],
        },
        ensure_ascii=False,
    )
    client = _FakeClient(payload)
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    monkeypatch.setattr(matcher, "_load_resume", lambda: "Junior Manual QA, около 1 года практического тестирования.")
    monkeypatch.setattr(matcher.config, "HH_MATCHER_MANUAL_REVIEW_ENABLED", True, raising=False)
    monkeypatch.setattr(matcher.config, "HH_MATCHER_MANUAL_REVIEW_MIN_SCORE", 0, raising=False)
    monkeypatch.setattr(matcher.config, "HH_MATCHER_MANUAL_REVIEW_MAX_SCORE", 100, raising=False)

    result = asyncio.run(
        matcher.evaluate_vacancy(
            {
                "id": "hh-service",
                "title": "Сервисный инженер",
                "company": "RepairCo",
                "snippet": "Тестирование оборудования после ремонта, контроль качества, выезды к клиентам",
            },
            "Диагностика плат, пайка, ремонт и проверка работоспособности устройств.",
        )
    )

    assert result["should_apply"] is False
    assert result["score"] <= 39
    assert result["cluster"] == "reject_non_qa"
    assert "cluster_reject_non_qa" in result["guard_flags"]
    assert "cluster_reject_non_qa" in result["hard_flags"]
    assert matcher.is_manual_review_candidate(result) is False


def test_evaluate_vacancy_blocks_low_response_probability(monkeypatch):
    payload = json.dumps(
        {
            "score": 84,
            "reason": "Manual QA хорошо подходит по задачам.",
            "should_apply": True,
            "red_flags": [],
        },
        ensure_ascii=False,
    )
    client = _FakeClient(payload)
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    monkeypatch.setattr(matcher, "_load_resume", lambda: "Junior Manual QA, около 1 года практического тестирования.")
    monkeypatch.setattr(matcher.config, "HH_MATCHER_AUTO_APPLY_MIN_RESPONSE_SCORE", 45, raising=False)

    result = asyncio.run(
        matcher.evaluate_vacancy(
            {
                "id": "hh-crowded",
                "title": "QA Engineer",
                "company": "Acme",
                "snippet": "Manual web testing",
                "salary": "не указана",
                "number_of_applicants": 500,
                "published_at": "2026-05-01T10:00:00",
            },
            "Ручное тестирование web-продукта.",
        )
    )

    assert result["should_apply"] is False
    assert result["cluster"] == "manual_web_qa"
    assert result["response_probability_score"] < 45
    assert "below_response_probability_threshold" in result["guard_flags"]
