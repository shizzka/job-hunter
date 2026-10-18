"""B4: a client gets a plain «ИИ перегружен», the admin gets the technical details."""
import asyncio

import notifier


def run(monkeypatch, targets, users):
    sent = []
    monkeypatch.setattr(notifier, "capture_delivery_target", lambda: ("token", tuple(targets), ""))
    monkeypatch.setattr(notifier.telegram_access, "list_users", lambda: users)
    monkeypatch.setattr(notifier.config, "NOTIFY_CHAT_ID", 0)

    async def send_to_chats(method, build, *, multipart=False, target=None):
        chats = target[1] if target else tuple(targets)
        for chat in chats:
            sent.append((chat, build(chat)["text"]))
        return True

    monkeypatch.setattr(notifier, "_send_to_chats", send_to_chats)
    asyncio.run(notifier.notify_llm_issue({"title": "QA", "company": "A"},
                                          {"error_kind": "llm_limits_exhausted", "llm_error": "providers_exhausted"}))
    return sent


def test_client_gets_plain_text_and_admin_gets_details(monkeypatch):
    users = [{"user_id": 1, "role": "admin", "profile": "qa"}, {"user_id": 2, "role": "user", "profile": "client_2"}]
    sent = run(monkeypatch, [2], users)
    client = [text for chat, text in sent if chat == 2]
    admin = [text for chat, text in sent if chat == 1]
    assert client and "ИИ сейчас перегружен" in client[0] and "llm-providers.env" not in client[0]
    assert admin and "llm-providers.env" in admin[0] and "Профиль:" in admin[0]


def test_admin_profile_keeps_technical_message(monkeypatch):
    users = [{"user_id": 1, "role": "admin", "profile": "qa"}]
    sent = run(monkeypatch, [1], users)
    assert len(sent) == 1 and "LLM лимиты исчерпаны" in sent[0][1]
