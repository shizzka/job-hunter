import asyncio

import hh_chat_responder as chat_responder


def test_ai_recruiter_self_intro_text_marks_named_assistant_message():
    message = {
        "id": "14320587410",
        "text": (
            "Олеся\n\nЗдравствуйте, Евгений. Я ассистент рекрутера на базе AI, "
            "благодарю за ваш отклик. Готовы ли вы рассмотреть эту вакансию?"
        ),
        "author": "Олеся",
        "avatar_alt": "",
        "avatar_src": "",
        "is_me": False,
    }

    classified = chat_responder._classify_message_author(message)

    assert classified["is_ai"] is True
    assert classified["is_other"] is False


def test_own_message_with_ai_words_stays_own_message():
    message = {
        "id": "1",
        "text": "Готов обсудить детали с AI-рекрутером и командой.",
        "author": "",
        "avatar_alt": "",
        "avatar_src": "",
        "is_me": True,
    }

    classified = chat_responder._classify_message_author(message)

    assert classified["is_me"] is True
    assert classified["is_ai"] is False
    assert classified["is_other"] is False


def test_known_hh_ai_avatar_still_marks_ai_message():
    message = {
        "id": "2",
        "text": "Ответьте на вопрос работодателя",
        "author": "",
        "avatar_alt": "",
        "avatar_src": "https://hhcdn.ru/file/18274603.png?from=chatik",
        "is_me": False,
    }

    classified = chat_responder._classify_message_author(message)

    assert classified["avatar_src"] == "https://hhcdn.ru/file/18274603.png"
    assert classified["is_ai"] is True


def test_robot_recruiter_author_label_marks_ai_message():
    message = {
        "id": "14020977208",
        "text": "Здравствуйте! Чтобы работодатель узнал о вас больше, ответьте на вопросы.",
        "author": "Робот-рекрутер",
        "avatar_alt": "",
        "avatar_src": "",
        "is_me": False,
    }

    classified = chat_responder._classify_message_author(message)

    assert classified["is_ai"] is True
    assert classified["is_other"] is False


def test_robot_recruiter_icon_label_marks_continuation_question_as_ai():
    message = {
        "id": "14353864970",
        "text": "Подскажите, пожалуйста, какие у Вас зарплатные ожидания?",
        "author": "",
        "avatar_alt": "Робот-рекрутер",
        "avatar_src": "",
        "is_me": False,
    }

    classified = chat_responder._classify_message_author(message)

    assert classified["is_ai"] is True
    assert classified["is_other"] is False


def test_human_name_with_ii_and_recruiter_word_does_not_mark_ai():
    message = {
        "id": "3",
        "text": "Здравствуйте, готов обсудить вакансию.",
        "author": "Даниил рекрутер",
        "avatar_alt": "",
        "avatar_src": "",
        "is_me": False,
    }

    classified = chat_responder._classify_message_author(message)

    assert classified["is_ai"] is False
    assert classified["is_other"] is True


def test_human_hr_question_mentioning_ai_assistant_does_not_mark_ai():
    message = {
        "id": "4",
        "text": "Подскажите, есть ли у вас опыт тестирования AI-ассистентов или чат-ботов?",
        "author": "Мария",
        "avatar_alt": "",
        "avatar_src": "",
        "is_me": False,
    }

    classified = chat_responder._classify_message_author(message)

    assert classified["is_ai"] is False
    assert classified["is_other"] is True


def test_message_matches_sent_text_with_chat_timestamp_suffix():
    expected = "Не готов рассматривать эту позицию, так как нет нужного опыта."
    actual = expected + "\n\n10:02"

    assert chat_responder._message_matches_sent_text(actual, expected) is True


def test_message_matches_sent_text_short_answer():
    assert chat_responder._message_matches_sent_text("Да\n\n13:07", "Да") is True


def test_message_matches_sent_text_rejects_mismatch():
    assert chat_responder._message_matches_sent_text("Спасибо, остаемся на связи", "Да") is False


def test_open_chatik_page_resets_stuck_tab_and_retries():
    class FakePage:
        def __init__(self):
            self.goto_calls = []
            self.waited_selectors = []

        async def goto(self, url, **kwargs):
            self.goto_calls.append((url, kwargs))
            if url.startswith(chat_responder.CHATIK_ROOT) and len(self.goto_calls) == 1:
                raise TimeoutError("navigation timed out")

        async def wait_for_selector(self, selector, **kwargs):
            self.waited_selectors.append((selector, kwargs))

        async def wait_for_timeout(self, timeout):
            return None

    page = FakePage()
    url = f"{chat_responder.CHATIK_ROOT}/chat/123"

    asyncio.run(
        chat_responder._open_chatik_page(
            page,
            url,
            chat_responder.CHATIK_CHAT_READY_SELECTOR,
            settle_ms=0,
        )
    )

    assert [call[0] for call in page.goto_calls] == [url, "about:blank", url]
    assert all(call[1]["wait_until"] == "commit" for call in page.goto_calls)
    assert page.waited_selectors == [
        (
            chat_responder.CHATIK_CHAT_READY_SELECTOR,
            {"state": "attached", "timeout": chat_responder.CHATIK_READY_TIMEOUT_MS},
        )
    ]


def test_application_only_preview_is_skipped_until_a_message_arrives():
    assert chat_responder._is_application_only_preview(
        "Middle QA инженер 23:24 PBF group Отклик на вакансию"
    ) is True
    assert chat_responder._is_application_only_preview(
        "Middle QA инженер 23:25 PBF group Ответьте на несколько вопросов"
    ) is False


def test_messages_contain_sent_text_when_robot_immediately_asks_next_question():
    expected = "Рассматриваю предложения от 80 000 ₽ на руки."
    messages = [
        {"text": expected + "\n\n08:47", "is_me": True},
        {"text": "Есть у Вас высшее техническое образование?", "is_me": False, "is_ai": True},
    ]

    assert chat_responder._messages_contain_sent_text(messages, expected) is True


def test_quick_reply_choice_extracts_yes_and_no():
    assert chat_responder._quick_reply_choice("Нет, высшего образования нет.") == "Нет"
    assert chat_responder._quick_reply_choice("Да. Есть опыт.") == "Да"


def test_quick_reply_choice_ignores_unrelated_answer():
    assert chat_responder._quick_reply_choice("Готов обсудить детали.") == ""


def test_hidden_ai_like_hr_screening_marks_suspicious_not_ai():
    message = {
        "id": "14400000001",
        "text": (
            "Здравствуйте, Евгений! Подскажите, пожалуйста, какой у вас опыт "
            "тестирования API и какие зарплатные ожидания по вакансии?"
        ),
        "author": "Анна",
        "avatar_alt": "",
        "avatar_src": "",
        "is_me": False,
    }

    classified = chat_responder._classify_message_author(message)

    assert classified["is_ai"] is False
    assert classified["is_ai_suspect"] is True
    assert classified["is_other"] is True


def test_plain_human_hr_message_is_not_suspicious_screening():
    message = {
        "id": "14400000002",
        "text": "Здравствуйте, Евгений! Спасибо за отклик, сегодня вернусь с обратной связью.",
        "author": "Мария",
        "avatar_alt": "",
        "avatar_src": "",
        "is_me": False,
    }

    classified = chat_responder._classify_message_author(message)

    assert classified["is_ai"] is False
    assert classified["is_ai_suspect"] is False
    assert classified["is_other"] is True


def test_cian_pet_project_question_is_suspicious_screening():
    message = {
        "id": "14409993530",
        "text": (
            "Алина\n\nПоняла, спасибо! А расскажи, участвовал ли ты в pet-проектах, "
            "хакатонах или учебных проектах? Например, в рамках курсов или самостоятельно?"
        ),
        "author": "Алина",
        "avatar_alt": "Алина",
        "avatar_src": "",
        "is_me": False,
    }

    classified = chat_responder._classify_message_author(message)

    assert classified["is_ai"] is False
    assert classified["is_ai_suspect"] is True


def test_cian_study_certificate_question_is_suspicious_screening():
    message = {
        "id": "14410048077",
        "text": (
            "Алина\n\nПоняла, спасибо! А сможешь ли ты предоставить справку об обучении "
            "с места учёбы перед началом стажировки?"
        ),
        "author": "Алина",
        "avatar_alt": "Алина",
        "avatar_src": "",
        "is_me": False,
    }

    classified = chat_responder._classify_message_author(message)

    assert classified["is_ai"] is False
    assert classified["is_ai_suspect"] is True


def test_study_certificate_question_has_safe_deterministic_answer():
    answer = chat_responder._deterministic_chat_answer(
        "А сможешь ли ты предоставить справку об обучении с места учёбы перед началом стажировки?"
    )

    assert answer
    assert "не смогу" in answer
    assert "документы об образовании" in answer


def test_preview_markup_contains_send_callback():
    markup = chat_responder.build_chat_answer_preview_markup("qa", "5394116371", "14410048077")
    buttons = [button for row in markup["inline_keyboard"] for button in row]

    assert any(button.get("text") == "Открыть чат" and button.get("url") for button in buttons)
    assert any(
        button.get("text") == "Отправить ответ"
        and button.get("callback_data") == "chat_send:qa:5394116371:14410048077"
        for button in buttons
    )


def test_get_messages_safe_returns_empty_result_on_timeout(monkeypatch):
    async def fail_open(*args, **kwargs):
        raise TimeoutError("chat did not render")

    class FakePage:
        url = f"{chat_responder.CHATIK_ROOT}/chat/123"

        async def goto(self, *args, **kwargs):
            return None

    monkeypatch.setattr(chat_responder, "_open_chatik_page", fail_open)

    result = asyncio.run(chat_responder.get_messages_safe(FakePage(), "123"))

    assert result["messages"] == []
    assert result["vacancy"] == {}
    assert result["chat_id"] == "123"
    assert "TimeoutError" in result["error"]
