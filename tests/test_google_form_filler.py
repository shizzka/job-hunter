import google_form_filler as gforms


def test_extract_google_form_urls_from_text_and_hh_redirect():
    text = "Заполните форму https://forms.gle/abc123). Спасибо"
    links = [
        {"href": "https://hh.ru/away?to=https%3A%2F%2Fdocs.google.com%2Fforms%2Fd%2Fe%2Fformid%2Fviewform%3Fusp%3Dsf_link", "text": "форма"}
    ]

    urls = gforms.extract_google_form_urls(text, links)

    assert urls[0] == "https://forms.gle/abc123"
    assert urls[1].startswith("https://docs.google.com/forms/d/e/formid/viewform")


def test_google_form_callback_data_roundtrip():
    preview = gforms.google_form_preview_callback_data("qa", "5416682595", "14513855732")
    submit = gforms.google_form_submit_callback_data("qa", "abcdef123456")

    assert preview == "gform_preview:qa:5416682595:14513855732"
    assert gforms.parse_google_form_preview_callback_data(preview) == ("qa", "5416682595", "14513855732")
    assert submit == "gform_submit:qa:abcdef123456"
    assert gforms.parse_google_form_submit_callback_data(submit) == ("qa", "abcdef123456")
    assert gforms.parse_google_form_submit_callback_data("gform_submit:qa:not-token") == ("", "")


def test_submit_saved_preview_rejects_unfilled_preview(monkeypatch):
    monkeypatch.setattr(
        gforms,
        "_load_state",
        lambda: {
            "items": {
                "abcdef123456": {
                    "status": "preview",
                    "form_url": "https://forms.gle/abc123",
                    "fill_result": {"filled": [], "skipped": [{"index": 0}]},
                }
            }
        },
    )

    class FakeHHClient:
        _page = None

    import asyncio

    result = asyncio.run(gforms.submit_saved_preview(FakeHHClient(), "abcdef123456"))

    assert result["ok"] is False
    assert result["message"] == "google form preview is not ready for submit"


def test_google_form_preview_markup_contains_submit_button():
    markup = gforms.build_google_form_preview_markup("qa", "abcdef123456", "https://forms.gle/abc123")
    buttons = [button for row in markup["inline_keyboard"] for button in row]

    assert any(button.get("url") == "https://forms.gle/abc123" for button in buttons)
    assert any(button.get("callback_data") == "gform_submit:qa:abcdef123456" for button in buttons)


def test_google_form_login_required_detection_handles_dutch_google_message():
    text = "Log in om door te gaan\nJe moet zijn ingelogd om dit formulier in te vullen."

    assert gforms._looks_like_google_form_login_required(text) is True


def test_google_form_button_text_helpers_handle_ru_en_nl():
    assert gforms._is_google_form_next_button_text("Далее")
    assert gforms._is_google_form_next_button_text("Next")
    assert gforms._is_google_form_next_button_text("Volgende")
    assert not gforms._is_google_form_next_button_text("Назад")

    assert gforms._is_google_form_submit_button_text("Отправить")
    assert gforms._is_google_form_submit_button_text("Submit")
    assert gforms._is_google_form_submit_button_text("Verzenden")
    assert not gforms._is_google_form_submit_button_text("Далее")

    assert gforms._is_google_form_email_consent_text("Указать в моем ответе адрес электронной почты test@example.com")
    assert gforms._is_google_form_email_consent_text("Record test@example.com as the email to be included with my response")
    assert not gforms._is_google_form_email_consent_text("Смартфон на базе Android")


def test_google_form_submit_success_detection_handles_common_languages():
    assert gforms._looks_like_google_form_submit_success("Ваш ответ записан.")
    assert gforms._looks_like_google_form_submit_success("Your response has been recorded.")
    assert gforms._looks_like_google_form_submit_success("Submit another response")
    assert gforms._looks_like_google_form_submit_success("Отправить ещё один ответ")
    assert gforms._looks_like_google_form_submit_success("Je antwoord is geregistreerd")
    assert not gforms._looks_like_google_form_submit_success("Это обязательный вопрос.")


def test_reindex_page_questions_preserves_page_metadata():
    questions = [
        {"index": 0, "dom_index": 3, "question": "ФИО"},
        {"index": 1, "dom_index": 4, "question": "Опыт"},
    ]

    reindexed = gforms._reindex_page_questions(questions, page_index=2, start_index=7)

    assert [q["index"] for q in reindexed] == [7, 8]
    assert [q["page_question_index"] for q in reindexed] == [0, 1]
    assert {q["page_index"] for q in reindexed} == {2}
    assert reindexed[0]["dom_index"] == 3


def test_asks_for_telegram_does_not_match_chatgpt():
    assert gforms._asks_for_telegram("ник в ТГ для связи")
    assert gforms._asks_for_telegram("Telegram username")
    assert not gforms._asks_for_telegram("Для каких QA-задач вы использовали ChatGPT / Claude?")


def test_prepare_form_answers_moves_checkbox_answer_list_to_options():
    questions = [{"index": 6, "type": "checkbox", "options": ["Web", "SaaS"]}]
    answers = [{"index": 6, "answer": ["Web", "SaaS"], "skip": False}]

    prepared = gforms._prepare_form_answers(questions, answers)

    assert prepared[0]["options"] == ["Web", "SaaS"]
    assert prepared[0]["answer"] == ["Web", "SaaS"]


def test_avoid_bare_other_options_replaces_required_other_checkbox():
    questions = [
        {
            "index": 7,
            "type": "checkbox",
            "required": True,
            "question": "Какими инструментами для работы с мобильными приложениями вы пользовались лично?",
            "options": [
                "TestFlight (для установки и тестирования бета-версий на iOS)",
                "Тестировал только на реальных физических смартфонах",
                "Другое:",
            ],
        }
    ]
    answers = [{"index": 7, "answer": "Другое:", "options": ["Другое:"], "skip": False}]

    prepared = gforms._prepare_form_answers(questions, answers)

    assert prepared[0]["options"] == ["Тестировал только на реальных физических смартфонах"]
    assert prepared[0]["answer"] == "Тестировал только на реальных физических смартфонах"


def test_avoid_bare_other_options_removes_other_when_other_answers_exist():
    questions = [{"index": 1, "type": "checkbox", "options": ["Web", "Другое:"]}]
    answers = [{"index": 1, "answer": "Web", "options": ["Web", "Другое:"], "skip": False}]

    prepared = gforms._prepare_form_answers(questions, answers)

    assert prepared[0]["options"] == ["Web"]


def test_avoid_bare_other_options_drops_free_text_that_is_not_an_option():
    questions = [
        {
            "index": 7,
            "type": "checkbox",
            "required": True,
            "question": "Какими инструментами для работы с мобильными приложениями вы пользовались лично?",
            "options": ["TestFlight", "Тестировал только на реальных физических смартфонах", "Другое:"],
        }
    ]
    answers = [{"index": 7, "answer": ["Другое:", "Нет опыта"], "options": ["Другое:", "Нет опыта"], "skip": False}]

    prepared = gforms._prepare_form_answers(questions, answers)

    assert prepared[0]["options"] == ["Тестировал только на реальных физических смартфонах"]


def test_google_form_preview_status_requires_submit_page():
    questions = [{"index": 0, "question": "ФИО", "required": True}]
    fill_result = {"filled": [{"index": 0}], "skipped": []}

    ok, message = gforms._google_form_preview_status(questions, fill_result, reached_submit=False)

    assert ok is False
    assert "submit page" in message


def test_google_form_preview_status_fails_when_required_field_skipped():
    questions = [
        {"index": 0, "question": "ФИО", "required": True},
        {"index": 1, "question": "Комментарий", "required": False},
    ]
    fill_result = {"filled": [{"index": 1}], "skipped": [{"index": 0}]}

    ok, message = gforms._google_form_preview_status(questions, fill_result)

    assert ok is False
    assert "required" in message


def test_google_form_preview_status_fails_when_nothing_was_filled():
    questions = [{"index": 0, "question": "Phone"}, {"index": 1, "question": "OS"}]
    fill_result = {"filled": [], "skipped": [{"index": 0}, {"index": 1}]}

    ok, message = gforms._google_form_preview_status(questions, fill_result)

    assert ok is False
    assert "no fields" in message


def test_google_form_preview_status_ok_when_at_least_one_field_filled():
    questions = [{"index": 0, "question": "Phone"}, {"index": 1, "question": "OS"}]
    fill_result = {"filled": [{"index": 0}], "skipped": [{"index": 1}]}

    ok, message = gforms._google_form_preview_status(questions, fill_result)

    assert ok is True
    assert message == "preview"


def test_reuse_cached_answers_for_same_form(monkeypatch):
    questions = [{"index": 0, "question": "Ваше ФИО"}]
    answers = [{"index": 0, "answer": "Евгений", "skip": False}]
    monkeypatch.setattr(
        gforms,
        "_load_state",
        lambda: {
            "items": {
                "old": {
                    "form_url": "https://forms.gle/abc123",
                    "created_at": 1,
                    "questions": questions,
                    "answers": answers,
                    "fill_result": {"filled": [{"index": 0}]},
                }
            }
        },
    )

    assert gforms._reuse_cached_answers("https://forms.gle/abc123", questions) == answers
    assert gforms._reuse_cached_answers("https://forms.gle/other", questions) == []


def test_contact_block_contains_candidate_contacts(monkeypatch):
    from prompt_blocks import build_contact_block

    monkeypatch.setenv("CANDIDATE_EMAIL", "qa@example.com")
    monkeypatch.setenv("CANDIDATE_PHONE", "+79990000000")
    monkeypatch.setenv("CANDIDATE_TELEGRAM", "@qa_candidate")
    monkeypatch.setenv(
        "CANDIDATE_RESUME_URL",
        "https://hh.ru/resume/97cc3b12ff0f83131e0039ed1f417945634133",
    )

    block = build_contact_block()

    assert "qa@example.com" in block
    assert "+79990000000" in block
    assert "@qa_candidate" in block
    assert "https://hh.ru/resume/97cc3b12ff0f83131e0039ed1f417945634133" in block
    assert "Email для связи" in block
    assert "Телефон для связи" in block
    assert "Telegram для связи" in block
    assert "Ссылка на резюме" in block


def test_contact_overrides_replace_llm_placeholders(monkeypatch):
    from google_form_filler import _apply_contact_overrides

    monkeypatch.setenv("CANDIDATE_EMAIL", "qa@example.com")
    monkeypatch.setenv("CANDIDATE_PHONE", "+79990000000")
    monkeypatch.setenv("CANDIDATE_TELEGRAM", "@qa_candidate")
    monkeypatch.setenv(
        "CANDIDATE_RESUME_URL",
        "https://hh.ru/resume/97cc3b12ff0f83131e0039ed1f417945634133",
    )
    questions = [
        {"index": 1, "question": "Укажите ваш ТГ для связи"},
        {"index": 2, "question": "Продублируйте ваше резюме (ссылкой)"},
        {"index": 3, "question": "Электронная почта"},
        {"index": 4, "question": "Мобильный телефон"},
        {"index": 5, "question": "Опыт"},
    ]
    answers = [
        {"index": 1, "answer": "@evgeny_qa", "skip": False},
        {"index": 2, "answer": "https://resume.link/evgeny_qa", "skip": False},
        {"index": 3, "answer": "не указана", "skip": False},
        {"index": 4, "answer": "не указан", "skip": False},
        {"index": 5, "answer": "ok", "skip": False},
    ]

    patched = _apply_contact_overrides(questions, answers)
    by_index = {item["index"]: item for item in patched}

    assert by_index[1]["answer"] == "@qa_candidate"
    assert by_index[2]["answer"] == "https://hh.ru/resume/97cc3b12ff0f83131e0039ed1f417945634133"
    assert by_index[3]["answer"] == "qa@example.com"
    assert by_index[4]["answer"] == "+79990000000"
    assert by_index[5]["answer"] == "ok"


def test_required_text_placeholder_is_replaced_with_fallback():
    questions = [
        {"index": 2, "question": "Электронная почта", "type": "text", "required": True},
    ]
    answers = [{"index": 2, "answer": "не указана", "skip": False}]

    patched = gforms._apply_required_overrides(questions, answers)

    assert patched[0]["index"] == 2
    assert patched[0]["answer"] == "Готов предоставить почту в чате hh.ru"
    assert patched[0]["source"] == "required_fallback"


def test_required_text_skip_is_replaced_with_fallback():
    questions = [
        {"index": 7, "question": "Почему вам интересна эта роль?", "type": "text", "required": True},
    ]
    answers = [{"index": 7, "answer": "skip", "skip": True}]

    patched = gforms._apply_required_overrides(questions, answers)

    assert patched[0]["index"] == 7
    assert patched[0]["skip"] is False
    assert patched[0]["source"] == "required_fallback"
    assert patched[0]["answer"]


def test_required_choice_skip_uses_safe_option():
    questions = [
        {"index": 8, "question": "Согласны пройти тестовое?", "type": "radio", "required": True, "options": ["Нет", "Да"]},
    ]
    answers = [{"index": 8, "answer": "skip", "skip": True}]

    patched = gforms._apply_required_overrides(questions, answers)

    assert patched[0]["skip"] is False
    assert patched[0]["options"] == ["Да"]
    assert patched[0]["source"] == "required_fallback"


def test_optional_skip_stays_skip():
    questions = [
        {"index": 9, "question": "Необязательный комментарий", "type": "text", "required": False},
    ]
    answers = [{"index": 9, "answer": "skip", "skip": True}]

    assert gforms._apply_required_overrides(questions, answers) == answers


def test_form_info_block_is_not_treated_as_question():
    text = (
        "Благодарим за заполнение анкеты!\n\n"
        "Мы внимательно рассматриваем каждую анкету и обычно возвращаемся с обратной связью.\n\n"
        "Если вы не получили ответ в течение 2 недель, это будет означать, что мы не готовы продолжить.\n\n"
        "Спасибо за уделенное время!"
    )

    assert gforms._looks_like_form_info_block(text) is True
    assert gforms._looks_like_form_info_block("Почему вам интересна именно эта роль?") is False


def test_google_form_url_match_uses_form_id():
    direct = "https://docs.google.com/forms/d/e/1FAIpQLScuP_MG8_dH0fMQ5cyOdv94spgyk7DFoYMAjpn1sgD4phh4hA/viewform?usp=send_form"
    clean = "https://docs.google.com/forms/d/e/1FAIpQLScuP_MG8_dH0fMQ5cyOdv94spgyk7DFoYMAjpn1sgD4phh4hA/viewform"

    assert gforms._same_google_form_url(direct, clean) is True


def test_cached_answer_quality_prefers_non_fallback_answers():
    good = [{"index": 1, "answer": "real", "skip": False}]
    fallback = [{"index": 1, "answer": "generic", "skip": False, "source": "required_fallback"}]

    assert gforms._cached_answer_quality(good) > gforms._cached_answer_quality(fallback)
