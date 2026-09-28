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

    assert messages[0][1] == "📋 На рассмотрении: 1. Показываю последние 1."
    assert "QA Engineer" in messages[1][1]
    buttons = [button for row in messages[1][2]["inline_keyboard"] for button in row]
    assert any(button["text"] == "Откликнуться с ИИ" for button in buttons)
    assert any(button["text"] == "⏰ Через сутки" for button in buttons)
