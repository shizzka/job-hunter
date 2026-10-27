"""Synthetic unified pending inbox paths; no network or installed profiles."""
import asyncio
import hashlib
import time
from pathlib import Path

import pytest

import manual_apply_queue as queue
from state_store.chat_responder import ChatResponderStateRepository
from state_store.google_forms import GoogleFormStateRepository
from telegram_app.callback_handlers import CallbackContext
from telegram_app.callback_handlers.review import handle_review_queue
from tests.test_f27_review import Bot as ReviewBot


class Bot(ReviewBot):
    def _selected_profile(self, principal):
        return principal['profile']
    def _has_active_command(self, profile):
        return False
    async def _show_form(self, chat, principal, profile, token, offset=0):
        self.events.append(('form', profile, token))
    async def _start_chat_ai_reply(self, chat, principal, **kwargs):
        self.events.append(('reply', kwargs, None))


@pytest.fixture
def inbox(tmp_path, monkeypatch):
    home = tmp_path / 'qa'
    monkeypatch.setattr(queue, '_queue_path', lambda profile_name=None: tmp_path / str(profile_name) / 'queue.json')
    monkeypatch.setattr(queue.company_blacklist, '_path', lambda profile_name=None: tmp_path / str(profile_name) / 'blacklist.json')
    monkeypatch.setattr('direction_views.DirectionView.chat_status', lambda self, chat: 'selected')
    bot = Bot(home)
    principal = {'user_id': 42, 'role': 'user', 'profile': 'qa'}
    vacancy = queue.create_candidate({'id': '1', 'title': 'Vacancy', 'company': 'Example'}, {'score': 95}, profile_name='qa')
    queue._store('qa').update(lambda data: data['items'][vacancy['token']].update(created_ts=time.time() - 30))
    form = 'a' * 12
    GoogleFormStateRepository(home).remember(form, {'created_at': time.time() - 50, 'chat_id': '100', 'message_id': '1',
        'form_url': 'https://docs.google.com/forms/d/e/synthetic/viewform', 'status': 'preview',
        'questions': [{'index': 0, 'question': 'Name', 'type': 'text'}], 'answers': [],
        'vacancy': {'title': 'Form vacancy'}}, trim_expired=False)
    draft = {'message_id': '2', 'answer': 'Original answer',
        'answer_hash': hashlib.sha256(b'Original answer').hexdigest(), 'revision': 'b' * 12,
        'candidate_revision': 'c' * 64, 'source_revision': 'd' * 64}
    ChatResponderStateRepository(home).save({'200': {'draft': draft, 'last_previewed_at': time.time() - 40}})
    return bot, principal, vacancy, form, draft


def show(bot, principal):
    asyncio.run(bot._show_review_queue(42, principal, profile_name='qa'))
    return bot.events[-1]


def tap(bot, principal, data, *, access=True, private=True):
    context = CallbackContext('cb', 42, principal, data,
        {'message_id': 7, 'chat': {'type': 'private' if private else 'group'}}, 42, 7,
        lambda name: access and name == 'qa')
    asyncio.run(handle_review_queue(bot, context))


def buttons(event):
    return [b for row in event[2]['inline_keyboard'] for b in row]


def test_mixed_inbox_oldest_first_card_navigation_and_same_list(inbox):
    bot, principal, vacancy, form, draft = inbox
    event = show(bot, principal)
    assert '1 из 3' in event[1] and 'Анкеты' in event[1]
    fill = next(b for b in buttons(event) if b['text'] == '📝 Заполнить')
    tap(bot, principal, fill['callback_data'])
    assert bot.events[-1] == ('form', 'qa', form)
    show(bot, principal)
    tap(bot, principal, next(b['callback_data'] for b in buttons(bot.events[-1]) if b['text'] == '▶'))
    assert '2 из 3' in bot.events[-1][1] and 'Ответ работодателю' in bot.events[-1][1]
    assert 'Original answer' in bot.events[-1][1]
    tap(bot, principal, 'rq:l:qa:0')
    text = bot.events[-1][1]
    assert text.index('Анкеты') < text.index('Ответ работодателю') < text.index('Vacancy')
    assert 'Сначала старые' in text


def test_inbox_draft_send_reuses_approved_revision(inbox):
    bot, principal, vacancy, form, draft = inbox
    show(bot, principal)
    tap(bot, principal, next(b['callback_data'] for b in buttons(bot.events[-1]) if b['text'] == '▶'))
    send = next(b['callback_data'] for b in buttons(bot.events[-1]) if b['text'] == '✅ Отправить')
    tap(bot, principal, send)
    assert bot.events[-1][0] == 'reply'
    assert bot.events[-1][1]['force_send'] is True
    assert bot.events[-1][1]['hh_message_id'] == '2~' + draft['revision']


@pytest.mark.parametrize('private,access', [(True, False), (False, True)])
def test_inbox_refuses_foreign_or_group_action_without_side_effect(inbox, private, access):
    bot, principal, vacancy, form, draft = inbox
    event = show(bot, principal)
    fill = next(b['callback_data'] for b in buttons(event) if b['text'] == '📝 Заполнить')
    tap(bot, principal, fill, private=private, access=access)
    assert bot.events[-1][0] == 'answer' and 'недоступ' in bot.events[-1][1].lower()
    assert not any(e[0] == 'form' for e in bot.events)


def test_form_later_is_persistent_repeat_safe_and_advances(inbox):
    bot, principal, vacancy, form, draft = inbox
    show(bot, principal)
    later = next(b['callback_data'] for b in buttons(bot.events[-1]) if b['text'] == '⏭ Позже')
    tap(bot, principal, later)
    assert 'Ответ работодателю' in bot.events[-1][1]
    from telegram_app.pending_inbox import InboxPostponements
    before = InboxPostponements(bot.home).load()
    tap(bot, principal, later)
    assert InboxPostponements(bot.home).load() == before
    fresh = Bot(bot.home)
    assert 'Ответ работодателю' in show(fresh, principal)[1]
    assert GoogleFormStateRepository(bot.home).load()['items'][form]['status'] == 'preview'


def test_stale_form_and_replaced_chat_draft_cannot_act(inbox):
    bot, principal, vacancy, form, draft = inbox
    show(bot, principal)
    fill = next(b['callback_data'] for b in buttons(bot.events[-1]) if b['text'] == '📝 Заполнить')
    tap(bot, principal, next(b['callback_data'] for b in buttons(bot.events[-1]) if b['text'] == '▶'))
    send = next(b['callback_data'] for b in buttons(bot.events[-1]) if b['text'] == '✅ Отправить')
    repo = GoogleFormStateRepository(bot.home)
    state = repo.load()
    state['items'][form]['status'] = 'submitted'
    repo.save(state)
    ChatResponderStateRepository(bot.home).update_chat('200', lambda chat: chat['draft'].update(revision='e' * 12))
    tap(bot, principal, fill)
    assert 'устарел' in bot.events[-1][1].lower()
    tap(bot, principal, send)
    assert 'устарел' in bot.events[-1][1].lower()
    assert not any(e[0] in {'reply', 'form'} for e in bot.events)


def test_manual_edit_saves_revision_then_requires_new_explicit_send(inbox):
    bot, principal, vacancy, form, draft = inbox
    show(bot, principal)
    tap(bot, principal, next(b['callback_data'] for b in buttons(bot.events[-1]) if b['text'] == '▶'))
    send = next(b['callback_data'] for b in buttons(bot.events[-1]) if b['text'] == '✅ Отправить')
    edit = next(b['callback_data'] for b in buttons(bot.events[-1]) if b['text'] == '✏️ Изменить')
    tap(bot, principal, edit)
    assert bot.events[-1][2] == {'force_reply': True, 'selective': True}
    assert not asyncio.run(bot._accept_inbox_reply(42, principal, {'text': 'Wrong reply'}))
    message = {'text': 'My updated answer', 'reply_to_message': {'message_id': 7}}
    assert asyncio.run(bot._accept_inbox_reply(42, principal, message))
    stored = ChatResponderStateRepository(bot.home).load()['200']['draft']
    assert stored['answer'] == message['text'] and stored['revision'] != draft['revision']
    assert stored['answer_hash'] == hashlib.sha256(message['text'].encode()).hexdigest()
    assert not any(e[0] == 'reply' for e in bot.events)
    tap(bot, principal, send)
    assert 'устарел' in bot.events[-1][1].lower()
    assert not asyncio.run(bot._accept_inbox_reply(42, principal, message))


def test_owner_inbox_includes_all_searches_but_tester_remains_bound(inbox, monkeypatch):
    import profile_access
    from tests.direction_view_fakes import two_directions
    bot, principal, vacancy, form, draft = inbox
    two_directions(Path(bot.home))
    second = queue.create_candidate({'id': 'electric', 'title': 'Other search',
        '_direction_selection': {'direction': 'electric'}}, {'score': 25}, profile_name='qa')
    queue._store('qa').update(lambda data: data['items'][vacancy['token']]['vacancy'].update(
        _direction_selection={'direction': 'qa'}))
    assert {item['token'] for item in bot._review_visible_items('qa')} >= {vacancy['token'], second['token']}
    # HistoryScope uses explicit ownership metadata for tester principals.
    queue._store('qa').update(lambda data: data['items'][vacancy['token']].update(direction='qa'))
    queue._store('qa').update(lambda data: data['items'][second['token']].update(direction='electric'))
    monkeypatch.setattr(profile_access, 'current_scope', lambda: ('qa-dev', 'qa', '42'))
    visible = bot._review_visible_items('qa')
    assert [item['token'] for item in visible] == [vacancy['token']]


def test_unowned_and_superseded_form_and_sent_drafts_are_not_pending(inbox, monkeypatch):
    bot, principal, vacancy, form, draft = inbox
    # Failure to prove chat ownership hides both sources.
    monkeypatch.setattr('direction_views.DirectionView.chat_status', lambda self, chat: 'review')
    assert [item['token'] for item in bot._review_visible_items('qa')] == [vacancy['token']]
    monkeypatch.setattr('direction_views.DirectionView.chat_status', lambda self, chat: 'selected')
    from google_forms import drafts
    drafts.edits_store(bot.home).update(lambda state: state.update({form: {'superseded_by': 'f' * 12}}))
    ChatResponderStateRepository(bot.home).update_chat('200', lambda chat: chat.update(last_replied_msg_id='2'))
    assert [item['token'] for item in bot._review_visible_items('qa')] == [vacancy['token']]


def test_manual_edit_cancel_and_stale_prompt_leave_draft_untouched(inbox):
    bot, principal, vacancy, form, draft = inbox
    show(bot, principal)
    tap(bot, principal, next(b['callback_data'] for b in buttons(bot.events[-1]) if b['text'] == '▶'))
    edit = next(b['callback_data'] for b in buttons(bot.events[-1]) if b['text'] == '✏️ Изменить')
    tap(bot, principal, edit)
    assert asyncio.run(bot._accept_inbox_reply(42, principal, {'text': '/cancel_reply'}))
    assert ChatResponderStateRepository(bot.home).load()['200']['draft'] == draft
    tap(bot, principal, edit)
    repo = ChatResponderStateRepository(bot.home)
    owner = repo.claim('200', 'reply', '2')
    assert owner
    assert asyncio.run(bot._accept_inbox_reply(42, principal,
        {'text': 'must not overwrite a sending reply', 'reply_to_message': {'message_id': 7}}))
    assert repo.load()['200']['draft'] == draft
    assert 'устарел' in bot.events[-1][1].lower()


def test_confirmed_draft_send_repeat_and_postponed_revision_are_stale(inbox):
    bot, principal, vacancy, form, draft = inbox
    show(bot, principal)
    tap(bot, principal, next(b['callback_data'] for b in buttons(bot.events[-1]) if b['text'] == '▶'))
    send = next(b['callback_data'] for b in buttons(bot.events[-1]) if b['text'] == '✅ Отправить')
    tap(bot, principal, send)
    ChatResponderStateRepository(bot.home).update_chat('200', lambda chat: chat.update(last_replied_msg_id='2'))
    tap(bot, principal, send)
    assert 'устарел' in bot.events[-1][1].lower()
    assert sum(event[0] == 'reply' for event in bot.events) == 1


def guarded_tap(bot, principal, data, monkeypatch, *, message_id=7):
    """Exercise the production durable callback guard around the real inbox handler."""
    from telegram_app.guards import callback_guard
    from tests.test_telegram_callbacks import _callback
    monkeypatch.setattr('telegram_access.resolve_user', lambda user: principal)
    async def dispatch(current_bot, callback):
        message = callback['message']
        return await handle_review_queue(current_bot, CallbackContext(callback['id'], 42, principal,
            callback['data'], message, 42, message['message_id'], lambda profile: profile == 'qa'))
    asyncio.run(callback_guard(dispatch)(bot, _callback(data, user_id=42, message_id=message_id, chat_type='private')))


def test_real_guard_new_later_after_expiry_and_old_keyboards_stay_inert(inbox, monkeypatch):
    from telegram_app.pending_inbox import InboxPostponements
    bot, principal, vacancy, form, draft = inbox
    clock = [time.time()]
    monkeypatch.setattr('telegram_app.pending_inbox.time.time', lambda: clock[0])
    show(bot, principal)
    old_later = next(b['callback_data'] for b in buttons(bot.events[-1]) if b['text'] == '⏭ Позже')
    guarded_tap(bot, principal, old_later, monkeypatch)
    first = InboxPostponements(bot.home).load()
    guarded_tap(bot, principal, old_later, monkeypatch, message_id=99)
    assert InboxPostponements(bot.home).load() == first
    assert bot.events[-1][1] == 'Уже сделано'
    clock[0] += 86401
    show(bot, principal)
    new_later = next(b['callback_data'] for b in buttons(bot.events[-1]) if b['text'] == '⏭ Позже')
    assert new_later != old_later
    # Even bypassing the outer claim, the expired keyboard cannot postpone again.
    tap(bot, principal, old_later)
    assert InboxPostponements(bot.home).load() == first
    guarded_tap(bot, principal, new_later, monkeypatch, message_id=100)
    second = InboxPostponements(bot.home).load()
    assert second['f' + form] > first['f' + form]
    guarded_tap(bot, principal, new_later, monkeypatch, message_id=101)
    assert InboxPostponements(bot.home).load() == second
    assert bot.events[-1][1] == 'Уже сделано'


def test_real_guard_draft_send_copies_dispatch_once(inbox, monkeypatch):
    bot, principal, vacancy, form, draft = inbox
    show(bot, principal)
    tap(bot, principal, next(b['callback_data'] for b in buttons(bot.events[-1]) if b['text'] == '▶'))
    send = next(b['callback_data'] for b in buttons(bot.events[-1]) if b['text'] == '✅ Отправить')
    guarded_tap(bot, principal, send, monkeypatch)
    guarded_tap(bot, principal, send, monkeypatch, message_id=99)
    assert sum(event[0] == 'reply' for event in bot.events) == 1
    assert bot.events[-1][1] == 'Уже сделано'


def test_bulk_dismiss_covers_inactive_search_advertised_by_unified_list(inbox):
    from tests.direction_view_fakes import two_directions
    bot, principal, vacancy, form, draft = inbox
    two_directions(Path(bot.home))
    queue._store('qa').update(lambda data: data['items'][vacancy['token']]['vacancy'].update(
        _direction_selection={'direction': 'qa'}))
    low = queue.create_candidate({'id': 'inactive-low', 'title': 'Inactive low',
        '_direction_selection': {'direction': 'electric'}}, {'score': 20}, profile_name='qa')
    asyncio.run(bot._show_review_list(42, principal, profile_name='qa'))
    assert any(button['callback_data'].startswith('rq:d:qa:40') for button in buttons(bot.events[-1]))
    asyncio.run(bot._review_dismiss_below(42, principal, 'qa', 40, confirmed=False))
    assert 'Убрать 1 вакансий' in bot.events[-1][1]
    confirm = buttons(bot.events[-1])[0]['callback_data']
    tap(bot, principal, confirm)
    assert queue.get_candidate(low['token'], profile_name='qa')['status'] == 'dismissed'
    assert queue.get_candidate(vacancy['token'], profile_name='qa')['status'] == 'pending'


from telegram_app.responses import TelegramResponses


class NativeBot(Bot, TelegramResponses):
    def _set_search_settings_state(self, user_id, **kwargs):
        self.state.setdefault('search_settings', {}).setdefault(str(user_id), {}).update(kwargs)
    def _clear_search_settings_state(self, user_id, key):
        self.state.get('search_settings', {}).get(str(user_id), {}).pop(key, None)
    def _search_settings_state(self, user_id):
        return self.state.get('search_settings', {}).get(str(user_id), {})
    async def _start_manual_ai_apply(self, chat, principal, **kwargs):
        self.events.append(('apply', kwargs, None))


def native_question(inbox, *, options=None):
    import question_memory
    old, principal, vacancy, form, draft = inbox
    bot = NativeBot(old.home)
    qid = question_memory.create_pending(bot.home, question='Expected salary?',
        control='radio' if options else 'text', options=options or [], token=vacancy['token'], profile_name='qa')
    item = next(item for item in bot._review_visible_items('qa') if item.get('qid') == qid)
    return bot, principal, vacancy, qid, item


def test_native_question_replaces_vacancy_and_choice_reuses_explicit_apply_workflow(inbox):
    import question_memory
    bot, principal, vacancy, qid, item = native_question(inbox, options=['100', '200'])
    assert vacancy['token'] not in {row['token'] for row in bot._review_visible_items('qa')}
    tap(bot, principal, f"rq:c:qa:{item['token']}")
    assert 'Анкеты' in bot.events[-1][1] and 'повторит отклик' in bot.events[-1][1]
    tap(bot, principal, next(button['callback_data'] for button in buttons(bot.events[-1]) if button['text'] == '📝 Заполнить'))
    answer = next(button['callback_data'] for button in buttons(bot.events[-1]) if button['text'] == '200')
    assert 'повторит отклик' in bot.events[-1][1]
    tap(bot, principal, answer)
    assert question_memory.peek_pending(bot.home, qid) is None
    assert question_memory.lookup(bot.home, 'Expected salary?')['answer'] == '200'
    assert bot.events[-1] == ('apply', {'profile_name': 'qa', 'token': vacancy['token']}, None)
    tap(bot, principal, answer)
    assert sum(event[0] == 'apply' for event in bot.events) == 1
    assert 'устарела' in bot.events[-1][1]


def test_native_question_text_binding_rechecks_source_at_submission(inbox):
    import question_memory
    bot, principal, vacancy, qid, item = native_question(inbox)
    tap(bot, principal, f"rq:T:qa:{item['token']}")
    assert bot._search_settings_state(42)['question_answer']['inbox_token'] == item['token']
    queue.mark_candidate(vacancy['token'], 'dismissed', profile_name='qa')
    assert asyncio.run(bot._accept_employer_question_text(42, principal, 'My answer'))
    assert question_memory.peek_pending(bot.home, qid) is not None
    assert not question_memory.lookup(bot.home, 'Expected salary?')
    assert not any(event[0] == 'apply' for event in bot.events)
    assert 'устарел' in bot.events[-1][1]


def test_native_question_live_text_answer_and_later_preserve_source(inbox):
    import question_memory
    bot, principal, vacancy, qid, item = native_question(inbox)
    tap(bot, principal, f"rq:c:qa:{item['token']}")
    later = next(button['callback_data'] for button in buttons(bot.events[-1]) if button['text'] == '⏭ Позже')
    tap(bot, principal, later)
    assert question_memory.peek_pending(bot.home, qid) is not None
    assert not {vacancy['token'], item['token']} & {row['token'] for row in bot._review_visible_items('qa')}
    # Another fresh question remains editable through its validated text workflow.
    from telegram_app.pending_inbox import InboxPostponements
    InboxPostponements(bot.home).store.update(lambda state: state.clear())
    tap(bot, principal, f"rq:T:qa:{item['token']}")
    assert asyncio.run(bot._accept_employer_question_text(42, principal, '150'))
    assert question_memory.lookup(bot.home, 'Expected salary?')['answer'] == '150'
    assert sum(event[0] == 'apply' for event in bot.events) == 1


def test_native_question_ttl_direction_and_resume_mismatch_are_hidden(inbox, monkeypatch):
    import question_memory
    import profile_access
    from tests.direction_view_fakes import two_directions
    bot, principal, vacancy, qid, item = native_question(inbox)
    two_directions(Path(bot.home))
    queue._store('qa').update(lambda data: data['items'][vacancy['token']]['vacancy'].update(
        _direction_selection={'direction': 'electric', 'hh_resume_hash': 'r2'}))
    path = str(Path(bot.home) / question_memory.PENDING_FILE)
    data = question_memory._read(path)
    data[qid].update(direction='qa', resume_id='r2')
    question_memory._write(path, data)
    assert not any(row.get('qid') == qid for row in bot._review_visible_items('qa'))
    data[qid].update(direction='electric', resume_id='wrong')
    question_memory._write(path, data)
    assert not any(row.get('qid') == qid for row in bot._review_visible_items('qa'))
    data[qid]['resume_id'] = 'r2'
    question_memory._write(path, data)
    assert any(row.get('qid') == qid for row in bot._review_visible_items('qa'))
    monkeypatch.setattr(profile_access, 'current_scope', lambda: ('qa-dev', 'qa', '42'))
    assert not any(row.get('qid') == qid for row in bot._review_visible_items('qa'))
    monkeypatch.setattr(profile_access, 'current_scope', lambda: ('', '', ''))
    data[qid]['created_at'] = time.time() - question_memory.PENDING_TTL_SECONDS - 1
    question_memory._write(path, data)
    tap(bot, principal, f"rq:F:qa:{item['token']}")
    assert bot.events[-1][0] == 'answer' and 'устарела' in bot.events[-1][1]


def test_true_oldest_pending_survives_more_than_250_and_creation_retention(inbox):
    bot, principal, vacancy, form, draft = inbox
    now = time.time()
    def seed(state):
        state['items'] = {f'{number:012x}': dict(vacancy, token=f'{number:012x}',
            created_ts=now - number, vacancy={'id': str(number), 'title': f'Job {number}'})
            for number in range(301)}
    queue._store('qa').update(seed)
    queue.create_candidate({'id': 'additional', 'title': 'Additional'}, {'score': 50}, profile_name='qa')
    rows = bot._review_visible_items('qa')
    assert rows[0]['token'] == f'{300:012x}'
    assert sum(not row.get('_inbox_kind') for row in rows) == 302
    assert len(queue.list_candidates('qa', limit=250)) == 250


@pytest.mark.parametrize('answer', ['', 'x' * 3501])
def test_manual_edit_rejects_empty_or_oversized_without_changing_draft(inbox, answer):
    bot, principal, vacancy, form, draft = inbox
    show(bot, principal)
    tap(bot, principal, next(b['callback_data'] for b in buttons(bot.events[-1]) if b['text'] == '▶'))
    tap(bot, principal, next(b['callback_data'] for b in buttons(bot.events[-1]) if b['text'] == '✏️ Изменить'))
    assert asyncio.run(bot._accept_inbox_reply(42, principal,
        {'text': answer, 'reply_to_message': {'message_id': 7}}))
    assert ChatResponderStateRepository(bot.home).load()['200']['draft'] == draft
    assert '3500' in bot.events[-1][1]
    assert bot.state['inbox_reply_pending']


def test_manual_edit_revoked_profile_and_expired_prompt_cannot_change_draft(inbox):
    bot, principal, vacancy, form, draft = inbox
    show(bot, principal)
    tap(bot, principal, next(b['callback_data'] for b in buttons(bot.events[-1]) if b['text'] == '▶'))
    tap(bot, principal, next(b['callback_data'] for b in buttons(bot.events[-1]) if b['text'] == '✏️ Изменить'))
    answer = {'text': 'Changed', 'reply_to_message': {'message_id': 7}}
    denied = dict(principal, profile='other')
    assert asyncio.run(bot._accept_inbox_reply(42, denied, answer))
    assert ChatResponderStateRepository(bot.home).load()['200']['draft'] == draft
    bot.state['inbox_reply_pending']['42:42']['created_at'] = time.time() - 86401
    assert not asyncio.run(bot._accept_inbox_reply(42, principal, answer))
    assert not bot.state['inbox_reply_pending']
    assert ChatResponderStateRepository(bot.home).load()['200']['draft'] == draft


def test_native_question_rechecks_ttl_after_callback_ack(inbox, monkeypatch):
    import question_memory
    bot, principal, vacancy, qid, item = native_question(inbox, options=['A'])
    original = bot._answer_callback_query
    async def acknowledge(*args, **kwargs):
        await original(*args, **kwargs)
        path = str(Path(bot.home) / question_memory.PENDING_FILE)
        data = question_memory._read(path)
        data[qid]['created_at'] = time.time() - question_memory.PENDING_TTL_SECONDS - 1
        question_memory._write(path, data)
    monkeypatch.setattr(bot, '_answer_callback_query', acknowledge)
    tap(bot, principal, f"rq:F:qa:{item['token']}")
    assert bot.events[-1][0] == 'send' and 'устарел' in bot.events[-1][1]
    assert not any(event[0] == 'apply' for event in bot.events)


def test_native_question_changed_source_cannot_use_previous_option_or_text_prompt(inbox):
    import question_memory
    bot, principal, vacancy, qid, item = native_question(inbox, options=['100'])
    tap(bot, principal, f"rq:T:qa:{item['token']}")
    path = str(Path(bot.home) / question_memory.PENDING_FILE)
    data = question_memory._read(path)
    data[qid]['question'] = 'Changed employer question'
    question_memory._write(path, data)
    tap(bot, principal, f"rq:Q:qa:{item['token']}:0")
    assert 'устарела' in bot.events[-1][1]
    assert asyncio.run(bot._accept_employer_question_text(42, principal, 'Old answer'))
    assert question_memory.peek_pending(bot.home, qid)['question'] == 'Changed employer question'
    assert not any(event[0] == 'apply' for event in bot.events)
