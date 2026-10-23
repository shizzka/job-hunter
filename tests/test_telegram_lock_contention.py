"""Real flock contention, synthetic files and finite safety releases only."""
import asyncio
from contextlib import contextmanager
import fcntl
import logging
import threading

import pytest

import analytics
import telegram_bot
from private_logging import PrivateFileHandler
from state_store.json_store import JsonStore
from state_store.private_journal import append_json, read_json_records
from tests.test_telegram_polling_health import bot


@contextmanager
def held_lock(path):
    ready, release, unlocked = threading.Event(), threading.Event(), threading.Event()

    def holder():
        with path.open('a+') as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            ready.set()
            # Baseline blocking code is released even if its event loop freezes.
            release.wait(2)
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
def test_contended_state_lock_fails_closed_and_next_update_runs(bot, tmp_path, monkeypatch, caplog, operation):
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
        await bot._run_update({'update_id': 1})
        await bot._run_update({'update_id': 2})
        assert handled == [2]
        assert not unlocked.is_set(), 'State lock blocked the event loop'

    monkeypatch.setattr(bot, '_handle_update', handle)
    with caplog.at_level(logging.ERROR), held_lock(lock) as unlocked:
        asyncio.run(scenario(unlocked))
    assert 'StateLockBusy' in caplog.text
    assert path.read_bytes() == original


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
