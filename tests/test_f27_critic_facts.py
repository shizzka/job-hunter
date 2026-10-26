"""Fact confirmations refer to the exact user, profile and draft shown, offline."""
import asyncio
from unittest.mock import AsyncMock

import pytest

import candidate_interview
import telegram_access
from tests.test_telegram_menu_routing import menu_bot
from tests.test_telegram_callbacks import _callback

USER = {'user_id': 42, 'role': 'user', 'profile': 'qa'}


def confirmation(bot, action='save'):
    rows = bot._send_text.call_args.kwargs['reply_markup']['inline_keyboard']
    labels = {'save': '✅ Сохранить факт', 'edit': '✏️ Изменить ответ', 'skip': '⏭ Пропустить'}
    return next(button['callback_data'] for row in rows for button in row if button['text'] == labels[action])


async def draft(bot, text):
    await bot._dispatch(42, USER, '/candidate_add_fact', '')
    assert await bot._accept_candidate_interview_input(42, USER, text)


@pytest.mark.parametrize('action', ['save', 'edit', 'skip'])
def test_superseded_fact_button_cannot_change_new_draft(menu_bot, monkeypatch, action):
    bot, _ = menu_bot
    monkeypatch.setattr(telegram_access, 'resolve_user', lambda uid: {**USER, 'user_id': uid})
    monkeypatch.setattr(bot, '_answer_callback_query', AsyncMock())
    async def scenario():
        await draft(bot, 'Fact A')
        old = confirmation(bot, action)
        await draft(bot, 'Fact B')
        before = bot._candidate_state(42, 'qa')
        await bot._handle_callback_query(_callback(old, user_id=42, chat_type='private'))
        assert bot._candidate_state(42, 'qa') == before
        assert candidate_interview.load(bot._profile('qa').home_dir)['facts'] == []
    asyncio.run(scenario())


@pytest.mark.parametrize('action', ['save', 'edit', 'skip'])
def test_current_fact_button_is_single_use(menu_bot, monkeypatch, action):
    bot, _ = menu_bot
    monkeypatch.setattr(telegram_access, 'resolve_user', lambda uid: USER)
    monkeypatch.setattr(bot, '_answer_callback_query', AsyncMock())
    async def scenario():
        await draft(bot, 'Exact confirmed text')
        data = confirmation(bot, action)
        callback = _callback(data, user_id=42, chat_type='private')
        await bot._handle_callback_query(callback)
        after = bot._candidate_state(42, 'qa')
        await bot._handle_callback_query({**callback, 'id': 'repeat'})
        assert bot._candidate_state(42, 'qa') == after
        facts = candidate_interview.load(bot._profile('qa').home_dir)['facts']
        assert len(facts) == (1 if action == 'save' else 0)
        if action == 'save':
            assert facts[0]['text'] == 'Exact confirmed text'
        if action == 'edit':
            assert after['mode'] == 'add'
        if action == 'skip':
            assert after == {}
    asyncio.run(scenario())


@pytest.mark.parametrize('change', ['user', 'profile', 'direction', 'restart', 'identical_draft'])
def test_fact_confirmation_refuses_changed_binding(menu_bot, monkeypatch, change):
    bot, _ = menu_bot
    monkeypatch.setattr(telegram_access, 'resolve_user', lambda uid: {**USER, 'user_id': uid})
    monkeypatch.setattr(bot, '_answer_callback_query', AsyncMock())
    async def scenario():
        await draft(bot, 'Same text')
        data = confirmation(bot)
        if change == 'profile':
            monkeypatch.setattr(bot, '_selected_profile', lambda p: 'other')
        elif change == 'direction':
            monkeypatch.setattr('telegram_app.history.token', lambda home: 'new-direction')
        elif change == 'restart':
            bot.__dict__.pop('_candidate_confirmations', None)
        elif change == 'identical_draft':
            await draft(bot, 'Same text')
        await bot._handle_callback_query(_callback(data, user_id=43 if change == 'user' else 42, chat_type='private'))
        assert candidate_interview.load(bot._profile('qa').home_dir)['facts'] == []
    asyncio.run(scenario())


def test_legacy_unbound_fact_callback_cannot_save_current_draft(menu_bot, monkeypatch):
    from telegram_app import history
    bot, _ = menu_bot
    monkeypatch.setattr(telegram_access, 'resolve_user', lambda uid: USER)
    monkeypatch.setattr(bot, '_answer_callback_query', AsyncMock())
    async def scenario():
        await draft(bot, 'Current fact')
        old = f"nav:{history.token(bot._profile('qa').home_dir)}:candidate_save"
        await bot._handle_callback_query(_callback(old, user_id=42, chat_type='private'))
        assert candidate_interview.load(bot._profile('qa').home_dir)['facts'] == []
    asyncio.run(scenario())
