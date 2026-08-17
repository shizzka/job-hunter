import asyncio

import hh_client
from hh import forms


def test_legacy_question_helpers_are_reexported():
    assert hh_client._extract_resume_salary_text is forms.extract_resume_salary_text
    assert hh_client._extract_numeric_salary is forms.extract_numeric_salary
    assert hh_client._is_salary_question is forms.is_salary_question
    assert hh_client._truncate_text is forms.truncate_text
    assert hh_client._format_question_answer_note is forms.format_question_answer_note
    assert hh_client._question_answer_item is forms.question_answer_item
    assert hh_client._is_risky_question is forms.is_risky_question
    assert hh_client._answer_question_from_library is forms.answer_question_from_library


def test_extract_resume_salary_text_prefers_salary_section():
    resume = "Опыт\n100 000 руб. оборота\n\n## Зарплата\n120 000 ₽ на руки\n"

    assert forms.extract_resume_salary_text(resume) == "120 000 ₽ на руки"


def test_extract_numeric_salary_limits_implausibly_long_value():
    assert forms.extract_numeric_salary("120 000 ₽") == "120000"
    assert forms.extract_numeric_salary("123456789") == "123456"
    assert forms.extract_numeric_salary("по договоренности") == ""


def test_question_answer_item_keeps_skip_metadata():
    assert forms.question_answer_item(
        "Вопрос",
        "Ответ",
        skipped=True,
        skip_reason="manual",
    ) == {
        "question": "Вопрос",
        "answer": "Ответ",
        "skipped": True,
        "skip_reason": "manual",
    }


def test_answer_library_rejects_risky_question():
    question = "Сколько лет коммерческого опыта AQA на Java?"

    assert forms.is_risky_question(question) is True
    assert forms.answer_question_from_library(question, max_chars=200) is None


class FakeInspectPage:
    async def evaluate(self, script):
        assert "codex:auto-question-inspect" in script
        return {"fields": [{"field_id": "field-1"}]}


def test_inspect_employer_questions_repairs_missing_result_keys():
    result = asyncio.run(
        forms.inspect_employer_questions(FakeInspectPage(), logger=hh_client.log)
    )

    assert result == {
        "page_text": "",
        "fields": [{"field_id": "field-1"}],
        "unsupported_fields": 0,
        "unsupported_items": [],
    }


def test_legacy_inspect_wrapper_forwards_patchable_dependencies(monkeypatch):
    client = hh_client.HHClient()
    client._page = object()
    captured = {}

    async def fake_inspect(page, *, logger):
        captured.update(page=page, logger=logger)
        return {"fields": []}

    monkeypatch.setattr(hh_client, "_inspect_employer_questions", fake_inspect)

    assert asyncio.run(client._inspect_employer_questions()) == {"fields": []}
    assert captured == {"page": client._page, "logger": hh_client.log}
