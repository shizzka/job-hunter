"""Critic regressions: unresolved queues and newly generated decision cards."""
import asyncio
from unittest.mock import AsyncMock

import pytest

import manual_apply_queue as queue
from telegram_app.callback_handlers import CallbackContext
from telegram_app.callback_handlers.review import handle_review_queue
from tests.test_f27_review import setup, tap


def buttons(markup):
    return [button for row in markup['inline_keyboard'] for button in row]


@pytest.mark.parametrize('orphan', [False, True])
def test_uncertain_cards_keep_safe_navigation_in_both_entry_paths(setup, orphan):
    bot, principal, items = setup
    for item in items:
        queue._store('qa').update(lambda data, token=item['token']: data['items'][token].update(
            status='uncertain', orphan_recovered=orphan))
    before = queue._read_queue('qa')
    asyncio.run(bot._show_review_queue(42, principal, profile_name='qa'))
    first = next(i for i in bot._review_visible_items('qa') if i['vacancy']['title'] in bot.events[-1][1])
    for entry in ['queue', 'open']:
        if entry == 'open':
            asyncio.run(bot._open_review_card(42, 'qa', first['token']))
        controls = buttons(bot.events[-1][2])
        assert {'◀', '▶', '☰ Вся очередь'} <= {b['text'] for b in controls}
        assert not any(b['text'] in {'👍', '👎', '⋯ Ещё'} or
                       str(b.get('callback_data', '')).startswith((
                           'manual_apply:', 'manual_fb:', 'manual_block_company:', 'manual_snooze:',
                           'rq:g:', 'rq:b:', 'rq:s:', 'rq:x:', 'rq:X:'))
                       for b in controls)
        tap(bot, principal, 'v', first['token'])
        assert bot.events[-1][0] == 'edit' and first['vacancy']['title'] not in bot.events[-1][1]
        tap(bot, principal, 'h', first['token'])
        assert bot.events[-1][0] == 'edit'
    tap(bot, principal, 'l', '0')
    assert 'Ждут решения: 3' in bot.events[-1][1]
    assert queue._read_queue('qa') == before


@pytest.mark.parametrize('action', ['g', 'b', 's', 'x', 'X', 'm', 'w'])
def test_uncertain_cannot_use_forged_or_old_mutation_and_more_controls(setup, action):
    bot, principal, items = setup
    token = items[0]['token']
    queue._store('qa').update(lambda data: data['items'][token].update(status='uncertain'))
    before = queue._read_queue('qa')
    for _ in range(2):
        tap(bot, principal, action, token)
        assert 'не подтверждён' in bot.events[-1][1]
    assert queue._read_queue('qa') == before
    assert not queue.company_blacklist.is_blocked('Company', 'qa')


@pytest.mark.parametrize('chat_type,access', [('group', True), ('private', False)])
@pytest.mark.parametrize('action', ['h', 'v', 'l', 'U'])
def test_uncertain_navigation_keeps_private_profile_guards(setup, chat_type, access, action):
    bot, principal, items = setup
    token = items[0]['token'] if action in {'h', 'v'} else '0'
    context = CallbackContext('cb', 42, principal, f'rq:{action}:qa:{token}',
        {'chat': {'type': chat_type}}, 42, 7, lambda name: access and name == 'qa')
    before = queue._read_queue('qa')
    asyncio.run(handle_review_queue(bot, context))
    assert bot.events[-1][0] == 'answer' and 'недоступна' in bot.events[-1][1]
    assert queue._read_queue('qa') == before


@pytest.mark.parametrize('status', ['pending', 'uncertain'])
def test_empty_selected_direction_exposes_accessible_unspecified_group(setup, monkeypatch, status):
    from tests.direction_view_fakes import two_directions
    bot, principal, items = setup
    two_directions(bot.home)
    for item in items:
        queue._store('qa').update(lambda data, token=item['token']: data['items'][token].update(status=status))
    asyncio.run(bot._show_review_queue(42, principal, profile_name='qa'))
    _, text, markup = bot.events[-1]
    assert 'Всё разобрано' not in text and '3' in text and 'направление не указано' in text.lower()
    button = next(b for b in buttons(markup) if b.get('callback_data') == 'rq:U:qa:0:s')
    context = CallbackContext('cb', 42, principal, button['callback_data'],
        {'chat': {'type': 'private'}}, 42, 7, lambda name: name == 'qa')
    asyncio.run(handle_review_queue(bot, context))
    assert all(item['vacancy']['title'] in bot.events[-1][1] for item in items)


def test_empty_tester_direction_never_advertises_unknown_or_foreign_unresolved(setup, monkeypatch):
    import profile_access
    from tests.direction_view_fakes import two_directions
    bot, principal, items = setup
    two_directions(bot.home)
    principal.update(role='tester', direction='qa')
    queue._store('qa').update(lambda data: data['items'][items[0]['token']].update(
        direction='electric', status='uncertain'))
    monkeypatch.setattr(profile_access, 'current_scope', lambda: ('qa-dev', 'qa', '42'))
    asyncio.run(bot._show_review_queue(42, principal, profile_name='qa'))
    _, text, markup = bot.events[-1]
    assert 'Job ' not in text and 'направление не указано' not in text.lower()
    assert not any(b.get('callback_data', '').startswith('rq:U:') for b in buttons(markup))
    for action in ['h', 'v']:
        for item in items:
            bot.events.clear()
            tap(bot, principal, action, item['token'])
            assert not any(e[0] in {'send', 'edit'} for e in bot.events)


def test_uncertain_arrows_are_repeatable_through_real_callback_dispatch(setup, monkeypatch):
    from tests.test_telegram_callbacks import _configured_bot, _callback
    fake, principal, items = setup
    for item in items:
        queue._store('qa').update(lambda data, token=item['token']: data['items'][token].update(status='uncertain'))
    bot, calls = _configured_bot(monkeypatch, principal=principal)
    monkeypatch.setattr(bot, '_profile', fake._profile)
    monkeypatch.setattr(bot, '_edit_text', fake._edit_text)
    before = queue._read_queue('qa')
    for _ in range(2):
        asyncio.run(bot._handle_callback_query(_callback(
            f'rq:v:qa:{items[0]["token"]}', user_id=42, chat_type='private')))
        assert fake.events[-1][0] == 'edit'
        assert 'нужна' in fake.events[-1][1].lower()
    assert len(fake.events) == 2 and queue._read_queue('qa') == before


@pytest.mark.parametrize('prefix', ['manual_apply', 'manual_fb', 'manual_snooze', 'manual_block_company'])
def test_legacy_decision_callbacks_cannot_mutate_uncertain_even_when_repeated(setup, monkeypatch, prefix):
    from tests.test_telegram_callbacks import _configured_bot, _callback
    fake, principal, items = setup
    token = items[0]['token']
    queue._store('qa').update(lambda data: data['items'][token].update(status='uncertain'))
    bot, calls = _configured_bot(monkeypatch, principal=principal)
    monkeypatch.setattr(bot, '_profile', fake._profile)
    monkeypatch.setattr(bot, '_send_text', fake._send_text)
    start = AsyncMock()
    monkeypatch.setattr(bot, '_start_manual_ai_apply', start)
    before = queue._read_queue('qa')
    data = f'{prefix}:qa:{token}' + (':good' if prefix == 'manual_fb' else '')
    for _ in range(2):
        asyncio.run(bot._handle_callback_query(_callback(data, user_id=42, chat_type='private')))
    start.assert_not_awaited()
    assert queue._read_queue('qa') == before
    assert not queue.company_blacklist.is_blocked('Company', 'qa')
    for event in fake.events:
        assert {'◀', '▶', '☰ Вся очередь'} <= {b['text'] for b in buttons(event[2])}


@pytest.mark.parametrize('allow,source', [(True, 'hh'), (False, 'hh'), (True, 'geekjob')])
def test_new_pending_notification_uses_shared_f27_presentation(setup, monkeypatch, allow, source):
    import notifier
    bot, principal, items = setup
    item = dict(items[0], allow_ai_apply=allow, vacancy=dict(items[0]['vacancy'], source=source))
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(notifier, 'send_message_with_markup', sent)
    asyncio.run(notifier.notify_needs_manual(item['vacancy'], 85, 'reason',
        note='Safety condition <check>', review_item=item))
    text = sent.await_args.args[0]
    markup = sent.await_args.kwargs['reply_markup']
    assert '📋 Ждут решения' in text and '85/100 · Очень хорошо подходит' in text
    assert 'Без вашего «Откликнуться» ничего не отправится.' in text
    assert 'Safety condition &lt;check&gt;' in text
    assert markup == queue.build_review_card_markup(item, 'qa')
    assert any(b['text'] == '✅ Откликнуться' for b in buttons(markup)) == (allow and source == 'hh')


@pytest.mark.parametrize('mode,yellow', [('confirm', False), ('preview', False), ('confirm', True), ('preview', True)])
def test_background_search_emits_real_f27_card_without_submitting(setup, monkeypatch, mode, yellow):
    import agent
    import config
    import notifier
    from types import SimpleNamespace
    from tests.test_observability import configure_synthetic_search
    bot, principal, _ = setup
    create = queue.create_candidate
    blocked = queue.company_blacklist.is_blocked
    configure_synthetic_search(monkeypatch, bot.home)
    monkeypatch.setattr(queue, 'create_candidate', create)
    monkeypatch.setattr(queue.company_blacklist, 'is_blocked', blocked)
    monkeypatch.setattr(agent.profile_mod, 'active', lambda: SimpleNamespace(name='qa', home_dir=bot.home))
    monkeypatch.setattr(config, 'HH_APPLICATION_MODE', mode)
    monkeypatch.setattr(config, 'ACTIVE_DIRECTION', {}, raising=False)
    vacancy = {'id': 'new', 'source': 'hh', 'title': 'New background job', 'company': 'Company',
               'url': 'https://hh.ru/vacancy/new', 'workFormats': ['REMOTE']}
    monkeypatch.setattr(agent.search_pipeline, 'collect_all', AsyncMock(return_value=[vacancy]))
    monkeypatch.setattr(agent.search_pipeline.filters, 'check_vacancy', lambda v: None)
    monkeypatch.setattr(agent, 'evaluate_vacancy', AsyncMock(return_value={
        'score': 71, 'should_apply': not yellow, 'reason': 'Matching reason', 'red_flags': []}))
    monkeypatch.setattr(agent, 'is_manual_review_candidate', lambda evaluation: yellow)
    dispatch = AsyncMock()
    monkeypatch.setattr(agent.apply_orchestrator, 'dispatch_apply', dispatch)
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(notifier, 'send_message_with_markup', sent)
    monkeypatch.setattr(agent, 'notify_needs_manual', notifier.notify_needs_manual)
    result = asyncio.run(agent.do_search())
    dispatch.assert_not_awaited()
    assert result['source_stats']['hh']['manual'] == 1
    sent.assert_awaited_once()
    text = sent.await_args.args[0]
    markup = sent.await_args.kwargs['reply_markup']
    assert '📋 Ждут решения' in text and '71/100 · Скорее подходит' in text
    assert 'Без вашего «Откликнуться» ничего не отправится.' in text
    assert ['◀', '👍', '👎', '▶'] in [[b['text'] for b in row] for row in markup['inline_keyboard']]
    assert any(b['text'] == '✅ Откликнуться' for b in buttons(markup)) == (mode == 'confirm')
    item = next(i for i in queue.list_candidates('qa') if i['vacancy']['id'] == 'new')
    assert item['status'] == 'pending' and item['allow_ai_apply'] == (mode == 'confirm')
