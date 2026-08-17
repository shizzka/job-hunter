import asyncio

from hh.chat import (
    CHATIK_CHAT_READY_SELECTOR,
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
