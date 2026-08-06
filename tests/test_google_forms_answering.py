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
