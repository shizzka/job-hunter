import asyncio

import hh_chat_responder as chat_responder
from runtime_context import ChatResponderLimits, RuntimePaths


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


def test_explicit_screening_form_question_has_deterministic_answer():
    answer = chat_responder._deterministic_chat_answer(
        "Готовы заполнить короткую анкету по опыту и ответить на несколько вопросов?"
    )

    assert answer
    assert "готов пройти короткую форму" in answer.casefold()


def test_form_word_in_unrelated_question_does_not_trigger_form_answer():
    question = "В какой форме вы обычно фиксируете вопросы по API-тестированию и баг-репорты?"

    assert chat_responder._is_screening_form_question(question) is False
    assert chat_responder._deterministic_chat_answer(question) is None


def test_screening_form_artifact_is_detected_for_guard():
    assert chat_responder._looks_like_screening_form_artifact(
        "Да, готов пройти короткую форму. Заполню вопросы по опыту и навыкам."
    ) is True


def test_generate_answer_drops_form_artifact_for_non_form_question(monkeypatch):
    import prompt_blocks

    class FakeCompletions:
        async def create(self, **kwargs):
            class Response:
                choices = [
                    type(
                        "Choice",
                        (),
                        {
                            "message": type(
                                "Message",
                                (),
                                {
                                    "content": (
                                        '{"status":"answer","answer":"Да, готов пройти короткую форму. '
                                        'Заполню вопросы по опыту и навыкам."}'
                                    )
                                },
                            )()
                        },
                    )()
                ]

            return Response()

    class FakeClient:
        chat = type("Chat", (), {"completions": FakeCompletions()})()

    async def fake_filtered_kb(*args, **kwargs):
        return ""

    monkeypatch.setattr(chat_responder, "_get_llm_client", lambda: FakeClient())
    monkeypatch.setattr(prompt_blocks, "build_profile_note_block", lambda: "")
    monkeypatch.setattr(prompt_blocks, "build_facts_block", lambda: "")
    monkeypatch.setattr(prompt_blocks, "build_salary_rule_block", lambda: "")
    monkeypatch.setattr(prompt_blocks, "build_knowledge_base_block", lambda **kwargs: "")
    monkeypatch.setattr(prompt_blocks, "build_filtered_kb_block", fake_filtered_kb)

    answer = asyncio.run(
        chat_responder.generate_answer(
            [{"text": "Какой у вас опыт API-тестирования?", "is_ai": True}],
            {},
            "QA resume",
        )
    )

    assert answer is None


def test_find_unseen_google_form_message_returns_latest_unseen_form():
    messages = [
        {"id": "10", "text": "Заполните https://docs.google.com/forms/d/e/FORM1/viewform", "is_me": False},
        {"id": "11", "text": "Наш ответ", "is_me": True},
        {"id": "12", "text": "Новая анкета https://forms.gle/abc123", "is_me": False},
    ]
    chat_state = {}

    item = chat_responder._find_unseen_google_form_message(messages, chat_state)

    assert item["message"]["id"] == "12"
    assert item["form_url"] == "https://forms.gle/abc123"


def test_find_unseen_google_form_message_skips_remembered_form():
    messages = [
        {"id": "10", "text": "Заполните https://docs.google.com/forms/d/e/FORM1/viewform", "is_me": False},
    ]
    url = "https://docs.google.com/forms/d/e/FORM1/viewform"
    key = chat_responder._google_form_seen_key(url, "10")
    chat_state = {"google_form_previews": {key: {"ok": True}}}

    assert chat_responder._find_unseen_google_form_message(messages, chat_state) == {}


def test_remember_google_form_preview_keeps_bounded_state():
    chat_state = {}

    for idx in range(35):
        chat_responder._remember_google_form_preview(
            chat_state,
            f"key-{idx}",
            {"ok": True, "token": f"tok{idx}", "form_url": f"https://forms.gle/{idx}", "message_id": str(idx)},
        )

    previews = chat_state["google_form_previews"]
    assert len(previews) == 30
    assert "key-34" in previews


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


def test_process_all_uses_injected_reply_limits(monkeypatch):
    import hh_client
    import notifier

    class FakePage:
        async def goto(self, *args, **kwargs):
            return None

        async def wait_for_timeout(self, timeout):
            return None

    class FakeHHClient:
        _page = FakePage()

    chats = [
        {"chat_id": "limited", "preview": "First question"},
        {"chat_id": "drafted", "preview": "Second question"},
    ]
    messages = {
        "messages": [
            {
                "id": "1",
                "text": "What is your QA experience?",
                "is_ai": True,
                "is_me": False,
            }
        ],
        "vacancy": {"title": "QA", "company": "Example"},
    }
    sleeps = []

    async def fake_list_chats(page):
        return chats

    async def fake_get_messages(page, chat_id):
        return messages

    async def fake_generate_answer(*args, **kwargs):
        return "About one year of practical QA experience."

    async def fake_fill_and_preview(*args, **kwargs):
        return {}

    async def fake_sleep(seconds):
        sleeps.append(seconds)

    async def fake_notify(*args, **kwargs):
        return True

    monkeypatch.setattr(hh_client, "_load_resume_text", lambda: "QA resume")
    monkeypatch.setattr(chat_responder, "load_state", lambda: {"limited": {"replies_count": 3}})
    monkeypatch.setattr(chat_responder, "list_chats", fake_list_chats)
    monkeypatch.setattr(chat_responder, "get_messages", fake_get_messages)
    monkeypatch.setattr(chat_responder, "generate_answer", fake_generate_answer)
    monkeypatch.setattr(chat_responder, "fill_and_preview", fake_fill_and_preview)
    monkeypatch.setattr(chat_responder.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(notifier, "send_message_with_markup", fake_notify)

    summary = asyncio.run(
        chat_responder.process_all(
            FakeHHClient(),
            dry_run=True,
            limits=ChatResponderLimits(
                max_replies_per_chat=3,
                reply_cooldown_s=7,
            ),
        )
    )

    assert summary["chats_scanned"] == 2
    assert summary["answers_drafted"] == 1
    assert summary["skipped"] == 1
    assert sleeps == [7]


def test_chat_preview_and_send_use_explicit_runtime_paths(tmp_path, monkeypatch):
    paths = RuntimePaths(
        home_dir=str(tmp_path / "profile-a"),
        hh_state_dir=str(tmp_path / "profile-a" / "state"),
        resume_file=str(tmp_path / "profile-a" / "resume.md"),
    )
    state_dirs = []

    async def fake_fill(page, chat_id, text, **kwargs):
        state_dirs.append(kwargs["state_dir"])
        return {"filled": True}

    async def fake_send(page, chat_id, text, **kwargs):
        preview = await kwargs["fill_preview"](page, chat_id, text)
        return bool(preview.get("filled"))

    monkeypatch.setattr(chat_responder, "_chat_fill_and_preview", fake_fill)
    monkeypatch.setattr(chat_responder, "_chat_send_message", fake_send)

    preview = asyncio.run(
        chat_responder.fill_and_preview(
            object(),
            "42",
            "Answer",
            runtime_paths=paths,
        )
    )
    sent = asyncio.run(
        chat_responder.send_message(
            object(),
            "42",
            "Answer",
            runtime_paths=paths,
        )
    )

    assert preview == {"filled": True}
    assert sent is True
    assert state_dirs == [paths.hh_state_dir, paths.hh_state_dir]
