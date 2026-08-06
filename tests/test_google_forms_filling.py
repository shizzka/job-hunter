import asyncio

import google_form_filler as legacy_google_forms
from google_forms import filling


def test_merge_fill_results_preserves_page_order():
    merged = filling._merge_fill_results(
        [
            {"filled": [{"index": 0}], "skipped": [{"index": 1}]},
            {"filled": [{"index": 2}], "skipped": []},
        ]
    )

    assert merged == {
        "filled": [{"index": 0}, {"index": 2}],
        "skipped": [{"index": 1}],
    }


def test_question_signature_normalizes_case_and_whitespace():
    questions = [
        {"question": "  QA   Experience "},
        {"question": "ТЕЛЕГРАМ"},
    ]

    assert filling._question_signature(questions) == ["qa experience", "телеграм"]


def test_legacy_module_reexports_pure_filling_helpers():
    helper_names = (
        "_google_form_preview_status",
        "_is_google_form_next_button_text",
        "_is_google_form_submit_button_text",
        "_looks_like_google_form_login_required",
        "_looks_like_google_form_submit_success",
        "_merge_fill_results",
        "_question_signature",
        "_reindex_page_questions",
    )

    for name in helper_names:
        assert getattr(legacy_google_forms, name) is getattr(filling, name)


class FakeLocator:
    def __init__(self, handles):
        self.handles = handles

    async def element_handles(self):
        return self.handles


class FakePage:
    def __init__(self, items):
        self.items = items

    def locator(self, selector):
        assert selector == 'div[role="listitem"]:visible'
        return FakeLocator(self.items)


class FakeField:
    def __init__(self):
        self.value = ""

    async def fill(self, value):
        self.value = value

    async def input_value(self):
        return self.value


class FakeOption:
    def __init__(self, label):
        self.label = label
        self.checked = False

    async def get_attribute(self, name):
        if name == "aria-label":
            return self.label
        if name == "aria-checked":
            return "true" if self.checked else "false"
        return None

    async def inner_text(self):
        return self.label

    async def click(self, *, timeout, force=False):
        self.checked = True


class FakeItem:
    def __init__(self, *, field=None, options=None):
        self.field = field
        self.options = options or []

    async def query_selector(self, selector):
        return self.field

    async def query_selector_all(self, selector):
        return self.options


def test_fill_form_fills_text_and_selects_radio_option():
    field = FakeField()
    no_option = FakeOption("Нет")
    yes_option = FakeOption("Да")
    page = FakePage(
        [
            FakeItem(field=field),
            FakeItem(options=[no_option, yes_option]),
        ]
    )
    questions = [
        {"index": 0, "dom_index": 0, "question": "Опыт", "type": "text"},
        {
            "index": 1,
            "dom_index": 1,
            "question": "Готовы?",
            "type": "radio",
            "options": ["Нет", "Да"],
        },
    ]
    answers = [
        {"index": 0, "answer": "Три года в QA", "skip": False},
        {"index": 1, "answer": "Да", "options": ["Да"], "skip": False},
    ]

    result = asyncio.run(filling.fill_form(page, questions, answers))

    assert result == {
        "filled": [
            {"index": 0, "type": "text", "answer": "Три года в QA"},
            {"index": 1, "type": "radio", "options": ["Да"]},
        ],
        "skipped": [],
    }
    assert field.value == "Три года в QA"
    assert no_option.checked is False
    assert yes_option.checked is True


def test_click_google_form_option_retries_with_force():
    class ForceHandle:
        def __init__(self):
            self.calls = []

        async def click(self, *, timeout, force=False):
            self.calls.append(force)
            if not force:
                raise RuntimeError("regular click failed")

        async def evaluate(self, script):
            raise AssertionError("evaluate fallback should not be used")

    handle = ForceHandle()

    asyncio.run(filling._click_google_form_option(handle))

    assert handle.calls == [False, True]


def test_legacy_module_reexports_browser_filling_helpers():
    helper_names = (
        "_click_google_form_next",
        "_click_google_form_option",
        "_click_google_form_submit",
        "_fill_google_form_email_consent",
        "_google_form_buttons",
        "_safe_screenshot",
        "_wait_google_form_submit_success",
        "fill_form",
    )

    for name in helper_names:
        assert getattr(legacy_google_forms, name) is getattr(filling, name)
