"""Real flock contention, synthetic files and finite safety releases only."""
import asyncio
from contextlib import contextmanager
import fcntl
import logging
import threading
import time
from unittest.mock import AsyncMock

import pytest

import analytics
import telegram_bot
from private_logging import PrivateFileHandler
from state_store.json_store import JsonStore
from state_store.private_journal import append_json, read_json_records
from state_store.locking import StateLockBusy, lock_wait_timeout, nonblocking_state_locks
from telegram_app.updates import STATE_BUSY
from telegram_app.guards import read_thread
from tests.test_telegram_polling_health import bot
from tests.test_telegram_menu_routing import menu_bot


@contextmanager
def held_lock(path, hold_seconds=2):
    ready, release, unlocked = threading.Event(), threading.Event(), threading.Event()

    def holder():
        with path.open('a+') as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            ready.set()
            # Baseline blocking code is released even if its event loop freezes.
            release.wait(hold_seconds)
        unlocked.set()

    thread = threading.Thread(target=holder)
    thread.start()
    assert ready.wait(1)
    try:
        yield unlocked
    finally:
        release.set()
        thread.join(timeout=3)
        assert not thread.is_alive()


def test_configured_logging_cannot_freeze_update_supervisor(bot, tmp_path, monkeypatch):
    path = tmp_path / 'contended.log'
    sink = PrivateFileHandler(path, channel='bot')
    installed = []
    monkeypatch.setattr(telegram_bot, '_build_logging_handlers', lambda *a: [sink])
    monkeypatch.setattr(logging, 'basicConfig', lambda **kwargs: installed.extend(kwargs['handlers']))
    telegram_bot._configure_logging(force=True)
    logger = logging.getLogger('telegram_app.updates')
    previous = logger.handlers, logger.level, logger.propagate
    logger.handlers, logger.propagate = installed, False
    logger.setLevel(logging.INFO)
    handled = []

    async def handle(update):
        handled.append(update['update_id'])
        await asyncio.sleep(0)

    monkeypatch.setattr(bot, '_handle_update', handle)

    async def scenario(unlocked):
        await bot._run_update({'update_id': 1})
        await bot._run_update({'update_id': 2})
        assert handled == [1, 2]
        assert not unlocked.is_set(), 'Logging held the event loop until the log lock was released'

    try:
        with held_lock(path.with_name('.contended.log.lock')) as unlocked:
            asyncio.run(scenario(unlocked))
    finally:
        logger.handlers, logger.level, logger.propagate = previous
        for handler in installed:
            handler.close()


@pytest.mark.parametrize('operation', ['state_sidecar', 'journal_read', 'journal_append', 'analytics_read'])
@pytest.mark.parametrize('callback', [False, True])
def test_contended_state_lock_fails_closed_and_next_update_runs(bot, tmp_path, monkeypatch, caplog, operation, callback):
    path = tmp_path / 'state.json'
    if operation == 'state_sidecar':
        store = JsonStore(path)
        store.save({'private': 'original'})
        lock = path.with_name('.state.json.lock')
        action = lambda: store.update(lambda state: {'changed': True})
    else:
        path.write_text('{"event":"synthetic"}\n')
        if operation == 'journal_append':
            lock = path.with_name('.state.json.lock')
            action = lambda: append_json(path, {'changed': True})
        elif operation == 'analytics_read':
            lock = path
            monkeypatch.setattr(analytics, '_destination', lambda: (str(path), '', ''))
            action = lambda: analytics._reconcile_event_tail({})
        else:
            lock = path
            action = lambda: read_json_records(path)
    original = path.read_bytes()
    handled = []

    async def handle(update):
        if update['update_id'] == 1:
            action()
            pytest.fail('Contended state must never be accessed without the lock')
        handled.append(update['update_id'])

    async def scenario(unlocked):
        message = {'chat': {'id': 42}}
        update = {'update_id': 1, 'message': message}
        if callback:
            update = {'update_id': 1, 'callback_query': {'id': 'click', 'message': message}}
        await bot._run_update(update)
        await bot._run_update({'update_id': 2})
        assert handled == [2]
        assert not unlocked.is_set(), 'State lock blocked the event loop'
        if callback:
            bot._answer_callback_query.assert_awaited_once_with('click', STATE_BUSY)
        bot._send_text.assert_awaited_once_with(42, STATE_BUSY)

    monkeypatch.setattr(bot, '_handle_update', handle)
    monkeypatch.setattr(bot, '_send_text', AsyncMock())
    monkeypatch.setattr(bot, '_answer_callback_query', AsyncMock())
    with caplog.at_level(logging.ERROR), held_lock(lock) as unlocked:
        asyncio.run(scenario(unlocked))
    assert 'StateLockBusy' in caplog.text
    assert path.read_bytes() == original


@pytest.mark.parametrize('offload', [asyncio.to_thread, read_thread])
def test_bot_workers_wait_for_brief_real_contention(tmp_path, offload):
    path = tmp_path / 'state.json'
    store = JsonStore(path)
    store.save({'original': True})

    @nonblocking_state_locks
    async def scenario():
        with held_lock(path.with_name('.state.json.lock'), hold_seconds=0.15):
            # Child tasks inherit the worker budget, not a zero-wait policy.
            task = asyncio.create_task(offload(store.load))
            assert await task == {'original': True}
    asyncio.run(scenario())


def test_worker_budget_expires_and_event_loop_keeps_running(tmp_path):
    path = tmp_path / 'state.json'
    store = JsonStore(path)
    store.save({})

    @nonblocking_state_locks
    async def scenario():
        ticks = 0
        task = asyncio.create_task(asyncio.to_thread(store.update, lambda state: {'changed': True}))
        started = time.monotonic()
        while not task.done():
            ticks += 1
            await asyncio.sleep(0.01)
        with pytest.raises(StateLockBusy):
            await task
        assert 0.45 <= time.monotonic() - started < 1.5
        assert ticks >= 5

    with held_lock(path.with_name('.state.json.lock')):
        asyncio.run(scenario())
    assert store.load() == {}


def test_event_loop_never_sleeps_even_with_positive_policy(tmp_path, monkeypatch):
    from state_store import locking
    from types import SimpleNamespace
    path = tmp_path / 'state.json'
    store = JsonStore(path)
    store.save({})
    def forbidden_sleep(_):
        pytest.fail('Event loop slept on flock')
    monkeypatch.setattr(locking, 'time', SimpleNamespace(monotonic=time.monotonic, sleep=forbidden_sleep))

    async def scenario():
        for budget in (0, 0.5):
            with lock_wait_timeout(budget), pytest.raises(StateLockBusy):
                store.load()
    with held_lock(path.with_name('.state.json.lock')):
        asyncio.run(scenario())


def test_unscoped_synchronous_caller_keeps_blocking_policy(tmp_path):
    path = tmp_path / 'state.json'
    store = JsonStore(path)
    store.save({'original': True})
    with held_lock(path.with_name('.state.json.lock'), hold_seconds=0.15) as unlocked:
        assert store.load() == {'original': True}
        assert unlocked.wait(1)


def test_queue_drainer_has_own_worker_policy(tmp_path, monkeypatch):
    from tests.test_telegram_command_queue import Bot, entry, settle
    path = tmp_path / 'state.json'
    store = JsonStore(path)
    store.save({})
    bot = Bot()
    calls = []
    def guard(profile):
        calls.append(store.load())
        return ''
    monkeypatch.setattr(bot, '_queue_guard_reason', guard)
    item = entry(bot)
    item.ready = True
    bot._command_queues['qa'] = [item]
    async def scenario():
        with held_lock(path.with_name('.state.json.lock'), hold_seconds=0.15), lock_wait_timeout(0):
            bot._schedule_queue('qa')
            await settle(bot)
        assert calls == [{}]
        assert bot.started == [('qa', item.label, True)]
    asyncio.run(scenario())


def test_active_command_has_own_worker_policy(tmp_path):
    from telegram_app.subprocesses import TelegramSubprocessManager
    path = tmp_path / 'state.json'
    store = JsonStore(path)
    store.save({})
    bot = TelegramSubprocessManager(admin_role='admin')
    result = []
    async def runner():
        result.append(await asyncio.to_thread(store.load))
    async def scenario():
        with held_lock(path.with_name('.state.json.lock'), hold_seconds=0.15), lock_wait_timeout(0):
            await bot._create_active_task(None, runner)
    asyncio.run(scenario())
    assert result == [{}]


def test_real_polling_router_replies_to_busy_message_and_callback_then_recovers(menu_bot, monkeypatch):
    from contextlib import ExitStack
    from pathlib import Path
    bot, _ = menu_bot
    monkeypatch.setattr(telegram_bot.config, 'TELEGRAM_CONTROL_BOT_TOKEN', 'synthetic')
    monkeypatch.setattr(telegram_bot.telegram_access, 'resolve_user',
                        lambda uid: {'user_id': uid, 'role': 'admin', 'profile': 'qa'})
    monkeypatch.setattr(telegram_bot.telegram_access, 'load_registry', lambda: {})
    monkeypatch.setattr(telegram_bot.runtime_control, 'register_current_process', lambda *a, **kw: 1)
    monkeypatch.setattr(telegram_bot.runtime_control, 'unregister_current_process', lambda *a: None)
    monkeypatch.setattr(bot, '_api_request', AsyncMock(return_value={}))
    monkeypatch.setattr(bot, '_maybe_send_daily_summary', AsyncMock())
    monkeypatch.setattr(bot, '_maybe_send_health_alert', AsyncMock())
    bot.drop_pending = False
    bot._save_state({'last_update_id': 0})
    original = Path(bot.runtime_paths.bot_state_file).read_bytes()
    message = {'from': {'id': 42}, 'chat': {'id': 42, 'type': 'private'}, 'text': '/help'}
    calls = []
    sent = []
    with ExitStack() as locks:
        async def poll():
            calls.append(True)
            if len(calls) == 1:
                path = Path(bot.runtime_paths.bot_state_file)
                locks.enter_context(held_lock(path.with_name(f'.{path.name}.lock')))
                return [{'update_id': 1, 'message': message},
                        {'update_id': 2, 'callback_query': {'id': 'click', 'from': {'id': 42},
                                                         'data': 'gs:confirm', 'message': message}}]
            assert Path(bot.runtime_paths.bot_state_file).read_bytes() == original
            locks.close()
            return [{'update_id': 3, 'message': message}]
        async def send(chat, text, **kwargs):
            sent.append(text)
            if text != STATE_BUSY:
                bot._stop_event.set()
            return {}
        monkeypatch.setattr(bot, '_get_updates', poll)
        monkeypatch.setattr(bot, '_send_text', send)
        async def scenario():
            monkeypatch.setattr(asyncio.get_running_loop(), 'add_signal_handler', lambda *a: None)
            await asyncio.wait_for(bot.run(), 1.5)
        asyncio.run(scenario())
    assert len(calls) == 2
    assert sent[0] == STATE_BUSY and len(sent) >= 2 and sent[-1] != STATE_BUSY
    assert any(c.args[0] == 'answerCallbackQuery' and c.args[1].get('text') == STATE_BUSY
               for c in bot._api_request.call_args_list)


def test_already_acknowledged_callback_still_gets_busy_chat_reply(bot, monkeypatch):
    from pathlib import Path
    from telegram_app.api import TelegramAPIError
    monkeypatch.setattr(telegram_bot.telegram_access, 'resolve_user',
                        lambda uid: {'user_id': uid, 'role': 'admin', 'profile': 'qa'})
    answers = []
    async def api(method, payload, **kwargs):
        assert method == 'answerCallbackQuery'
        answers.append(payload)
        if len(answers) > 1:
            raise TelegramAPIError(method, 400)
        return {}
    async def direction(*args):
        bot._update_state(lambda state: state.update(changed=True))
    monkeypatch.setattr(bot, '_api_request', api)
    monkeypatch.setattr(bot, '_direction_callback', direction)
    monkeypatch.setattr(bot, '_send_text', AsyncMock())
    bot._save_state({'original': True})
    path = Path(bot.runtime_paths.bot_state_file)
    update = {'update_id': 1, 'callback_query': {
        'id': 'click', 'from': {'id': 42}, 'data': 'dir:token:editmenu',
        'message': {'chat': {'id': 42, 'type': 'private'}}}}
    with held_lock(path.with_name(f'.{path.name}.lock')):
        asyncio.run(bot._run_update(update))
    assert 'text' not in answers[0] and answers[1]['text'] == STATE_BUSY
    bot._send_text.assert_awaited_once_with(42, STATE_BUSY)
    assert bot._load_state() == {'original': True}


def test_logging_queue_overload_is_bounded_and_observable():
    from private_logging import NonblockingLogHandler
    entered, release = threading.Event(), threading.Event()
    messages = []

    class SlowSink(logging.Handler):
        def emit(self, record):
            entered.set()
            release.wait(2)
            messages.append(record.getMessage())

    handler = NonblockingLogHandler([SlowSink()], capacity=2)
    record = logging.LogRecord('telegram_bot', logging.INFO, '', 0, 'marker', (), None)
    try:
        handler.handle(record)
        assert entered.wait(1)
        for _ in range(100):
            handler.handle(record)
        assert handler.records.qsize() <= 2
        assert handler.dropped_records == 98
    finally:
        release.set()
        handler.close()
    assert any('dropped 98 records' in message for message in messages)
    assert not handler._worker.is_alive()


def test_queued_logs_capture_context_and_redact_secrets(tmp_path):
    from private_logging import NonblockingLogHandler
    path = tmp_path / 'chat.log'
    handler = NonblockingLogHandler([PrivateFileHandler(path, channel='chat')])
    with analytics.event_context(channel='chat', run_id='synthetic-run', source='hh'):
        handler.handle(logging.LogRecord('synthetic-client', logging.WARNING, '', 0,
                                        'request https://auth.invalid/private-marker', (), None))
    handler.close()
    text = path.read_text()
    assert 'run=synthetic-run source=hh' in text
    assert 'private-marker' not in text
    assert '[url]' in text
