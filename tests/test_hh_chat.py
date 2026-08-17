import asyncio

from hh.chat import (
    CHATIK_CHAT_READY_SELECTOR,
    dismiss_cookies_banner,
    fill_and_preview,
    find_quick_reply_button,
    extract_messages,
    get_messages_safe,
    list_chats,
    message_matches_sent_text,
    messages_contain_sent_text,
    normalize_sent_message_text,
    open_chatik_page,
    quick_reply_choice,
)


def test_normalize_sent_message_text_collapses_nbsp_and_whitespace():
    assert normalize_sent_message_text(" Да\xa0\n 13:07 ") == "Да 13:07"


def test_sent_text_helpers_find_own_message_before_follow_up():
    expected = "Рассматриваю предложения от 80 000 ₽ на руки."
    messages = [
        {"text": expected + "\n\n08:47", "is_me": True},
        {"text": "Есть высшее образование?", "is_me": False},
    ]

    assert message_matches_sent_text(messages[0]["text"], expected) is True
    assert messages_contain_sent_text(messages, expected) is True


def test_quick_reply_choice_extracts_only_leading_yes_or_no():
    assert quick_reply_choice("Нет, высшего образования нет.") == "Нет"
    assert quick_reply_choice("Да. Есть опыт.") == "Да"
    assert quick_reply_choice("Готов обсудить детали.") == ""


class FakeNavigationPage:
    def __init__(self):
        self.url = "about:blank"
        self.goto_calls = []
        self.waited_selectors = []
        self.wait_calls = []

    async def goto(self, url: str, **kwargs):
        self.goto_calls.append((url, kwargs))
        self.url = url
        if len(self.goto_calls) == 1:
            raise TimeoutError("navigation timed out")

    async def wait_for_selector(self, selector: str, **kwargs):
        self.waited_selectors.append((selector, kwargs))

    async def wait_for_timeout(self, timeout_ms: int):
        self.wait_calls.append(timeout_ms)


def test_open_chatik_page_retries_with_injected_reset():
    page = FakeNavigationPage()
    reset_calls = []

    async def reset_page(current_page):
        reset_calls.append(current_page)

    asyncio.run(
        open_chatik_page(
            page,
            "https://chatik.hh.ru/chat/123",
            CHATIK_CHAT_READY_SELECTOR,
            settle_ms=25,
            navigation_timeout_ms=321,
            ready_timeout_ms=456,
            reset_page=reset_page,
        )
    )

    assert len(page.goto_calls) == 2
    assert all(
        call[1] == {"wait_until": "commit", "timeout": 321} for call in page.goto_calls
    )
    assert page.waited_selectors == [
        (CHATIK_CHAT_READY_SELECTOR, {"state": "attached", "timeout": 456})
    ]
    assert page.wait_calls == [25]
    assert reset_calls == [page]


class FakeEvaluatePage:
    def __init__(self, result):
        self.result = result
        self.scripts = []

    async def evaluate(self, script: str):
        self.scripts.append(script)
        return self.result


def test_list_chats_uses_injected_navigation_and_returns_evaluated_rows():
    expected = [{"chat_id": "123", "preview": "QA Engineer"}]
    page = FakeEvaluatePage(expected)
    open_calls = []

    async def open_page(*args, **kwargs):
        open_calls.append((args, kwargs))

    result = asyncio.run(list_chats(page, open_page=open_page))

    assert result == expected
    assert open_calls == [
        (
            (
                page,
                "https://chatik.hh.ru/",
                '[data-qa^="chatik-open-chat-"]',
            ),
            {
                "settle_ms": 2000,
            },
        )
    ]
    assert "[data-qa^=\"chatik-open-chat-\"]" in page.scripts[0]


def test_extract_messages_runs_injected_author_classifier():
    message = {
        "id": "10",
        "text": "Вопрос",
        "author": "Робот-рекрутер",
        "is_me": False,
    }
    page = FakeEvaluatePage({"messages": [message], "vacancy": {}})
    classified = []

    def classify_author(item):
        item["classified"] = True
        classified.append(item)
        return item

    result = asyncio.run(extract_messages(page, classify_author=classify_author))

    assert result["messages"][0]["classified"] is True
    assert classified == [message]


def test_get_messages_safe_returns_structured_error_with_injected_dependencies():
    page = object()
    reset_calls = []

    async def fail_open(*args, **kwargs):
        raise TimeoutError("chat did not render")

    async def reset_page(current_page):
        reset_calls.append(current_page)

    result = asyncio.run(
        get_messages_safe(
            page,
            "123",
            open_page=fail_open,
            reset_page=reset_page,
        )
    )

    assert result["messages"] == []
    assert result["vacancy"] == {}
    assert result["chat_id"] == "123"
    assert result["error"] == "TimeoutError: chat did not render"
    assert reset_calls == [page]


class FakeCookieButton:
    def __init__(self, text: str = ""):
        self.text = text
        self.clicked = False

    async def click(self):
        self.clicked = True

    async def inner_text(self):
        return self.text

    async def is_visible(self):
        return True

    async def is_enabled(self):
        return True


class FakeCookiePage:
    def __init__(self, button):
        self.button = button
        self.selectors = []
        self.wait_calls = []

    async def query_selector(self, selector: str):
        self.selectors.append(selector)
        if selector == '[data-qa="cookies-policy-banner-accept"]':
            return self.button
        return None

    async def wait_for_timeout(self, timeout_ms: int):
        self.wait_calls.append(timeout_ms)


def test_dismiss_cookies_banner_uses_existing_selector_order():
    button = FakeCookieButton()
    page = FakeCookiePage(button)

    asyncio.run(dismiss_cookies_banner(page))

    assert page.selectors == [
        '[data-qa="cookies-policy-informer-accept"]',
        '[data-qa="cookies-policy-banner-accept"]',
    ]
    assert button.clicked is True
    assert page.wait_calls == [400]


class FakeQuickReplyPage:
    def __init__(self, buttons):
        self.buttons = buttons

    async def query_selector_all(self, selector: str):
        assert selector == "button"
        return self.buttons


def test_find_quick_reply_button_returns_visible_enabled_exact_match():
    ignored = FakeCookieButton("Нет")
    expected = FakeCookieButton("Да")
    page = FakeQuickReplyPage([ignored, expected])

    result = asyncio.run(find_quick_reply_button(page, "Да"))

    assert result is expected


class FakePreviewPage:
    def __init__(self):
        self.screenshot_paths = []

    async def screenshot(self, *, path: str):
        self.screenshot_paths.append(path)


def test_fill_and_preview_keeps_quick_reply_unsubmitted():
    page = FakePreviewPage()
    open_calls = []
    dismiss_calls = []
    quick_button = object()

    async def open_page(*args, **kwargs):
        open_calls.append((args, kwargs))

    async def dismiss_cookies(current_page):
        dismiss_calls.append(current_page)

    async def find_quick_reply(current_page, choice: str):
        assert current_page is page
        assert choice == "Да"
        return quick_button

    result = asyncio.run(
        fill_and_preview(
            page,
            "123",
            "Да, готов.",
            state_dir="/tmp/chat-state",
            now=lambda: 456.9,
            open_page=open_page,
            dismiss_cookies=dismiss_cookies,
            find_quick_reply=find_quick_reply,
        )
    )

    assert result == {
        "filled": True,
        "quick_reply": "Да",
        "screenshot_path": "/tmp/chat-state/chat_preview_123_456.png",
    }
    assert open_calls == [
        (
            (
                page,
                "https://chatik.hh.ru/chat/123",
                CHATIK_CHAT_READY_SELECTOR,
            ),
            {"settle_ms": 2000},
        )
    ]
    assert dismiss_calls == [page]
    assert page.screenshot_paths == ["/tmp/chat-state/chat_preview_123_456.png"]


class FakeTextInput:
    def __init__(self):
        self.focused = False
        self.value = ""

    async def focus(self):
        self.focused = True

    async def fill(self, value: str):
        self.value = value


class FakeTextPreviewPage(FakePreviewPage):
    def __init__(self):
        super().__init__()
        self.input = FakeTextInput()
        self.wait_calls = []

    async def query_selector(self, selector: str):
        assert selector == 'textarea[data-qa="chatik-new-message-text"]'
        return self.input

    async def wait_for_timeout(self, timeout_ms: int):
        self.wait_calls.append(timeout_ms)


def test_fill_and_preview_fills_textarea_without_sending():
    page = FakeTextPreviewPage()

    async def no_op(*args, **kwargs):
        return None

    result = asyncio.run(
        fill_and_preview(
            page,
            "456",
            "Готов обсудить детали.",
            state_dir="/tmp/chat-state",
            now=lambda: 789,
            open_page=no_op,
            dismiss_cookies=no_op,
            find_quick_reply=no_op,
        )
    )

    assert result == {
        "filled": True,
        "screenshot_path": "/tmp/chat-state/chat_preview_456_789.png",
    }
    assert page.input.focused is True
    assert page.input.value == "Готов обсудить детали."
    assert page.wait_calls == [500]
    assert page.screenshot_paths == ["/tmp/chat-state/chat_preview_456_789.png"]
