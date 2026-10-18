import asyncio

import manual_apply_queue
import telegram_bot


def test_review_screen_sends_profile_queue_with_actions(monkeypatch):
    bot = telegram_bot.TelegramBot("qa")
    principal = {"user_id": 42, "role": telegram_bot.ROLE_USER, "profile": "client_42"}
    item = {
        "token": "token-1",
        "allow_ai_apply": True,
        "vacancy": {
            "title": "QA Engineer",
            "company": "Acme",
            "source": "hh",
            "url": "https://hh.ru/vacancy/1",
        },
        "evaluation": {"score": 77, "reason": "Хорошее совпадение"},
    }
    messages = []

    monkeypatch.setattr(bot, "_set_selected_menu", lambda *args: None)
    monkeypatch.setattr(bot, "_menu_reply_markup", lambda *args, **kwargs: {"menu": "review"})
    monkeypatch.setattr(manual_apply_queue, "list_candidates", lambda *args, **kwargs: [item])

    async def send_text(chat_id, text, *, reply_markup=None):
        messages.append((chat_id, text, reply_markup))
        return {}

    monkeypatch.setattr(bot, "_send_text", send_text)
    asyncio.run(bot._show_review_queue(42, principal, profile_name="client_42"))

    # One compact list message; a number opens the full card.
    assert len(messages) == 1 and messages[0][1].startswith("📋 На рассмотрении: 1")
    assert "1. QA Engineer" in messages[0][1] and "Acme · 📊 77" in messages[0][1] and "<b>" not in messages[0][1]
    assert messages[0][2]["inline_keyboard"][0][0] == {"text": "1", "callback_data": "rq:o:client_42:token-1"}
    monkeypatch.setattr(manual_apply_queue, "get_candidate", lambda token, profile_name=None: dict(item, status="pending"))
    asyncio.run(bot._open_review_card(42, "client_42", "token-1"))
    assert "QA Engineer" in messages[1][1]
    buttons = [button for row in messages[1][2]["inline_keyboard"] for button in row]
    assert any(button["text"] == "✅ Откликнуться с ИИ" for button in buttons)
    assert any(button["text"] == "⏰ Через сутки" for button in buttons)
    assert len(messages[1][2]["inline_keyboard"]) == 3


def test_review_list_pages_by_six(monkeypatch):
    bot = telegram_bot.TelegramBot("qa")
    principal = {"user_id": 42, "role": telegram_bot.ROLE_USER, "profile": "client_42"}
    items = [{"token": f"t{i}", "vacancy": {"title": f"V{i}", "company": "C"}, "evaluation": {"score": i}}
             for i in range(1, 15)]
    messages = []
    monkeypatch.setattr(bot, "_set_selected_menu", lambda *args: None)
    monkeypatch.setattr(manual_apply_queue, "list_candidates", lambda *args, **kwargs: items)

    async def send_text(chat_id, text, *, reply_markup=None):
        messages.append((text, reply_markup))

    async def edit_text(chat_id, message_id, text, *, reply_markup=None):
        messages.append((text, reply_markup))

    monkeypatch.setattr(bot, "_send_text", send_text)
    monkeypatch.setattr(bot, "_edit_text", edit_text)
    asyncio.run(bot._show_review_queue(42, principal, profile_name="client_42", sort="new"))
    asyncio.run(bot._show_review_queue(42, principal, profile_name="client_42", page=2, message_id=5, sort="new"))
    assert "стр. 1/3" in messages[0][0] and "6. V6" in messages[0][0] and "7. V7" not in messages[0][0]
    nav = messages[0][1]["inline_keyboard"][-3]
    assert [b["callback_data"] for b in nav] == ["rq:p:client_42:2:n", "rq:p:client_42:0:n", "rq:p:client_42:1:n"]
    assert "стр. 3/3" in messages[1][0] and "13. V13" in messages[1][0] and "14. V14" in messages[1][0]


def test_review_list_puts_best_first_and_offers_bulk_dismiss(monkeypatch):
    bot = telegram_bot.TelegramBot("qa")
    principal = {"user_id": 42, "role": telegram_bot.ROLE_USER, "profile": "client_42"}
    items = [{"token": f"t{i}", "vacancy": {"title": f"V{i}"}, "evaluation": {"score": score}}
             for i, score in enumerate([30, 80, None, 55, 45], start=1)]
    messages, feedback = [], []
    monkeypatch.setattr(bot, "_set_selected_menu", lambda *args: None)
    monkeypatch.setattr(manual_apply_queue, "list_candidates", lambda *args, **kwargs: list(items))
    monkeypatch.setattr(manual_apply_queue, "record_feedback",
                        lambda token, value, **kwargs: feedback.append((token, value)) or {"token": token})

    async def send_text(chat_id, text, *, reply_markup=None):
        messages.append((text, reply_markup))

    monkeypatch.setattr(bot, "_send_text", send_text)
    asyncio.run(bot._show_review_queue(42, principal, profile_name="client_42"))
    text, markup = messages[0]
    order = [line.split(". ", 1)[1].split("\n")[0] for line in text.splitlines() if line[:1].isdigit()]
    assert order == ["V2", "V4", "V5", "V1", "V3"]  # 80, 55, 45, 30, unscored last
    rows = markup["inline_keyboard"]
    assert rows[-2] == [{"text": "🆕 Сначала новые", "callback_data": "rq:p:client_42:0:n"}]
    assert [b["callback_data"] for b in rows[-1]] == ["rq:d:client_42:40", "rq:d:client_42:50", "rq:d:client_42:60"]

    asyncio.run(bot._review_dismiss_below(42, principal, "client_42", 50, confirmed=False))
    assert "Убрать 2 вакансий с баллом ниже 50" in messages[-1][0] and feedback == []
    asyncio.run(bot._review_dismiss_below(42, principal, "client_42", 50, confirmed=True))
    assert sorted(feedback) == [("t1", "bad"), ("t5", "bad")]
