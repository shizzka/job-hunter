"""Offline admission, real dispatch/polling, and durable stale-button regressions."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
import threading
from unittest.mock import AsyncMock

import pytest

import telegram_bot
from telegram_app import guards, hh_counter_cache, history
from tests.test_telegram_menu_routing import menu_bot
from tests.test_telegram_command_queue import real_bot, source, spin_until


def test_claim_timestamps_expire_and_legacy_claims_get_full_grace(menu_bot, monkeypatch):
    from state_store import bot_state
    bot, _ = menu_bot
    now = [10_000_000.0]
    clock = SimpleNamespace(time=lambda: now[0])
    monkeypatch.setattr(bot_state, 'time', clock)
    monkeypatch.setattr(guards, 'time', clock)
    monkeypatch.setattr(telegram_bot.telegram_access, 'resolve_user',
                        lambda uid: {'user_id': uid, 'role': 'user', 'profile': 'qa'})
    bot._save_state({'callback_claims': {'legacy': True, 'old': now[0] - bot_state.CALLBACK_CLAIM_TTL - 1,
                                        'recent': now[0] - 10}})
    assert bot._load_state()['callback_claims'] == {'legacy': now[0], 'recent': now[0] - 10}
    actions = []
    @guards.callback_guard
    async def act(self, callback):
        actions.append(callback['_guard_claim'])
    callback = {'id': 'a', 'data': 'gs:confirm', 'from': {'id': 42},
                'message': {'message_id': 7, 'chat': {'id': 42, 'type': 'private'}}}
    monkeypatch.setattr(bot, '_answer_callback_query', AsyncMock())
    asyncio.run(act(bot, callback))
    claim = actions[0]
    created = now[0]
    assert bot._load_state()['callback_claims'][claim] == created
    now[0] += bot_state.CALLBACK_CLAIM_TTL - 1
    asyncio.run(act(bot, callback))
    assert len(actions) == 1
    assert bot._load_state()['callback_claims']['legacy'] == created
    now[0] += 2
    asyncio.run(act(bot, callback))
    assert actions == [claim, claim]
    assert bot._load_state()['callback_claims'] == {claim: now[0]}


def test_every_bot_state_write_prunes_claims_and_persists_legacy_migration(tmp_path, monkeypatch):
    from state_store import bot_state
    from state_store.json_store import atomic_write_json
    path = tmp_path / 'state.json'
    atomic_write_json(path, {'callback_claims': {'legacy': True}, 'last_update_id': 7})
    now = [10_000_000.0]
    monkeypatch.setattr(bot_state, 'time', SimpleNamespace(time=lambda: now[0]))
    store = bot_state.BotStateStore(path)
    store.update(lambda state: state.update(last_update_id=8))
    assert store.load()['callback_claims'] == {'legacy': now[0]}
    now[0] += bot_state.CALLBACK_CLAIM_TTL - 1
    store = bot_state.BotStateStore(path)  # Restart must not renew the grace period.
    store.update(lambda state: state.update(last_update_id=9))
    assert store.load()['callback_claims'] == {'legacy': now[0] - bot_state.CALLBACK_CLAIM_TTL + 1}
    now[0] += 2
    store.update(lambda state: state.update(last_update_id=10))
    assert store.load() == {'callback_claims': {}, 'last_update_id': 10}


@pytest.mark.parametrize('command', sorted(guards.READS))
def test_reads_are_single_flight_cool_down_and_are_per_user_command(monkeypatch, command):
    clock = [100.0]
    monkeypatch.setattr(guards, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    class Bot:
        def __init__(self):
            self.calls, self.messages = [], []
            self.entered, self.finish = asyncio.Event(), asyncio.Event()
        async def _send_text(self, chat, text):
            self.messages.append(text)
        @guards.read_command
        async def read(self, chat, principal, cmd, arg):
            self.calls.append((principal['user_id'], cmd))
            if principal['user_id'] == 1 and cmd == command:
                self.entered.set()
                await self.finish.wait()
    async def scenario():
        bot = Bot()
        first = asyncio.create_task(bot.read(1, {'user_id': 1}, command, ''))
        await bot.entered.wait()
        await asyncio.gather(bot.read(1, {'user_id': 1}, command, ''),
                             bot.read(2, {'user_id': 2}, command, ''),
                             bot.read(1, {'user_id': 1}, '/start', ''))
        assert bot.calls == [(1, command), (2, command), (1, '/start')]
        assert bot.messages == [guards.BUSY]
        bot.finish.set()
        await first
        if command in {'/applications', '/applications_invitations', '/applications_rejections'}:
            # Local history filters and page controls reopen immediately.
            # The in-flight, per-user and per-command assertions above still
            # apply; existing expensive reads retain their five-second limit.
            await bot.read(1, {'user_id': 1}, command, '')
            assert bot.messages == [guards.BUSY]
            assert bot.calls == [(1, command), (2, command), (1, '/start'), (1, command)]
            return
        clock[0] += 4.999
        await bot.read(1, {'user_id': 1}, command, '')
        assert bot.messages == [guards.BUSY] * 2
        clock[0] += 0.001
        await bot.read(1, {'user_id': 1}, command, '')
        assert bot.calls[-1] == (1, command) and len(bot.calls) == 4
    asyncio.run(scenario())


def test_real_two_updates_and_start_answer_during_stats(menu_bot, monkeypatch):
    bot, _ = menu_bot
    entered, finish = threading.Event(), threading.Event()
    computations = []
    def report(*args):
        computations.append(True)
        entered.set()
        assert finish.wait(5)
        return 'Synthetic', ['stats complete'], None
    monkeypatch.setattr(history, '_history_report', report)
    principal = {'user_id': 42, 'profile': 'qa', 'role': 'user'}
    monkeypatch.setattr(telegram_bot.telegram_access, 'resolve_user', lambda uid: principal)
    monkeypatch.setattr(bot, '_send_getting_started', AsyncMock())
    def update(text, n):
        return {'update_id': n, 'message': {'from': {'id': 42},
                'chat': {'id': 42, 'type': 'private'}, 'text': text}}
    async def scenario():
        try:
            # Exercise the production polling entry point, not parallel _dispatch alone.
            await bot._run_update(update(telegram_bot.BUTTON_STATS, 1))
            await spin_until(entered.is_set)
            await asyncio.wait_for(asyncio.gather(bot._run_update(update('/stats', 2)),
                                                  bot._run_update(update('/start', 3))), 1)
            await spin_until(lambda: any(c.args[1] == guards.BUSY for c in bot._send_text.call_args_list))
            assert any(c.args[1] != guards.BUSY for c in bot._send_text.call_args_list)
            assert computations == [True]
        finally:
            finish.set()
            await asyncio.gather(*getattr(bot, '_read_update_tasks', ()))
    asyncio.run(scenario())
    assert any(c.args[1] == 'stats complete' for c in bot._send_text.call_args_list)


def test_timeout_keeps_guard_until_thread_finishes(menu_bot, monkeypatch):
    bot, _ = menu_bot
    entered, finish = threading.Event(), threading.Event()
    calls = []
    def report(*args):
        calls.append(True)
        entered.set()
        assert finish.wait(5)
        return 'report', None, None
    monkeypatch.setattr(history, '_run_report', report)
    monkeypatch.setattr(history, 'RUN_SUMMARY_TIMEOUT_SECONDS', 0.01)
    clock = [100.0]
    monkeypatch.setattr(guards, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    principal = {'user_id': 42, 'role': 'user', 'profile': 'qa'}
    async def scenario():
        try:
            await bot._dispatch(42, principal, '/log', '')
            assert entered.is_set()
            clock[0] += 100
            await bot._dispatch(42, principal, '/hunter_log', '')
            assert calls == [True]
            assert bot._send_text.call_args.args[1] == guards.BUSY
        finally:
            finish.set()
            await spin_until(lambda: all(v is not None for v in bot._request_slots.values()))
    asyncio.run(scenario())


@pytest.mark.parametrize('command', ['/analyze', '/chat_ai'])
def test_llm_calls_not_duplicated_running_or_queued(monkeypatch, tmp_path, command):
    async def scenario():
        bot, _, messages, _ = real_bot(monkeypatch, tmp_path)
        principal = {'user_id': 1, 'role': 'admin', 'profile': 'qa'}
        entered, finish = asyncio.Event(), asyncio.Event()
        calls = []
        async def capture(active, *args, **kwargs):
            calls.append(True)
            entered.set()
            await finish.wait()
            return {'ok': False, 'stdout': '', 'stderr': ''}
        monkeypatch.setattr(bot, '_run_active_command_capture', capture)
        monkeypatch.setattr(bot, '_authorize_ai_usage', AsyncMock(return_value=True))
        monkeypatch.setattr(bot, '_daemon_state', lambda name: {'running': False})
        monkeypatch.setattr(bot, '_delete_message', AsyncMock())
        # Chat preview uses the real starter and runner, mocked only at HH/LLM capture.
        arg = '123 456' if command == '/chat_ai' else ''
        await asyncio.gather(bot._dispatch(42, principal, command, arg),
                             bot._dispatch(42, principal, command, arg))
        await asyncio.wait_for(entered.wait(), 1)
        assert calls == [True] and bot._queue_length('qa') == 0
        assert any(m['text'] == guards.BUSY for m in messages)
        finish.set()
        await bot._active_commands['qa'].task
        await bot._shutdown_command_queue()
    asyncio.run(scenario())


@pytest.mark.parametrize('data', ['dir:token:deleteyes', 'dir:token:autoyes', 'manual_apply:qa:token',
                                   'chat_send:qa:123:456', 'gf:send:qa:token:proof',
                                   'manual_feedback:qa:token:reject', 'sr:do:all:all:proof', 'gs:confirm', 'gs:search'])
def test_action_claim_is_atomic_and_survives_stale_message_restart(menu_bot, monkeypatch, data):
    bot, _ = menu_bot
    principal = {'user_id': 42, 'role': 'user', 'profile': 'qa'}
    monkeypatch.setattr(telegram_bot.telegram_access, 'resolve_user', lambda uid: {**principal, 'user_id': uid})
    answered, actions = [], []
    done_reply = "Карточка: не ждёт решения." if data.startswith("manual_apply:") else guards.DONE
    async def answer(callback_id, text='', **kwargs):
        answered.append(text)
    monkeypatch.setattr(bot, '_answer_callback_query', answer)
    entered, finish = None, None
    @guards.callback_guard
    async def act(self, callback):
        actions.append(callback['from']['id'])
        entered.set()
        await finish.wait()
    callback = {'id': 'a', 'data': data, 'from': {'id': 42},
                'message': {'message_id': 7, 'chat': {'id': 42, 'type': 'private'}}}
    async def scenario():
        nonlocal entered, finish
        entered, finish = asyncio.Event(), asyncio.Event()
        first = asyncio.create_task(act(bot, callback))
        await entered.wait()
        await act(bot, {**callback, 'id': 'b'})
        assert actions == [42] and answered == [done_reply]
        finish.set()
        await first
        restarted = telegram_bot.TelegramBot('qa', runtime_paths=bot.runtime_paths)
        monkeypatch.setattr(restarted, '_answer_callback_query', answer)
        monkeypatch.setattr(restarted, '_profile_names', bot._profile_names)
        await act(restarted, {**callback, 'id': 'stale'})
        assert actions == [42] and answered == [done_reply] * 2
        other = deepcopy(callback)
        other['from']['id'] = 43
        await act(bot, other)
        assert actions == [42, 43]
    asyncio.run(scenario())


@pytest.mark.parametrize('failure', [False, True])
def test_hh_cache_single_flight_ttl_manual_refresh_and_user_isolation(menu_bot, monkeypatch, failure):
    bot, _ = menu_bot
    clock = [100.0]
    monkeypatch.setattr(hh_counter_cache, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    entered, finish = threading.Event(), threading.Event()
    calls = []
    def refresh(**kwargs):
        calls.append(True)
        entered.set()
        assert finish.wait(5)
        if failure:
            raise RuntimeError('synthetic')
        return {'total': 7}
    monkeypatch.setattr(telegram_bot.hh_response_counter, 'refresh', refresh)
    async def get(uid=42, force=False):
        return await hh_counter_cache.snapshot(bot, uid, 'qa', bot._profile('qa'), force=force)
    async def scenario():
        try:
            first = asyncio.create_task(get())
            await spin_until(entered.is_set)
            # Even after 60 seconds an unfinished request cannot be duplicated.
            clock[0] += 61
            repeated = await asyncio.gather(*(get(force=True) for _ in range(12)))
            assert calls == [True] and all(r[2] == guards.BUSY for r in repeated)
            first.cancel()
            with pytest.raises(asyncio.CancelledError):
                await first
            await get(force=True)
            assert calls == [True]
        finally:
            finish.set()
        await spin_until(lambda: bot._hh_counter_cache[next(iter(bot._hh_counter_cache))]['task'].done())
        # Force refresh permitted at >=60s from the preceding attempt.
        await get(force=True)
        assert len(calls) == 2
        await asyncio.gather(*(get(force=True) for _ in range(12)))
        assert len(calls) == 2
        clock[0] += 59
        await get(force=True)
        assert len(calls) == 2
        clock[0] += 1
        if not failure:
            _, age, _ = await get()
            assert age == 1 and len(calls) == 2  # ordinary reads cache for 3 minutes
        await get(force=True)
        assert len(calls) == 3
        await get(uid=43)
        assert len(calls) == 4  # no global cooldown
        if not failure:
            clock[0] += 180
            await get()
            assert len(calls) == 5
    asyncio.run(scenario())


def test_hh_age_visible_in_real_dispatch(menu_bot, monkeypatch):
    bot, _ = menu_bot
    clock = [100.0]
    monkeypatch.setattr(hh_counter_cache, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    calls = []
    monkeypatch.setattr(telegram_bot.hh_response_counter, 'refresh', lambda **kw: calls.append(True) or {'total': 7})
    principal = {'user_id': 42, 'role': 'user', 'profile': 'qa'}
    async def scenario():
        await bot._dispatch(42, principal, '/hh_responses', '')
        clock[0] += 120
        await bot._dispatch(42, principal, '/hh_response_count', '')
        assert calls == [True]
        assert 'обновлено 2 мин назад' in bot._send_text.call_args.args[1]
    asyncio.run(scenario())


@pytest.mark.parametrize('data,done_reply', [('manual_apply:qa:token', 'Карточка: ждёт решения.'),
                                           ('chat_ai_any:qa:123:456', guards.BUSY)])
def test_real_callback_double_tap_only_one_queue_entry_and_capture(monkeypatch, tmp_path, data, done_reply):
    from tests.test_telegram_command_queue import pending_card_lookup
    if data.startswith('manual_apply:'):
        pending_card_lookup(monkeypatch)
    import runtime_control
    import chat_context
    monkeypatch.setattr(chat_context, 'safe_resolve', lambda *args: SimpleNamespace(
        status=chat_context.OWN, resume_id='1', direction='', owner_profile=''))
    async def scenario():
        bot, _, _, _ = real_bot(monkeypatch, tmp_path)
        monkeypatch.setattr(bot, '_answer_callback_query', AsyncMock())
        monkeypatch.setattr(bot, '_authorize_ai_usage', AsyncMock(return_value=True))
        finish = asyncio.Event()
        captures = []
        async def capture(argv, **kwargs):
            captures.append(argv)
            await finish.wait()
            return {'ok': False, 'stdout': '', 'stderr': ''}
        monkeypatch.setattr(runtime_control, 'run_command_capture', capture)
        active = bot._mark_active_command(principal={'user_id': 1}, label='blocker', profile_name='qa')
        active.task = SimpleNamespace(done=lambda: False)
        callback = source('token')
        callback['data'] = data
        await asyncio.gather(bot._handle_callback_query(callback), bot._handle_callback_query({**callback, 'id': 'b'}))
        assert bot._queue_length('qa') == 1
        assert any(c.args[1:] == (done_reply,) for c in bot._answer_callback_query.call_args_list)
        bot._clear_active_command('qa')
        await spin_until(lambda: len(captures) == 1)
        await bot._handle_callback_query({**callback, 'id': 'c'})
        assert bot._queue_length('qa') == 0 and len(captures) == 1
        finish.set()
        await spin_until(lambda: not bot._active_commands and not bot._queue_drainers)
        await bot._shutdown_command_queue()
    asyncio.run(scenario())


def test_other_read_command_during_stats_has_own_slot(menu_bot, monkeypatch):
    bot, _ = menu_bot
    entered, finish = threading.Event(), threading.Event()
    def report(*args):
        entered.set()
        assert finish.wait(5)
        return 'synthetic', ['stats complete'], None
    monkeypatch.setattr(history, '_history_report', report)
    monkeypatch.setattr(history, '_run_report', lambda *args: ('log complete', None, None))
    principal = {'user_id': 42, 'role': 'user', 'profile': 'qa'}
    async def scenario():
        stats = asyncio.create_task(bot._dispatch(42, principal, '/stats', ''))
        try:
            await spin_until(entered.is_set)
            await asyncio.wait_for(bot._dispatch(42, principal, '/log', ''), 1)
            assert bot._send_text.call_args.args[1] == 'log complete'
        finally:
            finish.set()
            await stats
    asyncio.run(scenario())


def test_finishing_old_launch_cannot_release_new_preview_admission(monkeypatch, tmp_path):
    """A fast runner can finish while its original markup request still awaits."""
    import chat_context
    import runtime_control
    # The actor and private delivery chat must both belong to this profile.
    for uid in (1, 42):
        telegram_bot.telegram_access.upsert_user(uid, role='admin', profile='qa')
    monkeypatch.setattr(chat_context, 'safe_resolve', lambda *args: SimpleNamespace(
        status=chat_context.OWN, resume_id='1', direction='', owner_profile=''))
    async def scenario():
        bot, _, _, _ = real_bot(monkeypatch, tmp_path)
        monkeypatch.setattr(bot, '_authorize_ai_usage', AsyncMock(return_value=True))
        monkeypatch.setattr(bot, '_answer_callback_query', AsyncMock())
        outer_wait, release_outer, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
        edits, captures = [], []
        async def edit(*args, **kwargs):
            edits.append(True)
            if len(edits) == 2:
                outer_wait.set()
                await release_outer.wait()
        async def capture(argv, **kwargs):
            captures.append(argv)
            if len(captures) > 1:
                await finish.wait()
            return {'ok': False, 'stdout': '', 'stderr': ''}
        monkeypatch.setattr(bot, '_edit_reply_markup', edit)
        monkeypatch.setattr(runtime_control, 'run_command_capture', capture)
        callback = source('token')
        callback['data'] = 'chat_ai_any:qa:123:456'
        first = asyncio.create_task(bot._handle_callback_query(callback))
        await asyncio.wait_for(outer_wait.wait(), 2)
        await spin_until(lambda: not bot._active_commands)
        await bot._handle_callback_query({**callback, 'id': 'second'})
        assert bot._queue_length('qa') == 1
        release_outer.set()
        await first
        await bot._handle_callback_query({**callback, 'id': 'third'})
        assert bot._answer_callback_query.call_args.args[1] == guards.BUSY
        assert bot._queue_length('qa') <= 1
        await spin_until(lambda: len(captures) == 2)
        assert bot._queue_length('qa') == 0
        finish.set()
        await spin_until(lambda: not bot._active_commands and not bot._queue_drainers)
        await bot._shutdown_command_queue()
    asyncio.run(scenario())


def test_hh_profile_switch_cannot_bypass_user_refresh_limit(menu_bot, monkeypatch):
    bot, _ = menu_bot
    clock = [100.0]
    monkeypatch.setattr(hh_counter_cache, 'time', SimpleNamespace(monotonic=lambda: clock[0]))
    calls = []
    monkeypatch.setattr(telegram_bot.hh_response_counter, 'refresh', lambda **kw: calls.append(kw['profile_name']) or {'total': 7})
    async def scenario():
        await hh_counter_cache.snapshot(bot, 42, 'qa', bot._profile('qa'))
        await hh_counter_cache.snapshot(bot, 42, 'other', bot._profile('qa'), force=True)
        assert calls == ['qa']
        # A different user can refresh immediately, even in the same profile.
        await hh_counter_cache.snapshot(bot, 43, 'other', bot._profile('qa'), force=True)
        assert calls == ['qa', 'other']
        clock[0] += 60
        await hh_counter_cache.snapshot(bot, 42, 'other', bot._profile('qa'), force=True)
        assert calls == ['qa', 'other', 'other']
    asyncio.run(scenario())
