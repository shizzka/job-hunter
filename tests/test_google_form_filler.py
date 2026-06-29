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


def test_google_form_preview_markup_contains_submit_button():
    markup = gforms.build_google_form_preview_markup("qa", "abcdef123456", "https://forms.gle/abc123")
    buttons = [button for row in markup["inline_keyboard"] for button in row]

    assert any(button.get("url") == "https://forms.gle/abc123" for button in buttons)
    assert any(button.get("callback_data") == "gform_submit:qa:abcdef123456" for button in buttons)


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

    monkeypatch.setenv("CANDIDATE_TELEGRAM", "@qa_candidate")
    monkeypatch.setenv(
        "CANDIDATE_RESUME_URL",
        "https://hh.ru/resume/97cc3b12ff0f83131e0039ed1f417945634133",
    )

    block = build_contact_block()

    assert "@qa_candidate" in block
    assert "https://hh.ru/resume/97cc3b12ff0f83131e0039ed1f417945634133" in block
    assert "Telegram для связи" in block
    assert "Ссылка на резюме" in block


def test_contact_overrides_replace_llm_placeholders(monkeypatch):
    from google_form_filler import _apply_contact_overrides

    monkeypatch.setenv("CANDIDATE_TELEGRAM", "@qa_candidate")
    monkeypatch.setenv(
        "CANDIDATE_RESUME_URL",
        "https://hh.ru/resume/97cc3b12ff0f83131e0039ed1f417945634133",
    )
    questions = [
        {"index": 1, "question": "Укажите ваш ТГ для связи"},
        {"index": 2, "question": "Продублируйте ваше резюме (ссылкой)"},
        {"index": 3, "question": "Опыт"},
    ]
    answers = [
        {"index": 1, "answer": "@evgeny_qa", "skip": False},
        {"index": 2, "answer": "https://resume.link/evgeny_qa", "skip": False},
        {"index": 3, "answer": "ok", "skip": False},
    ]

    patched = _apply_contact_overrides(questions, answers)
    by_index = {item["index"]: item for item in patched}

    assert by_index[1]["answer"] == "@qa_candidate"
    assert by_index[2]["answer"] == "https://hh.ru/resume/97cc3b12ff0f83131e0039ed1f417945634133"
    assert by_index[3]["answer"] == "ok"


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
