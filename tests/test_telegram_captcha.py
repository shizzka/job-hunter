import asyncio

import captcha_bridge
import telegram_bot


def test_user_can_answer_only_their_profile_captcha(monkeypatch):
    bot = telegram_bot.TelegramBot("qa")
    principal = {
        "user_id": 42,
        "role": telegram_bot.ROLE_USER,
        "profile": "client_marina",
    }
    captured = {}
    messages = []

    monkeypatch.setattr(telegram_bot.telegram_access, "resolve_user", lambda user_id: principal)
    monkeypatch.setattr(bot, "_selected_profile", lambda current_principal: "client_marina")
    monkeypatch.setattr(
        captcha_bridge,
        "peek_pending",
        lambda profile_name="": {"id": "captcha-1", "profile_name": profile_name},
    )
    monkeypatch.setattr(
        captcha_bridge,
        "write_response",
        lambda request_id, answer, profile_name="": captured.update(
            request_id=request_id,
            answer=answer,
            profile_name=profile_name,
        ),
    )

    async def false_handler(*args, **kwargs):
        return False

    async def send_text(chat_id, text, **kwargs):
        messages.append((chat_id, text))
        return {}

    monkeypatch.setattr(bot, "_maybe_accept_hh_auth_response", false_handler)
    monkeypatch.setattr(bot, "_accept_form_answer", false_handler)
    monkeypatch.setattr(bot, "_accept_search_query_input", false_handler)
    monkeypatch.setattr(bot, "_send_text", send_text)

    asyncio.run(bot._handle_update({
        "message": {
            "chat": {"id": 42, "type": "private"},
            "from": {"id": 42},
            "text": "абвгд",
        },
    }))

    assert captured == {
        "request_id": "captcha-1",
        "answer": "абвгд",
        "profile_name": "client_marina",
    }
    assert messages == [(42, "✅ Принял текст captcha. Вставляю его в форму hh.ru…")]
