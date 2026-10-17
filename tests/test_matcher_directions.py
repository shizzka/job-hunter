"""Direction routing with synthetic evidence and captured clients; no services."""
import asyncio
import hashlib
import json
from pathlib import Path

import pytest

import config
import matcher
import prompt_blocks
from hh import forms
from tests.test_cover_grounding import approved, client_for


CLAIM = "Монтирую кабельные линии и собираю электрощиты."
RESUME = f"Электрик. 5 лет опыта. {CLAIM}"
LIMITS = "[DO_NOT_CLAIM] Не заявлять IV группу электробезопасности.\n[LIMIT] Без ночных смен.\n"
ELECTRICIAN = {"id": "synthetic-electrician", "title": "Электромонтажник", "company": "Пример",
               "snippet": "Монтаж кабеля и щитов, от 2 лет опыта", "salary": "100000"}
QA_CASES = [
    {"title": "QA Engineer", "snippet": "Ручное тестирование"},
    {"title": "Junior QA Engineer", "snippet": "Опыт от 1 года, API"},
    {"title": "Middle QA Engineer", "snippet": "Опыт от 2 лет, API"},
    {"title": "Middle Manual QA", "snippet": "Опыт 1-3 года, SQL"},
    {"title": "Senior QA Engineer", "snippet": "REST API"},
    {"title": "Junior AQA Python", "snippet": "Автотесты, pytest"},
    {"title": "Mobile QA", "snippet": "Android, эмуляторы"},
    {"title": "QA банка", "snippet": "Банковский процесс, регресс, Jira"},
]
SNAPSHOT = Path(__file__).parent / "fixtures" / "dir1_qa_prompt_sha256.json"


@pytest.fixture
def evidence(tmp_path, monkeypatch):
    home = tmp_path / "electrician"
    knowledge = home / "knowledge"
    knowledge.mkdir(parents=True)
    (home / "resume.md").write_text(RESUME, encoding="utf-8")
    (knowledge / "candidate_facts.md").write_text(f"- [FACT] {CLAIM}\n{LIMITS}", encoding="utf-8")
    monkeypatch.setattr(config, "RESUME_FILE", str(home / "resume.md"))
    monkeypatch.setattr(config, "HH_AUTO_ANSWER_PROFILE_NOTE", "")
    monkeypatch.setenv("KB_RETRIEVAL_ENABLED", "1")
    monkeypatch.setattr(matcher.resume_versions, "record_input", lambda *args: None)
    monkeypatch.setattr(prompt_blocks, "build_profile_note_block", lambda: "")
    monkeypatch.setattr(prompt_blocks, "build_facts_block", lambda: "")
    monkeypatch.setattr(prompt_blocks, "build_cover_facts_blocks", lambda: ("", ""))
    return home


def scoring_prompt(monkeypatch, vacancy):
    client = client_for(json.dumps({"score": 95, "should_apply": True, "reason": "Опыт подходит", "red_flags": []}))
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    result = asyncio.run(matcher.evaluate_vacancy(vacancy))
    return client.calls[0]["messages"][0]["content"], result


def cover_prompt(monkeypatch, vacancy, claim=CLAIM):
    client = client_for(claim + " Обсудим детали?", json.dumps(approved(claim)))
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    result = asyncio.run(matcher.generate_cover_letter(vacancy))
    return client.calls[0]["messages"][0]["content"], result, client


def qa_fingerprints(monkeypatch):
    fingerprints = {}
    for vacancy in QA_CASES:
        key = vacancy["title"]
        evaluation, _ = scoring_prompt(monkeypatch, vacancy)
        cover, _, _ = cover_prompt(monkeypatch, vacancy)
        blocks = {"evaluation": evaluation, "cover": cover,
                  "style": matcher._build_cover_letter_style_block(vacancy),
                  "positioning": matcher._build_cover_letter_positioning_block(vacancy)}
        fingerprints[key] = {name: hashlib.sha256(value.encode("utf-8")).hexdigest()
                             for name, value in blocks.items()}
    return fingerprints


def test_qa_prompts_and_styles_match_head_2720977(evidence, monkeypatch):
    monkeypatch.setattr(config, "VACANCY_FILTER_POLICY", "qa")
    actual = qa_fingerprints(monkeypatch)
    assert actual == json.loads(SNAPSHOT.read_text(encoding="utf-8"))


@pytest.mark.parametrize("title,snippet", [
    ("Электромонтажник", "Монтаж кабеля, от 2 лет опыта"),
    ("Junior электрик", "Обучение, без опыта"),
    ("Middle электрик", "Опыт 1-3 года"),
    ("Ведущий электрик", "10 лет опыта"),
])
def test_generic_scoring_has_neutral_rubric_and_all_restrictions(evidence, monkeypatch, title, snippet):
    monkeypatch.setattr(config, "VACANCY_FILTER_POLICY", "generic")
    prompt, result = scoring_prompt(monkeypatch, dict(ELECTRICIAN, title=title, snippet=snippet))
    assert "Нейтральная рубрика" in prompt
    for text in ("оборудование", "группы электробезопасности", "уровень", "график", "[DO_NOT_CLAIM]", "[LIMIT]"):
        assert text.casefold() in prompt.casefold()
    assert all(line in prompt for line in LIMITS.splitlines())
    for text in ("qa", "sdet", "aqa", "junior+ challenge", "postman", "technical qa"):
        assert text not in prompt.casefold()
    assert result["score"] == 95
    assert result["should_apply"] is True
    assert result["cover_style"] == result["cluster"] == "generic"
    assert not result.get("guard_flags")


def test_generic_cover_is_business_style_and_still_verified(evidence, monkeypatch):
    monkeypatch.setattr(config, "VACANCY_FILTER_POLICY", "generic")
    prompt, cover, client = cover_prompt(monkeypatch, ELECTRICIAN)
    assert cover == CLAIM + " Обсудим детали?"
    assert len(client.calls) == 2  # Author and real grounding with a synthetic response.
    assert "cover_style: generic" in prompt
    assert "до 1200 символов" in prompt and "максимум 1500 символов" in prompt
    assert "деловой" in prompt
    assert "один-два релевантных факта" in prompt
    assert all(line in prompt for line in LIMITS.splitlines())
    for text in ("qa", "postman", "sdet", "aqa", "поковырял", "тест-кейс", "rest api"):
        assert text not in prompt.casefold()
    assert matcher.analyze_cover_letter(cover)["cover_style"] == "generic"
    assert matcher._build_cover_letter_positioning_block(ELECTRICIAN) == ""
    # Explicit stale style input cannot force a QA template on a generic profile.
    assert matcher._build_cover_letter_style_block(ELECTRICIAN, cover_style="api_qa") == matcher._build_cover_letter_style_block(ELECTRICIAN)


def test_generic_cover_accepts_confirmed_years_without_junior_qa_ceiling(evidence, monkeypatch):
    monkeypatch.setattr(config, "VACANCY_FILTER_POLICY", "generic")
    claim = "Работаю электриком 5 лет."
    (evidence / "resume.md").write_text(RESUME + " " + claim, encoding="utf-8")
    _, cover, client = cover_prompt(monkeypatch, ELECTRICIAN, claim)
    assert cover == claim + " Обсудим детали?"
    assert len(client.calls) == 2
    assert matcher.analyze_cover_letter(cover)["grounding_status"] == "verified"


def test_generic_cover_rejects_invented_group_even_with_supported_verdict(evidence, monkeypatch):
    monkeypatch.setattr(config, "VACANCY_FILTER_POLICY", "generic")
    claim = "Есть IV группа электробезопасности."
    client = client_for(claim + " Обсудим детали?", json.dumps(approved(claim)))
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    cover = asyncio.run(matcher.generate_cover_letter(ELECTRICIAN))
    assert claim not in cover
    assert matcher.analyze_cover_letter(cover)["fallback_cover_letter"] is True


def test_generic_scoring_keeps_boundaries_after_large_common_knowledge(evidence, monkeypatch):
    monkeypatch.setattr(config, "VACANCY_FILTER_POLICY", "generic")
    (evidence / "knowledge" / "candidate_facts.md").write_text(
        "- [FACT] Проверка программных продуктов.\n" * 2000 + LIMITS
        + "\n- [FACT] Монтаж кабеля и слаботочных систем.\n", encoding="utf-8")
    prompt, _ = scoring_prompt(monkeypatch, ELECTRICIAN)
    assert all(line in prompt for line in LIMITS.splitlines())
    assert "Монтаж кабеля и слаботочных систем." in prompt


@pytest.mark.parametrize("policy,expected", [("generic", False), ("qa", True)])
def test_questionnaire_qa_templates_and_risky_questions_are_policy_bound(monkeypatch, policy, expected):
    monkeypatch.setattr(config, "VACANCY_FILTER_POLICY", policy)
    assert bool(forms.answer_question_from_library("Ваш опыт Python и автотестов?", max_chars=1000)) is expected
    assert forms.is_risky_question("Сколько лет коммерческого опыта AQA?") is expected
    assert bool(forms.answer_question_from_library("Какой формат работы и график?", max_chars=1000)) is expected


def test_questionnaire_uses_captured_policy_after_profile_switch(monkeypatch):
    from types import SimpleNamespace
    from answer_grounding import candidate_context
    monkeypatch.setattr(config, "VACANCY_FILTER_POLICY", "qa")
    with candidate_context(None, settings=SimpleNamespace(VACANCY_FILTER_POLICY="generic")):
        assert forms.answer_question_from_library("Ваш опыт SQL?", max_chars=1000) is None
        assert forms.is_risky_question("Сколько лет коммерческого опыта AQA?") is False


@pytest.mark.parametrize("policy", ["qa", "generic"])
def test_legacy_selector_rules_follow_policy(monkeypatch, policy):
    monkeypatch.setattr(config, "VACANCY_FILTER_POLICY", policy)
    client = client_for('{"selected": [1]}')
    selected = asyncio.run(prompt_blocks.select_kb_sections(
        "Монтаж электропроводки", [{"num": 1, "title": "Монтаж кабеля"}], client))
    assert selected == [1]
    prompt = client.calls[0]["messages"][1]["content"]
    assert ("QA-кандидата" in prompt) is (policy == "qa")
    assert ("обязательно секция «API-тестирование»" in prompt) is (policy == "qa")
    assert ("оборудование" in prompt) is (policy == "generic")


@pytest.mark.parametrize("policy,upper,bonus", [("qa", 79, 14), ("generic", 57, 0)])
def test_generic_does_not_use_qa_middle_threshold_or_junior_bonus(monkeypatch, policy, upper, bonus):
    monkeypatch.setattr(config, "VACANCY_FILTER_POLICY", policy)
    monkeypatch.setattr(config, "HH_MATCHER_AUTO_APPLY_MIN_SCORE", 58)
    monkeypatch.setattr(config, "HH_MATCHER_MIDDLE_CHALLENGE_MIN_SCORE", 80)
    monkeypatch.setattr(config, "HH_MATCHER_MANUAL_REVIEW_MAX_SCORE", 0)
    assert matcher._manual_review_score_window()[1] == upper
    score = matcher.estimate_response_probability_score(dict(ELECTRICIAN, snippet="Монтаж кабеля"), cluster="generic")
    junior = matcher.estimate_response_probability_score(dict(ELECTRICIAN, snippet="Junior, обучение"), cluster="generic")
    assert junior - score == bonus


@pytest.mark.parametrize("policy,expected", [("qa", "QA инженер"), ("generic", "Электрик")])
def test_google_form_fallback_does_not_prefer_qa_for_generic(monkeypatch, policy, expected):
    from google_forms.answering import _required_option_fallback
    monkeypatch.setattr(config, "VACANCY_FILTER_POLICY", policy)
    assert _required_option_fallback(["Электрик", "QA инженер"], "Направление?") == [expected]


def test_generic_questionnaire_generates_from_evidence_instead_of_qa_library(evidence, monkeypatch):
    from tests.test_answer_grounding_regressions import ScriptedResponses, evidence as proof, question_dependencies
    monkeypatch.setattr(config, "VACANCY_FILTER_POLICY", "generic")
    client = ScriptedResponses(({"status": "answer", "answer": CLAIM}, "stop"),
                               (proof(CLAIM), "stop"))
    result = asyncio.run(forms.answer_question_with_llm(
        {"question_text": "Опыт монтажа, есть ли automation?", "input_type": "textarea"},
        RESUME, **question_dependencies(client)))
    assert result == CLAIM
    assert len(client.calls) == 2
    assert "вопрос работодателя" in client.calls[0]["messages"][1]["content"]
    assert "manual/API QA" not in str(client.calls)


def test_electrician_with_shared_knowledge_scores_and_writes_offline(tmp_path, monkeypatch):
    home = tmp_path / "owner"
    common = home / "knowledge"
    common.mkdir(parents=True)
    (common / "candidate_facts.md").write_text(f"- [FACT] {CLAIM}\n{LIMITS}", encoding="utf-8")
    direction = home / "profiles" / "electrician"
    direction.mkdir(parents=True)
    (direction / "knowledge").symlink_to(common, target_is_directory=True)
    (direction / "resume.md").write_text(RESUME, encoding="utf-8")
    monkeypatch.setattr(config, "RESUME_FILE", str(direction / "resume.md"))
    monkeypatch.setattr(config, "VACANCY_FILTER_POLICY", "generic")
    monkeypatch.setattr(config, "HH_AUTO_ANSWER_PROFILE_NOTE", "")
    monkeypatch.setattr(matcher.resume_versions, "record_input", lambda *args: None)
    monkeypatch.setenv("KB_RETRIEVAL_ENABLED", "1")
    evaluation_prompt, result = scoring_prompt(monkeypatch, ELECTRICIAN)
    prompt, cover, _ = cover_prompt(monkeypatch, ELECTRICIAN)
    assert result["score"] == 95 and result["cluster"] == "generic"
    assert cover == CLAIM + " Обсудим детали?"
    for text in (evaluation_prompt, prompt):
        assert all(line in text for line in LIMITS.splitlines())
