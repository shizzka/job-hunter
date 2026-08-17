from hh.chat import (
    message_matches_sent_text,
    messages_contain_sent_text,
    normalize_sent_message_text,
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
