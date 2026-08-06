import asyncio

import google_form_filler as legacy_google_forms
from google_forms import extraction


class FakePage:
    def __init__(self, questions):
        self.questions = questions
        self.selector = ""
        self.timeout = 0
        self.script = ""

    async def wait_for_selector(self, selector, *, timeout):
        self.selector = selector
        self.timeout = timeout

    async def evaluate(self, script):
        self.script = script
        return self.questions


def test_extract_form_questions_filters_empty_and_info_blocks():
    page = FakePage(
        [
            {"index": 0, "question": "Ваш опыт", "type": "text", "required": True},
            {"index": 1, "question": "   ", "type": "text", "required": False},
            {
                "index": 2,
                "question": "Благодарим за заполнение анкеты. Мы свяжемся с вами.",
                "type": "text",
                "required": False,
            },
        ]
    )

    questions = asyncio.run(extraction.extract_form_questions(page))

    assert questions == [
        {"index": 0, "question": "Ваш опыт", "type": "text", "required": True}
    ]
    assert page.selector == 'form, div[role="listitem"]:visible'
    assert page.timeout == 30000
    assert "document.querySelectorAll" in page.script


def test_legacy_module_reexports_extraction_helpers():
    assert legacy_google_forms.extract_form_questions is extraction.extract_form_questions
    assert legacy_google_forms._looks_like_form_info_block is extraction._looks_like_form_info_block
