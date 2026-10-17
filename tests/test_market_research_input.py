import asyncio

import telegram_bot


def test_multiline_titles_after_prompt_start_research(monkeypatch):
    bot = telegram_bot.TelegramBot("qa")
    principal = {"user_id": 42, "role": telegram_bot.ROLE_ADMIN, "profile": "qa"}
    messages, started = [], []
    monkeypatch.setattr(telegram_bot.telegram_access, "resolve_user", lambda user_id: principal)
    monkeypatch.setattr(bot, "_selected_profile", lambda current: "qa")

    async def send_text(chat_id, text, **kwargs):
        messages.append(text)
        return {}

    async def start(chat_id, current, text):
        started.append(text)

    monkeypatch.setattr(bot, "_send_text", send_text)
    monkeypatch.setattr(bot, "_start_market_research", start)

    def message(text):
        return {"message": {"chat": {"id": 42, "type": "private"}, "from": {"id": 42}, "text": text}}

    asyncio.run(bot._handle_update(message("📈 Рынок вакансий")))
    asyncio.run(bot._handle_update(message("IT Recruiter\nQA Lead\nHead of QA")))
    assert started == ["IT Recruiter\nQA Lead\nHead of QA"], messages
