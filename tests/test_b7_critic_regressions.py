"""B7 critic: mailbox, historical logs, recipient scopes and account invitations."""
import asyncio
from datetime import datetime
import json
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import agent
import analytics
import config
import directions
import hh_auth_bridge as bridge
import invitation_sync
import notifier
import profile as profiles
import profile_access
import telegram_access
import telegram_bot
from telegram_app.auth_bridge import can_answer_hh_auth_prompt
from tests.direction_view_fakes import two_directions


@pytest.fixture
def scoped_bot(tmp_path, monkeypatch):
    monkeypatch.setenv('JOB_HUNTER_HOME', str(tmp_path))
    monkeypatch.setattr(config, 'JOB_HUNTER_HOME', str(tmp_path))
    monkeypatch.setattr(config, 'JOB_HUNTER_INSTANCE_KIND', 'dev')
    monkeypatch.setattr(config, 'NOTIFY_CHAT_ID', 0)
    monkeypatch.setattr(config, 'TELEGRAM_ACCESS_FILE', str(tmp_path / 'telegram-users.json'))
    monkeypatch.setattr(config, 'HH_AUTH_BRIDGE_DIR', str(tmp_path / 'mailbox'))
    home = tmp_path / 'profiles' / 'qa-dev'
    home.mkdir(parents=True)
    (home / 'profile.env').write_text('HH_ENABLED=0\n')
    two_directions(home)
    profile = profiles.load_profile('qa-dev')
    monkeypatch.setattr(profiles, '_active_profile', profile)
    monkeypatch.setattr(config, 'JOB_HUNTER_HOME', profile.home_dir)
    monkeypatch.setattr(config, 'ACTIVE_DIRECTION', profile.direction, raising=False)
    monkeypatch.setattr(config, 'ACTIVE_DIRECTION_NAME', profile.direction_name, raising=False)
    for key, attr in [('ANALYTICS_EVENTS_FILE', 'analytics_events_file'),
                      ('ANALYTICS_STATE_FILE', 'analytics_state_file'), ('RUN_HISTORY_FILE', 'run_history_file')]:
        monkeypatch.setattr(config, key, getattr(profile, attr))
    tester = telegram_access.upsert_user(202, profile='qa-dev', role='tester', direction='qa')
    bot = telegram_bot.TelegramBot('qa-dev')
    bot._send_text = AsyncMock(return_value={})
    bot._send_text_safely = AsyncMock(return_value={})
    bot._answer_callback_query = AsyncMock()
    return bot, tester, profile


@pytest.mark.parametrize('kind,allowed,forbidden', [('dev', 'qa-dev', 'qa'), ('prod', 'qa', 'qa-dev')])
@pytest.mark.parametrize('expired', [False, True])
def test_mailbox_discovery_and_cached_operations_respect_instance(tmp_path, monkeypatch, kind, allowed, forbidden, expired):
    monkeypatch.setattr(config, 'HH_AUTH_BRIDGE_DIR', str(tmp_path / 'mailbox'))
    ids = {}
    for mode, name in [('dev' if forbidden.endswith('-dev') else 'prod', forbidden), (kind, allowed)]:
        monkeypatch.setattr(config, 'JOB_HUNTER_INSTANCE_KIND', mode)
        ids[name] = bridge.create_request(kind='code', profile_name=name, prompt='synthetic code')
    monkeypatch.setattr(config, 'JOB_HUNTER_INSTANCE_KIND', kind)
    if expired:
        path = tmp_path / 'mailbox' / f'hh_auth_pending.{forbidden}.json'
        payload = json.loads(path.read_text())
        payload['timeout_at'] = time.time() - 1
        path.write_text(json.dumps(payload))
    forbidden_bytes = (tmp_path / 'mailbox' / f'hh_auth_pending.{forbidden}.json').read_bytes()
    pending = bridge.peek_pending()
    assert pending['profile_name'] == allowed
    assert (tmp_path / 'mailbox' / f'hh_auth_pending.{forbidden}.json').read_bytes() == forbidden_bytes
    assert not can_answer_hh_auth_prompt({'role': 'admin'}, {'profile_name': forbidden})
    before = {p: p.read_bytes() for p in (tmp_path / 'mailbox').iterdir() if p.is_file()}
    for explicit in ('', forbidden):
        with pytest.raises(PermissionError):
            bridge.write_response(ids[forbidden], '1234', profile_name=explicit)
        with pytest.raises(PermissionError):
            bridge.complete_request(ids[forbidden], profile_name=explicit)
        with pytest.raises(PermissionError):
            asyncio.run(bridge.wait_for_response(ids[forbidden], profile_name=explicit))
    with pytest.raises(PermissionError):
        bridge.peek_pending(forbidden)
    assert all(p.read_bytes() == content for p, content in before.items())
    bridge.write_response(ids[allowed], '1234')
    assert asyncio.run(bridge.wait_for_response(ids[allowed])) == '1234'
    # Discovery with no in-process cache must also exclude the other mailbox.
    monkeypatch.setattr(bridge, '_request_channels', {})
    with pytest.raises(ValueError):
        bridge.write_response(ids[forbidden], '1234')
    assert bridge.peek_pending() is None


@pytest.mark.parametrize('kind,forbidden', [('dev', 'qa'), ('prod', 'qa-dev')])
def test_admin_text_does_not_answer_other_instance_mailbox(scoped_bot, monkeypatch, kind, forbidden):
    bot, _, _ = scoped_bot
    monkeypatch.setattr(config, 'JOB_HUNTER_INSTANCE_KIND', 'dev' if forbidden.endswith('-dev') else 'prod')
    request_id = bridge.create_request(kind='code', profile_name=forbidden, prompt='synthetic')
    monkeypatch.setattr(config, 'JOB_HUNTER_INSTANCE_KIND', kind)
    assert not asyncio.run(bot._maybe_accept_hh_auth_response(101, {'role': 'admin'}, '1234'))
    monkeypatch.setattr(config, 'JOB_HUNTER_INSTANCE_KIND', 'dev' if forbidden.endswith('-dev') else 'prod')
    assert bridge.peek_pending(forbidden)['id'] == request_id


def test_tester_raw_and_legacy_chat_logs_are_not_disclosed(scoped_bot):
    bot, tester, profile = scoped_bot
    Path(profile.log_file).write_text('qa ordinary line\nelectric SECRET line\n')
    asyncio.run(bot._dispatch(202, tester, '/raw_log', ''))
    assert 'Сырой лог недоступен' in bot._send_text.call_args.args[1]
    assert 'SECRET' not in str(bot._send_text.call_args_list)
    # Even a legacy line with a QA chat id may contain unverified raw text.
    from telegram_app import history
    from direction_history import HistoryScope
    with profile_access.principal_scope(tester):
        assert history.chat_rows(profile.home_dir, 'chat_id=1 SECRET legacy') == []
        journal = Path(profile.home_dir) / 'chat_journal.jsonl'
        journal.write_text('\n'.join(json.dumps(row) for row in [
            {'event': 'chat_decision', 'chat_id': '1', 'direction': 'qa', 'decision': 'replied'},
            {'event': 'chat_decision', 'chat_id': '2', 'direction': 'electric', 'decision': 'rejected'}]))
        text = history.chat_text(profile.home_dir, HistoryScope(profile.home_dir), 'SECRET legacy')
        assert 'чат 1' in text and 'чат 2' not in text and 'SECRET' not in text


def test_daily_summary_uses_each_recipient_scope(scoped_bot, monkeypatch):
    bot, tester, profile = scoped_bot
    telegram_access.upsert_user(303, profile='qa-dev')
    monkeypatch.setattr(bot, '_daily_summary_due', lambda: (True, 'synthetic-day'))
    now = datetime.now().isoformat()
    Path(profile.analytics_events_file).write_text('\n'.join(json.dumps({
        'event': 'decision', 'decision': 'applied_auto', 'vacancy_id': str(i),
        'source': 'hh', 'created_at': now, 'direction': direction,
    }) for i, direction in [(1, 'qa'), (2, 'electric'), (3, 'electric')]))
    # A direction change must not broadcast that direction's summary to tester.
    directions.set_active_direction(profile.home_dir, 'electric')
    asyncio.run(bot._maybe_send_daily_summary())
    assert [call.args[0] for call in bot._send_text_safely.call_args_list] == [303]
    directions.set_active_direction(profile.home_dir, 'qa')
    bot._send_text_safely.reset_mock()
    asyncio.run(bot._maybe_send_daily_summary())
    own = next(call.args[1] for call in bot._send_text_safely.call_args_list if call.args[0] == 202)
    assert 'Откликов: 1' in own
    assert 'Откликов: 3' not in own
    assert not profile_access.current_scope()[0]


@pytest.mark.parametrize('direction,expected', [('qa', [202]), ('electric', []), (None, [])])
@pytest.mark.parametrize('active', ['qa', 'electric'])
def test_worker_notification_requires_event_ownership(scoped_bot, monkeypatch, direction, expected, active):
    _, _, profile = scoped_bot
    directions.set_active_direction(profile.home_dir, active)
    monkeypatch.setattr(config, 'TELEGRAM_BOT_TOKEN', 'synthetic-token')
    delivered = []
    async def deliver(url, payload, **kwargs):
        delivered.append(payload['chat_id'])
        return True
    monkeypatch.setattr(notifier, '_deliver', deliver)
    event = {'title': 'synthetic invitation', 'direction': direction} if direction else {'title': 'unowned'}
    asyncio.run(notifier.notify_invitation(event))
    assert delivered == expected
    # A configured fallback and an explicitly pinned target cannot bypass it.
    if direction != 'qa':
        profile.notify.chat_id = 202
        assert notifier._resolve_target_chat_ids(profile) == []
        result = asyncio.run(notifier._send_to_chats('sendMessage', notifier._build_text_payload('private', 'HTML', None),
                                                    target=('synthetic-token', (202,), '')))
        assert result is False and delivered == []


def _account_invitations(profile):
    # The DOM exposes vacancy identity only. Saved application-resume evidence
    # binds it to QA/electric; a third vacancy has no ownership evidence.
    Path(profile.home_dir, 'negotiation_snapshots.jsonl').write_text('\n'.join(json.dumps(row) for row in [
        {'vacancyId': 11, 'resumeId': 111}, {'vacancyId': 22, 'resumeId': 222}]))
    return [{'title': title, 'company': 'synthetic', 'url': f'https://hh.ru/vacancy/{number}', 'status': 'invitation'}
            for number, title in [(11, 'QA only'), (22, 'electric private'), (33, 'unknown private')]]


@pytest.mark.parametrize('scope_source', ['request', 'worker_environment'])
def test_tester_check_filters_before_recording_and_notification(scoped_bot, monkeypatch, scope_source):
    bot, tester, profile = scoped_bot
    items = _account_invitations(profile)
    recorded = []
    monkeypatch.setattr(analytics, 'record_invitations', lambda rows: recorded.extend(rows))
    statuses = []
    monkeypatch.setattr(analytics, 'record_negotiation_statuses', lambda rows: statuses.extend(rows))
    monkeypatch.setattr(invitation_sync.hh_pipeline, 'enabled', lambda: False)
    fake = SimpleNamespace(start=AsyncMock(), stop=AsyncMock(), is_logged_in=AsyncMock(return_value=True),
                           get_negotiation_statuses=AsyncMock(return_value=items),
                           check_negotiations=AsyncMock(return_value={'invitations': items}))
    monkeypatch.setattr(agent, 'HHClient', lambda: fake)
    monkeypatch.setattr(agent, '_write_runtime_status', lambda *a: None)
    monkeypatch.setattr(agent, 'office_log', AsyncMock())
    monkeypatch.setattr(agent, 'create_task', lambda *a: None)
    monkeypatch.setattr(agent, 'notify_invitation', AsyncMock())
    # Exercise the real Telegram /check entry and its scoped worker boundary.
    async def start_cli(chat_id, principal, profile_name, *args):
        if scope_source == 'worker_environment':
            env = profile_access.worker_environment()
            for key in ('JOB_HUNTER_SCOPED_PROFILE', 'JOB_HUNTER_SCOPED_DIRECTION', 'JOB_HUNTER_SCOPED_USER'):
                monkeypatch.setenv(key, env[key])
            with profile_access.principal_scope(None):
                await agent.do_check_invitations()
        else:
            await agent.do_check_invitations()
    monkeypatch.setattr(bot, '_start_cli_command', start_cli)
    asyncio.run(bot._dispatch(202, tester, '/check', ''))
    assert [row['title'] for row in recorded] == ['QA only']
    assert [row['title'] for row in statuses] == ['QA only']
    assert [call.args[0]['title'] for call in agent.notify_invitation.call_args_list] == ['QA only']
    assert all(row['direction'] == 'qa' for row in recorded + statuses)
    assert fake.stop.await_count == 1
