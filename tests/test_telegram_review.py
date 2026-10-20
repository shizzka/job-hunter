import asyncio
from types import SimpleNamespace

import pytest

import directions
import manual_apply_queue
import telegram_bot
from direction_views import DirectionView
from tests.direction_view_fakes import two_directions, make_direction


@pytest.fixture(autouse=True)
def review_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(telegram_bot.TelegramBot, "_profile", lambda self, name: SimpleNamespace(home_dir=str(tmp_path)))


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


def test_review_direction_filter_paging_legacy_and_switch(tmp_path, monkeypatch):
    two_directions(tmp_path)
    bot = telegram_bot.TelegramBot("qa")
    principal = {"user_id": 42, "role": "user", "profile": "qa"}
    items = [{"token": f"qa{i}", "vacancy": {"title": f"QA {i}", "_direction_selection": {"direction": "qa"}},
              "evaluation": {"score": 20}} for i in range(7)]
    items += [
        {"token": "electric", "vacancy": {"title": "Electric", "_direction_selection": {"direction": "electric"}}},
        {"token": "query", "vacancy": {"title": "Old query", "_search_query": "QA Engineer"}},
        {"token": "resume", "vacancy": {"title": "Old resume", "_direction_selection": {"hh_resume_numeric_id": 222}}},
        {"token": "unknown", "vacancy": {"title": "Unknown"}, "evaluation": {"score": 10}},
    ]
    sent, feedback = [], []
    monkeypatch.setattr(bot, "_set_selected_menu", lambda *a: None)
    monkeypatch.setattr(manual_apply_queue, "list_candidates", lambda *a, **kw: items)
    monkeypatch.setattr(manual_apply_queue, "record_feedback", lambda token, *a, **kw: feedback.append(token) or True)
    monkeypatch.setattr(manual_apply_queue, "get_candidate", lambda token, **kw: dict(next(i for i in items if i['token'] == token), status="pending"))

    async def send(chat, text, **kw):
        sent.append((text, kw.get("reply_markup")))
    monkeypatch.setattr(bot, "_send_text", send)
    monkeypatch.setattr(bot, "_edit_text", lambda chat, message, text, **kw: send(chat, text, **kw))
    asyncio.run(bot._show_review_queue(42, principal, profile_name="qa", page=1, sort="new"))
    text, markup = sent[-1]
    assert "На рассмотрении: 8 · QA Engineer · стр. 2/2" in text
    assert "7. QA 6" in text and "8. Old query" in text
    assert "Electric" not in text and "Unknown" not in text
    assert markup['inline_keyboard'][0] == [
        {"text": "7", "callback_data": "rq:o:qa:qa6"}, {"text": "8", "callback_data": "rq:o:qa:query"}]
    assert any(b['callback_data'] == 'rq:u:qa:0:s' for r in markup['inline_keyboard'] for b in r)
    asyncio.run(bot._show_review_queue(42, principal, profile_name="qa", unspecified=True))
    assert "На рассмотрении: 1 · направление не указано" in sent[-1][0]
    assert "Unknown" in sent[-1][0] and "Old query" not in sent[-1][0]
    asyncio.run(bot._review_dismiss_below(42, principal, "qa", 30, confirmed=True))
    assert feedback == [f"qa{i}" for i in range(7)]
    directions.set_active_direction(str(tmp_path), "electric")
    asyncio.run(bot._show_review_queue(42, principal, profile_name="qa", page=1, message_id=5))
    assert "На рассмотрении: 2 · Электромонтажник" in sent[-1][0]
    assert "Electric" in sent[-1][0] and "Old resume" in sent[-1][0] and "QA 0" not in sent[-1][0]
    asyncio.run(bot._open_review_card(42, "qa", "qa0"))
    assert "Выбрано другое направление" in sent[-1][0]


def test_review_legacy_evidence_must_be_unambiguous(tmp_path):
    two_directions(tmp_path)
    view = DirectionView(str(tmp_path))
    assert view.item_direction({"vacancy": {"_search_query": " QA   ENGINEER "}}) == "qa"
    assert view.item_direction({"vacancy": {"hh_resume_hash": "resume_222"}}) == "electric"
    assert view.item_direction({"vacancy": {"_search_query": "QA Engineer", "hh_resume_numeric_id": 222}}) is None
    assert view.item_direction({"vacancy": {"_search_profile": "Москва"}}) is None
    assert view.item_direction({"vacancy": {"_direction_selection": {"direction": "removed"}}}) is None
    value = directions.load_direction(str(tmp_path), "electric")
    value['search_queries'].append("QA Engineer")
    directions.save_direction(str(tmp_path), value)
    assert DirectionView(str(tmp_path)).item_direction({"vacancy": {"_search_query": "QA Engineer"}}) is None


def test_single_direction_review_keeps_old_cards(tmp_path, monkeypatch):
    make_direction(tmp_path)
    directions.set_active_direction(str(tmp_path), "qa")
    view = DirectionView(str(tmp_path))
    items = [{"token": "old", "vacancy": {"title": "Old"}}]
    assert view.review_items(items) == items
    assert not view.review_items(items, unspecified=True)
    assert view.item_direction({"vacancy": {"_search_query": "Unrelated"}}) is None


def test_review_bulk_confirmation_cannot_cross_direction_switch(tmp_path, monkeypatch):
    two_directions(tmp_path)
    bot = telegram_bot.TelegramBot('qa')
    sent, feedback = [], []
    monkeypatch.setattr(manual_apply_queue, 'list_candidates', lambda *a, **kw: [
        {'token': 'qa', 'vacancy': {'_direction_selection': {'direction': 'qa'}}, 'evaluation': {'score': 10}},
        {'token': 'electric', 'vacancy': {'_direction_selection': {'direction': 'electric'}}, 'evaluation': {'score': 10}}])
    monkeypatch.setattr(manual_apply_queue, 'record_feedback', lambda token, *a, **kw: feedback.append(token))

    async def send(chat, text, **kw):
        sent.append((text, kw.get('reply_markup')))
    monkeypatch.setattr(bot, '_send_text', send)
    principal = {'user_id': 42, 'role': 'user', 'profile': 'qa'}
    scope = DirectionView(str(tmp_path)).callback_scope
    asyncio.run(bot._review_dismiss_below(42, principal, 'qa', 30, confirmed=False, expected_scope=scope))
    assert 'Убрать 1 вакансий' in sent[-1][0]
    confirm = sent[-1][1]['inline_keyboard'][0][0]['callback_data']
    assert confirm == f'rq:D:qa:30:{scope}' and len(confirm.encode()) <= 64
    directions.set_active_direction(str(tmp_path), 'electric')
    asyncio.run(bot._review_dismiss_below(42, principal, 'qa', 30, confirmed=True, expected_scope=scope))
    assert not feedback and 'Выбрано другое направление' in sent[-1][0]


@pytest.mark.parametrize('group', ['p', 'u'])
def test_review_page_callbacks_keep_the_separate_group(monkeypatch, group):
    from unittest.mock import AsyncMock
    from tests.test_telegram_callbacks import _configured_bot, _callback
    bot, calls = _configured_bot(monkeypatch)
    show = AsyncMock()
    monkeypatch.setattr(bot, '_show_review_queue', show)
    asyncio.run(bot._handle_callback_query(_callback(f'rq:{group}:qa:2:n')))
    assert show.call_args.kwargs == {
        'profile_name': 'qa', 'page': 2, 'message_id': 7, 'sort': 'new', 'unspecified': group == 'u'}
