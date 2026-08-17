import asyncio

from hh.chat import (
    CHATIK_CHAT_READY_SELECTOR,
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
