"""Home restores the accessible inbox and the scheduler's actual local window."""
import asyncio
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import client_hh_auth
import manual_apply_queue as queue
import telegram_access
from tests.test_bot_profile_isolation import synthetic_profiles, dev_bot
from tests.test_telegram_callbacks import _callback


@pytest.fixture
def configured(dev_bot, monkeypatch):
    bot = dev_bot
    profile = bot._profile('qa-dev')
    cookie = Path(profile.hh.cookies_file)
    cookie.parent.mkdir(parents=True, exist_ok=True)
    cookie.write_text('[]')
    monkeypatch.setattr(client_hh_auth, 'load_hh_resume_catalog', lambda name: [{'id': 'resume_111'}])
    monkeypatch.setattr(bot, '_daemon_state', lambda name: {'running': False, 'pid': 0})
    return bot, profile


@pytest.mark.parametrize('role,selected,count', [('user', False, 3), ('user', True, 4), ('tester', True, 1)])
def test_start_and_review_expose_the_same_accessible_unresolved_groups(configured, role, selected, count):
    bot, profile = configured
    principal = telegram_access.upsert_user(202, profile='qa-dev', role=role, direction='qa')
    if selected:
        own = queue.create_candidate({'id': 'own', 'title': 'Selected card', 'source': 'hh'},
                                     {'score': 85}, profile_name='qa-dev')
        queue._store('qa-dev').update(lambda data: data['items'][own['token']].update(direction='qa'))
    for identity in ('unknown-one', 'unknown-two'):
        item = queue.create_candidate({'id': identity, 'title': identity, 'source': 'hh'},
                                      {'score': 70}, profile_name='qa-dev')
        if identity == 'unknown-two':
            queue._store('qa-dev').update(lambda data: data['items'][item['token']].update(status='uncertain'))
    other_search = queue.create_candidate({'id': 'other-search', 'title': 'Other search card', 'source': 'hh'},
                                     {'score': 90}, profile_name='qa-dev')
    queue._store('qa-dev').update(lambda data: data['items'][other_search['token']].update(direction='electric'))
    async def scenario():
        await bot._dispatch(202, principal, '/start', '')
        calls = bot._send_text.call_args_list
        assert f'📋 Ждут решения: {count}' in calls[0].args[1]
        button = next(b for call in calls for row in (call.kwargs.get('reply_markup') or {}).get('inline_keyboard', [])
                      for b in row if b['text'] == f'📋 Посмотреть {count}')
        await bot._handle_callback_query(_callback(button['callback_data'], user_id=202, chat_id=202,
                                                  chat_type='private'))
        text = bot._send_text.call_args.args[1]
        assert 'Всё разобрано' not in text
        if role == 'tester':
            assert 'Selected card' in text and 'unknown-' not in text and 'Other search card' not in text
        else:
            await bot._show_review_list(202, principal, profile_name='qa-dev')
            assert 'Other search card' in bot._send_text.call_args.args[1]
            assert 'Направление не указано: 2' in str(bot._send_text.call_args.kwargs)
            await bot._show_review_list(202, principal, profile_name='qa-dev', unspecified=True)
            unknown = bot._send_text.call_args.args[1]
            assert 'unknown-one' in unknown and 'unknown-two' in unknown
            assert 'Other search card' not in unknown and 'Selected card' not in unknown
    asyncio.run(scenario())


@pytest.mark.parametrize('state', ['missing', 'valid', 'different_pid', 'stale', 'different_interval', 'searching'])
def test_home_uses_scheduler_window_or_explicit_unknown(configured, monkeypatch, state):
    bot, profile = configured
    principal = telegram_access.upsert_user(202, profile='qa-dev', role='user')
    now = datetime.now(timezone.utc)
    due = now + timedelta(hours=3)
    schedule = {'pid': 4321, 'status': 'waiting', 'interval_min': profile.search_interval_min,
                'updated_at': now.isoformat(), 'next_search_at': due.isoformat()}
    if state == 'different_pid':
        schedule['pid'] = 4322
    elif state == 'stale':
        schedule['updated_at'] = (now - timedelta(hours=1)).isoformat()
    elif state == 'different_interval':
        schedule['interval_min'] += 1
    elif state == 'searching':
        schedule.update(status='searching', next_search_at=None)
    if state != 'missing':
        (Path(profile.home_dir) / 'daemon_schedule.json').write_text(json.dumps(schedule))
    monkeypatch.setattr(bot, '_daemon_state', lambda name: {'running': True, 'pid': 4321})
    asyncio.run(bot._dispatch(202, principal, '/start', ''))
    text = bot._send_text.call_args.args[1]
    assert 'в течение' not in text
    if state == 'valid':
        assert due.astimezone().strftime('%d.%m %H:%M') in text
    elif state == 'searching':
        assert 'поиск уже выполняется' in text
    else:
        assert 'время пока неизвестно' in text


@pytest.mark.parametrize('fresh_interval,fresh_enabled', [(0, False), (20, True), (20, False)])
def test_daemon_publishes_deadline_after_completed_search_and_clears_it_on_stop(tmp_path, monkeypatch,
                                                                            fresh_interval, fresh_enabled):
    import agent
    import daemon_schedule
    import run_auditor
    from types import SimpleNamespace
    from unittest.mock import AsyncMock
    clock = [100000.0]
    start = datetime(2026, 10, 26, 12, tzinfo=timezone.utc)
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            value = start + timedelta(seconds=clock[0] - 100000)
            return value.astimezone(tz) if tz else value
    monkeypatch.setattr(daemon_schedule, 'datetime', Clock)
    monkeypatch.setattr(agent.config, 'DAEMON_PID_FILE', str(tmp_path / 'daemon.pid'))
    monkeypatch.setattr(agent.config, 'SEARCH_INTERVAL_MIN', 480)
    monkeypatch.setattr(agent.config, 'HH_FRESH_SEARCH_INTERVAL_MIN', fresh_interval)
    monkeypatch.setattr(agent.config, 'HH_FRESH_SEARCH_ENABLED', fresh_enabled)
    monkeypatch.setattr(agent.config, 'HH_ENABLED', True)
    monkeypatch.setattr(agent.config, 'INVITE_CHECK_INTERVAL_MIN', 60)
    monkeypatch.setattr(agent.runtime_control, 'register_current_process', lambda *a, **kw: None)
    monkeypatch.setattr(agent.runtime_control, 'unregister_current_process', lambda *a, **kw: None)
    monkeypatch.setattr(agent, '_write_runtime_status', lambda *a, **kw: None)
    monkeypatch.setattr(agent, '_audit_destination', lambda: {})
    monkeypatch.setattr(run_auditor, 'enqueue_daily_summary', lambda **kw: False)
    monkeypatch.setattr(agent, 'office_log', AsyncMock())
    monkeypatch.setattr(agent, 'do_check_invitations', AsyncMock())
    async def search():
        state = json.loads((tmp_path / 'daemon_schedule.json').read_text())
        assert state['status'] == 'searching' and state['next_search_at'] is None
        clock[0] += 120
    monkeypatch.setattr(agent, 'do_search', search)
    observed = []
    async def scenario():
        loop = asyncio.get_running_loop()
        monkeypatch.setattr(loop, 'add_signal_handler', lambda *a: None)
        monkeypatch.setattr(agent.asyncio, 'get_event_loop', lambda: SimpleNamespace(
            time=lambda: clock[0], add_signal_handler=lambda *a: None))
        async def wait(coroutine, *, timeout):
            state = json.loads((tmp_path / 'daemon_schedule.json').read_text())
            observed.append(state)
            event = coroutine.cr_frame.f_locals['self']
            event.set()
            return await coroutine
        monkeypatch.setattr(agent.asyncio, 'wait_for', wait)
        await agent.do_daemon()
    asyncio.run(scenario())
    assert len(observed) == 1
    state = observed[0]
    assert state['status'] == 'waiting' and state['interval_min'] == 480
    minutes = fresh_interval if fresh_enabled and fresh_interval else 480
    assert datetime.fromisoformat(state['next_search_at']) == start + timedelta(seconds=120, minutes=minutes)
    stopped = json.loads((tmp_path / 'daemon_schedule.json').read_text())
    assert stopped['status'] == 'stopped' and stopped['next_search_at'] is None
