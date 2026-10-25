"""F27 pending decisions use synthetic state and temporary profile queues only."""
import asyncio
from types import SimpleNamespace

import pytest

import manual_apply_queue as queue
from telegram_app.review import TelegramReview
from telegram_app.callback_handlers import CallbackContext
from telegram_app.callback_handlers.review import handle_review_queue


class Bot(TelegramReview):
    def __init__(self, home):
        self.home, self.events, self.state = str(home), [], {}
    def _profile(self, name):
        return SimpleNamespace(home_dir=self.home)
    def _profile_names(self):
        return ['qa', 'other']
    def _set_selected_menu(self, *args):
        pass
    def _update_state(self, mutate):
        mutate(self.state)
        return self.state
    def _load_state(self):
        return self.state
    def _menu_reply_markup(self, *args, **kwargs):
        return {}
    async def _send_text(self, chat, text, **kwargs):
        self.events.append(('send', text, kwargs.get('reply_markup')))
        return {'message_id': 7}
    async def _edit_text(self, chat, message, text, **kwargs):
        self.events.append(('edit', text, kwargs.get('reply_markup')))
    async def _answer_callback_query(self, callback, text='', **kwargs):
        self.events.append(('answer', text, kwargs))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(queue, '_queue_path', lambda profile_name=None: tmp_path / str(profile_name) / 'queue.json')
    monkeypatch.setattr(queue.company_blacklist, '_path', lambda profile_name=None: tmp_path / str(profile_name) / 'blacklist.json')
    bot = Bot(tmp_path / 'qa')
    principal = {'user_id': 42, 'role': 'user', 'profile': 'qa'}
    items = [queue.create_candidate({'id': str(i), 'title': f'Job {i}', 'company': 'Company',
             'url': f'https://hh.ru/vacancy/{i}'}, {'score': score}, profile_name='qa')
             for i, score in enumerate([85, 71, 45])]
    return bot, principal, items


def tap(bot, principal, action, token, *, access=True):
    context = CallbackContext('cb', 42, principal, f'rq:{action}:qa:{token}',
        {'message_id': 7, 'chat': {'type': 'private'}}, 42, 7, lambda name: access and name == 'qa')
    asyncio.run(handle_review_queue(bot, context))


def test_review_opens_first_card_with_exact_primary_rows(setup):
    bot, principal, items = setup
    asyncio.run(bot._show_review_queue(42, principal, profile_name='qa'))
    _, text, markup = bot.events[-1]
    assert text.startswith('📋 Ждут решения · 1 из 3')
    assert '85/100 · Очень хорошо подходит' in text
    assert 'Без вашего «Откликнуться» ничего не отправится.' in text
    assert [[b['text'] for b in row] for row in markup['inline_keyboard']] == [
        ['✅ Откликнуться'], ['◀', '👍', '👎', '▶'], ['❓ Почему', '⋯ Ещё']]


def test_arrows_edit_and_good_consumes_without_apply_and_hint_once(setup):
    bot, principal, items = setup
    asyncio.run(bot._show_review_queue(42, principal, profile_name='qa'))
    tap(bot, principal, 'v', items[0]['token'])
    assert bot.events[-1][0] == 'edit' and '2 из 3' in bot.events[-1][1]
    tap(bot, principal, 'g', items[1]['token'])
    assert queue.get_candidate(items[1]['token'], profile_name='qa')['status'] == 'dismissed'
    assert any(e[1] == 'Запомнил: вакансия хорошая. Отклик не отправлен.' for e in bot.events)
    assert '👍 — хорошая, но откликаться не буду; 👎 — мимо.' in bot.events[-1][1]
    before = (queue._queue_path('qa').with_name(queue.FEEDBACK_LOG)).read_text()
    tap(bot, principal, 'g', items[1]['token'])
    assert (queue._queue_path('qa').with_name(queue.FEEDBACK_LOG)).read_text() == before
    assert 'уже' in next(e[1] for e in reversed(bot.events) if e[0] == 'answer').lower()


def test_more_block_confirmation_cancel_then_confirm_and_stale(setup):
    bot, principal, items = setup
    token = items[0]['token']
    tap(bot, principal, 'm', token)
    assert bot.events[-1][0] == 'edit'
    assert [row[0]['text'] for row in bot.events[-1][2]['inline_keyboard']] == [
        '🔗 Открыть вакансию', '⏰ Отложить на сутки', '☰ Вся очередь', '🚫 Заблокировать компанию', '◀ Назад']
    tap(bot, principal, 'x', token)
    assert not queue.company_blacklist.is_blocked('Company', 'qa')
    tap(bot, principal, 'c', token)
    assert not queue.company_blacklist.is_blocked('Company', 'qa')
    tap(bot, principal, 'X', token)
    assert not queue.company_blacklist.is_blocked('Company', 'qa')
    tap(bot, principal, 'x', token)
    tap(bot, principal, 'X', token)
    assert queue.company_blacklist.is_blocked('Company', 'qa')
    tap(bot, principal, 'X', token)
    assert 'уже' in bot.events[-1][1].lower()


def test_access_refusal_no_queue_mutation(setup):
    bot, principal, items = setup
    tap(bot, principal, 'g', items[0]['token'], access=False)
    assert queue.get_candidate(items[0]['token'], profile_name='qa')['status'] == 'pending'
    assert 'недоступ' in bot.events[-1][1].lower()


def test_bad_and_snooze_advance_then_empty(setup):
    bot, principal, items = setup
    asyncio.run(bot._show_review_queue(42, principal, profile_name='qa'))
    tap(bot, principal, 'b', items[0]['token'])
    assert '2 из 3' in bot.events[-1][1]
    tap(bot, principal, 's', items[1]['token'])
    tap(bot, principal, 'b', items[2]['token'])
    assert bot.events[-1][1] == '✅ Всё разобрано. Сейчас от вас ничего не требуется.'
    assert bot.events[-1][2]['inline_keyboard'][0][0]['text'] == '🔎 Найти вакансии'


def test_full_sent_cover_is_owner_bound_and_not_truncated(setup):
    bot, principal, items = setup
    token = items[0]['token']
    store = queue._store('qa')
    item = queue.claim_candidate(token, store=store)
    cover = '<My exact cover>\n' + 'Текст письма. ' * 450
    assert not queue.save_sent_cover(token, 'wrong-owner', cover, store=store)
    assert queue.save_sent_cover(token, item['owner'], cover, store=store)
    queue.finish_candidate(token, item['owner'], 'applied', store=store)
    tap(bot, principal, 'e', token)
    assert ''.join(event[1] for event in bot.events if event[0] == 'send') == cover
    assert not queue.save_sent_cover(token, item['owner'], 'overwrite', store=store)


@pytest.mark.parametrize('score,label', [(85,'Очень хорошо подходит'), (70,'Скорее подходит'),
    (50,'Есть совпадения'), (49,'Подходит частично — решать вам')])
def test_score_bands(score, label):
    assert label in TelegramReview._review_item_text({'evaluation': {'score': score}}, '•')


def test_confirmed_success_shows_receipt_then_next_snapshot_position(setup):
    bot, principal, items = setup
    asyncio.run(bot._show_review_queue(42, principal, profile_name='qa'))
    token = items[0]['token']
    store = queue._store('qa')
    claim = queue.claim_candidate(token, store=store)
    queue.save_sent_cover(token, claim['owner'], 'Полное письмо', store=store)
    queue.finish_candidate(token, claim['owner'], 'applied', store=store)
    assert asyncio.run(bot._show_manual_apply_success(42, principal, 'qa', token))
    receipt = bot.events[-2]
    assert receipt[1].startswith('📨 Отклик отправлен')
    assert receipt[2]['inline_keyboard'][0][0] == {
        'text': '📨 Открыть отклик', 'callback_data': f'rq:e:qa:{token}'}
    assert '2 из 3' in bot.events[-1][1]
    for action in ['g', 'b', 's', 'X', 'm', 'w', 'c', 'h', 'v']:
        tap(bot, principal, action, token)
        assert 'отклик отправлен' in bot.events[-1][1]
    assert queue.get_candidate(token, profile_name='qa')['status'] == 'applied'


def test_good_hint_is_per_user_and_only_once(setup):
    bot, principal, items = setup
    tap(bot, principal, 'g', items[0]['token'])
    assert '👍 — хорошая' in bot.events[-1][1]
    tap(bot, principal, 'g', items[1]['token'])
    assert '👍 — хорошая' not in bot.events[-1][1]
    assert bot.state['review_good_hint_users'] == {'42': True}


def test_last_good_keeps_first_use_hint_on_empty_screen(setup):
    bot, principal, items = setup
    for item in items[1:]:
        queue.record_feedback(item['token'], 'bad', profile_name='qa', consume_decision=True)
    tap(bot, principal, 'g', items[0]['token'])
    assert bot.events[-1][1].startswith('✅ Всё разобрано. Сейчас от вас ничего не требуется.')
    assert '👍 — хорошая, но откликаться не буду; 👎 — мимо.' in bot.events[-1][1]


def test_snooze_copy_does_not_extend_and_profile_token_cannot_cross(setup):
    bot, principal, items = setup
    token = items[0]['token']
    first = queue.snooze_candidate(token, profile_name='qa')
    assert queue.snooze_candidate(token, profile_name='qa') is None
    assert queue.get_candidate(token, profile_name='qa')['snoozed_until'] == first['snoozed_until']
    assert queue.get_candidate(token, profile_name='other') is None
    assert queue.record_feedback(token, 'good', profile_name='other', consume_decision=True) is None


def test_entire_queue_pages_remain_reusable_in_place(setup):
    bot, principal, _ = setup
    tap(bot, principal, 'l', '0')
    assert bot.events[-1][0] == 'edit' and 'Вакансии на проверку: 3' in bot.events[-1][1]
    context = CallbackContext('cb', 42, principal, 'rq:L:qa:0:n',
        {'message_id': 7, 'chat': {'type': 'private'}}, 42, 7, lambda name: name == 'qa')
    asyncio.run(handle_review_queue(bot, context))
    assert bot.events[-1][0] == 'edit' and 'Сверху новые.' in bot.events[-1][1]


def test_real_callback_claim_releases_refused_confirmation_then_blocks_once(setup, monkeypatch):
    from tests.test_telegram_callbacks import _configured_bot, _callback
    fake, principal, items = setup
    bot, calls = _configured_bot(monkeypatch, principal=principal)
    monkeypatch.setattr(bot, '_profile', fake._profile)
    monkeypatch.setattr(bot, '_edit_text', fake._edit_text)
    token = items[0]['token']
    async def run():
        for action in ['x', 'c', 'X']:
            await bot._handle_callback_query(_callback(f'rq:{action}:qa:{token}', user_id=42, chat_type='private'))
        assert not queue.company_blacklist.is_blocked('Company', 'qa')
        assert 'Подтверждение устарело' in calls['answers'][-1][1]
        for action in ['x', 'X']:
            await bot._handle_callback_query(_callback(f'rq:{action}:qa:{token}', user_id=42, chat_type='private'))
        assert queue.company_blacklist.is_blocked('Company', 'qa')
        await bot._handle_callback_query(_callback(f'rq:X:qa:{token}', user_id=42, chat_type='private'))
        assert calls['answers'][-1][1] == 'Карточка: компания заблокирована.'
    asyncio.run(run())


@pytest.mark.parametrize('action', ['e', 'g', 'b', 's', 'x', 'm', 'w', 'c', 'h', 'v'])
def test_tester_cannot_read_or_act_on_foreign_or_unknown_card(setup, monkeypatch, action):
    import directions
    import profile_access
    bot, principal, items = setup
    principal.update(role='tester', direction='allowed')
    token = items[0]['token']
    def foreign(data):
        item = data['items'][token]
        item['direction'] = 'foreign'
        if action == 'e':
            item.update(status='applied', sent_cover='Private foreign letter')
    queue._store('qa').update(foreign)
    monkeypatch.setattr(profile_access, 'current_scope', lambda: ('qa-dev', 'allowed', '42'))
    allowed = {'name': 'allowed', 'title': 'Allowed', 'search_queries': ['QA'], 'variants': []}
    monkeypatch.setattr(directions, 'list_directions', lambda home: [allowed])
    monkeypatch.setattr(directions, 'active_direction', lambda home: allowed)
    before = queue._read_queue('qa')['items'][token]
    tap(bot, principal, action, token)
    assert not any(e[0] in {'send', 'edit'} for e in bot.events)
    assert queue._read_queue('qa')['items'][token] == before
    assert not queue.company_blacklist.is_blocked('Company', 'qa')


def test_tester_queue_never_exposes_unknown_bucket(setup, monkeypatch):
    import directions
    import profile_access
    bot, principal, items = setup
    principal.update(role='tester', direction='allowed')
    monkeypatch.setattr(profile_access, 'current_scope', lambda: ('qa-dev', 'allowed', '42'))
    allowed = {'name': 'allowed', 'title': 'Allowed', 'search_queries': ['QA'], 'variants': []}
    monkeypatch.setattr(directions, 'list_directions', lambda home: [allowed])
    monkeypatch.setattr(directions, 'active_direction', lambda home: allowed)
    assert bot._review_visible_items('qa') == []
    tap(bot, principal, 'U', '0')
    assert not any('Job ' in e[1] for e in bot.events)


def test_tester_cannot_approve_foreign_template(setup, monkeypatch):
    import directions
    import profile_access
    bot, principal, items = setup
    token = items[0]['token']
    queue._store('qa').update(lambda data: data['items'][token].update(direction='foreign'))
    monkeypatch.setattr(profile_access, 'current_scope', lambda: ('qa-dev', 'allowed', '42'))
    allowed = {'name': 'allowed', 'title': 'Allowed', 'search_queries': ['QA'], 'variants': []}
    monkeypatch.setattr(directions, 'list_directions', lambda home: [allowed])
    monkeypatch.setattr(directions, 'active_direction', lambda home: allowed)
    assert queue.allow_stub_cover(token, profile_name='qa') is False
    assert not queue._read_queue('qa')['items'][token].get('allow_stub_cover')


@pytest.mark.parametrize('prefix', ['manual_apply', 'stub', 'mad'])
def test_duplicate_manual_callback_scopes_uncertain_preview_before_router(setup, monkeypatch, prefix):
    """Spent callbacks run the outer guard before scoped_request binds the tester."""
    import hashlib
    import time
    import directions
    import profile_access
    from tests.test_telegram_callbacks import _configured_bot, _callback
    fake, _, _ = setup
    principal = {'user_id': 42, 'role': 'tester', 'profile': 'qa-dev', 'direction': 'allowed'}
    bot, calls = _configured_bot(monkeypatch, principal=principal)
    monkeypatch.setattr(bot, '_profile_names', lambda: ['qa-dev'])
    monkeypatch.setattr(bot, '_selected_profile', lambda p: p['profile'])
    monkeypatch.setattr(bot, '_profile', fake._profile)
    # Registry preparation is separate from the context binding exercised here.
    monkeypatch.setattr(profile_access, 'require_tester_binding',
                        lambda p: (p['profile'], p['direction']))
    allowed = {'name': 'allowed', 'title': 'Allowed', 'search_queries': ['QA'], 'variants': []}
    monkeypatch.setattr(directions, 'list_directions', lambda home: [allowed])
    monkeypatch.setattr(directions, 'active_direction', lambda home: allowed)
    item = queue.create_candidate({'id': 'foreign', 'title': 'Private foreign vacancy'}, {},
                                  profile_name='qa-dev')
    queue._store('qa-dev').update(lambda data: data['items'][item['token']].update(
        direction='foreign', status='uncertain', orphan_recovered=True))
    data = f'{prefix}:qa-dev:{item["token"]}'
    claim = hashlib.sha256(repr((42, 42, 0, data)).encode()).hexdigest()
    bot._update_state(lambda state: state.setdefault('callback_claims', {}).update({claim: time.time()}))
    assert not profile_access.current_scope()[1]
    before = queue._read_queue('qa-dev')
    asyncio.run(bot._handle_callback_query(_callback(data, user_id=42, chat_type='private')))
    assert calls['messages'] == []
    assert calls['answers'][-1][1] == 'Карточка: не ждёт решения.'
    assert queue._read_queue('qa-dev') == before
