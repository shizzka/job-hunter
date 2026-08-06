import asyncio
from types import SimpleNamespace

import google_form_filler as legacy_google_forms
from google_forms import answering


def test_prepare_form_answers_applies_contacts_and_required_fallbacks(monkeypatch):
    monkeypatch.setenv("CANDIDATE_EMAIL", "qa@example.com")
    questions = [
        {"index": 1, "question": "Электронная почта", "type": "text", "required": True},
        {
            "index": 2,
            "question": "Согласны пройти тестовое?",
            "type": "radio",
            "required": True,
            "options": ["Нет", "Да"],
        },
    ]
    answers = [
        {"index": 1, "answer": "не указана", "skip": False},
        {"index": 2, "answer": "skip", "skip": True},
    ]

    prepared = answering._prepare_form_answers(questions, answers)
    by_index = {item["index"]: item for item in prepared}

    assert by_index[1]["answer"] == "qa@example.com"
    assert by_index[1]["source"] == "contact_override"
    assert by_index[2]["options"] == ["Да"]
    assert by_index[2]["source"] == "required_fallback"


def test_required_salary_fallback_uses_configured_baseline(monkeypatch):
    monkeypatch.setattr(answering.config, "HH_AUTO_ANSWER_SALARY_BASELINE", "150000")

    answer = answering._required_text_fallback("Ваши зарплатные ожидания?")

    assert "150000 ₽" in answer


def test_legacy_module_reexports_answer_preparation_helpers():
    helper_names = (
        "_apply_contact_overrides",
        "_apply_required_overrides",
        "_avoid_bare_other_options",
        "_best_option_match",
        "_norm",
        "_prepare_form_answers",
    )

    for name in helper_names:
        assert getattr(legacy_google_forms, name) is getattr(answering, name)


def test_generate_form_answers_uses_injected_client_and_parser(monkeypatch):
    import hh_client
    import prompt_blocks

    monkeypatch.setattr(hh_client, "_load_resume_text", lambda: "QA resume")
    monkeypatch.setattr(prompt_blocks, "build_profile_note_block", lambda: "profile\n")
    monkeypatch.setattr(prompt_blocks, "build_contact_block", lambda: "")
    monkeypatch.setattr(prompt_blocks, "build_facts_block", lambda: "facts\n")
    monkeypatch.setattr(prompt_blocks, "build_salary_rule_block", lambda: "")
    monkeypatch.setattr(prompt_blocks, "build_knowledge_base_block", lambda **kwargs: "fallback\n")
    monkeypatch.setattr(prompt_blocks, "get_candidate_contacts", lambda: {})
    monkeypatch.setattr(answering.config, "HH_QUESTION_MODEL", "test-model")

    async def filtered_knowledge(*args, **kwargs):
        return "knowledge\n"

    monkeypatch.setattr(prompt_blocks, "build_filtered_kb_block", filtered_knowledge)

    class FakeCompletions:
        def __init__(self):
            self.request = {}

        async def create(self, **kwargs):
            self.request = kwargs
            message = SimpleNamespace(content='{"answers":[]}')
            return SimpleNamespace(choices=[SimpleNamespace(message=message)])

    completions = FakeCompletions()
    client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    parsed = {
        "answers": [
            {
                "index": 0,
                "answer": "Три года в QA",
                "options": [],
                "skip": False,
                "confidence": "high",
            }
        ]
    }

    answers = asyncio.run(
        answering.generate_form_answers(
            [{"index": 0, "question": "Опыт", "type": "text", "required": False}],
            vacancy={"title": "QA engineer", "company": "Example"},
            source_message="Заполните форму",
            client_factory=lambda: client,
            json_parser=lambda raw: parsed,
        )
    )

    assert answers == parsed["answers"]
    assert completions.request["model"] == "test-model"
    assert completions.request["temperature"] == 0.2
    assert "QA engineer" in completions.request["messages"][1]["content"]
    assert "Заполните форму" in completions.request["messages"][1]["content"]


def test_legacy_generate_form_answers_forwards_patchable_dependencies(monkeypatch):
    captured = {}
    client_factory = lambda: object()
    json_parser = lambda raw: {}

    async def fake_generate(questions, **kwargs):
        captured.update(kwargs)
        return questions

    monkeypatch.setattr(legacy_google_forms, "_generate_form_answers", fake_generate)
    monkeypatch.setattr(legacy_google_forms, "get_llm_client", client_factory)
    monkeypatch.setattr(legacy_google_forms, "parse_llm_json", json_parser)

    result = asyncio.run(
        legacy_google_forms.generate_form_answers(
            [{"index": 0}],
            vacancy={"title": "QA"},
            source_message="message",
        )
    )

    assert result == [{"index": 0}]
    assert captured["client_factory"] is client_factory
    assert captured["json_parser"] is json_parser
    assert captured["logger"] is legacy_google_forms.log
