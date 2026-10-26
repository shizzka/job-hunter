"""Deferred Telegram delivery rechecks the registry at every output boundary."""
import asyncio
from types import SimpleNamespace

import pytest

import config
import profile_access
import telegram_access
from telegram_bot import TelegramBot
from telegram_app.command_queue import QueueEntry


@pytest.fixture
def delivery(monkeypatch):
    monkeypatch.setattr(config, 'JOB_HUNTER_INSTANCE_KIND', 'dev')
    monkeypatch.setattr(config, 'NOTIFY_CHAT_ID', 0)
    users = []
    monkeypatch.setattr(telegram_access, 'load_registry', lambda: {'users': users})
    # Synthetic prepared directions; retain real instance/profile/binding checks.
    def prepared(principal):
        profile, direction = profile_access.tester_binding(principal)
        profile_access.require_profile(profile)
        if direction not in {'alpha', 'beta'}:
            raise PermissionError('direction missing')
        return profile, direction
    monkeypatch.setattr(profile_access, 'require_tester_binding', prepared)
    bot = TelegramBot.__new__(TelegramBot)
    bot._init_command_queue()
    bot._active_commands = {}
    bot.profile_name = 'sample-dev'
    monkeypatch.setattr(bot, '_profile', lambda name: SimpleNamespace(notify=SimpleNamespace(chat_id=99)))
    messages = []
    async def api(method, payload, **kwargs):
        messages.append((method, payload))
        return {'message_id': len(messages)}
    monkeypatch.setattr(bot, '_api_request', api)
    return bot, users, messages


def principal(uid=1, *, profile='sample-dev', role='user', direction='alpha', enabled=True):
    return dict(user_id=uid, profile=profile, role=role, direction=direction, enabled=enabled)


def queued(uid=1, *, chat=None, direction='alpha', label='private task'):
    item = QueueEntry('sample-dev', chat or uid, uid, label, None,
                      source={'data': 'manual_apply:sample-dev:token',
                              'message': {'message_id': 4, 'reply_markup': {'inline_keyboard': []}}})
    # Attribute assignment also makes the pre-fix tests reach the vulnerable send.
    item.direction = direction
    return item


@pytest.mark.parametrize('binding', [None, principal(99, profile='sample'), principal(99, enabled=False)])
def test_configured_fallback_requires_live_matching_binding(delivery, binding):
    bot, users, _ = delivery
    if binding:
        users.append(binding)
    assert bot._profile_recipient_ids('sample-dev') == []


@pytest.mark.parametrize('role', ['admin', 'user', 'tester'])
def test_same_profile_registered_recipients_receive_compatible_pause(delivery, role):
    bot, users, messages = delivery
    users.append(principal(role=role))
    bot._command_queues['sample-dev'] = [queued()]
    asyncio.run(bot._pause_queue('sample-dev', 'reason'))
    assert [payload['chat_id'] for _, payload in messages] == [1]


@pytest.mark.parametrize('binding', [None, principal(enabled=False), principal(profile='other-dev'),
    principal(profile='sample'), principal(role='tester', direction='beta'),
    principal(role='tester', direction='missing')])
def test_queued_recipient_binding_is_rechecked_before_pause(delivery, binding):
    bot, users, messages = delivery
    if binding:
        users.append(binding)
    bot._command_queues['sample-dev'] = [queued()]
    asyncio.run(bot._pause_queue('sample-dev', 'private reason'))
    assert messages == []


def test_pause_rechecks_later_recipient_after_await(delivery, monkeypatch):
    bot, users, messages = delivery
    users.extend([principal(1), principal(2)])
    async def send(method, payload, **kwargs):
        messages.append((method, payload))
        users[1]['enabled'] = False
        return {}
    monkeypatch.setattr(bot, '_api_request', send)
    asyncio.run(bot._pause_queue('sample-dev', 'reason'))
    assert [payload['chat_id'] for _, payload in messages] == [1]


@pytest.mark.parametrize('directions', [[], ['alpha', 'beta'], ['alpha', '']])
def test_tester_does_not_receive_unproved_shared_queue_pause(delivery, directions):
    bot, users, messages = delivery
    users.append(principal(role='tester'))
    bot._command_queues['sample-dev'] = [queued(direction=name) for name in directions]
    asyncio.run(bot._pause_queue('sample-dev', 'private reason'))
    assert messages == []


def test_tester_summary_only_contains_currently_authorized_entries(delivery):
    bot, users, messages = delivery
    users.append(principal(role='tester'))
    bot._queue_history['sample-dev'] = [queued(label='allowed'), queued(direction='beta', label='foreign secret')]
    asyncio.run(bot._queue_summary('sample-dev'))
    assert len(messages) == 1
    assert 'allowed' in messages[0][1]['text']
    assert 'foreign secret' not in messages[0][1]['text']
    assert 'задач: 1' in messages[0][1]['text']


def test_revoked_recipient_gets_neither_summary_nor_restored_markup(delivery):
    bot, users, messages = delivery
    users.append(principal(enabled=False))
    item = queued()
    bot._queue_history['sample-dev'] = [item]
    async def scenario():
        await bot._queue_summary('sample-dev')
        await bot._queue_markup(item, restore=True)
    asyncio.run(scenario())
    assert messages == []


def test_multichunk_queue_notice_stops_after_revocation(delivery, monkeypatch):
    bot, users, messages = delivery
    users.append(principal())
    item = queued()
    item.label = 'private\n' * 1500
    bot._queue_history['sample-dev'] = [item]
    async def send(method, payload, **kwargs):
        messages.append((method, payload))
        users[0]['enabled'] = False
        return {}
    monkeypatch.setattr(bot, '_api_request', send)
    asyncio.run(bot._queue_summary('sample-dev'))
    assert len(messages) == 1


def test_guest_direct_reply_remains_available(delivery):
    bot, _, messages = delivery
    asyncio.run(bot._send_text(123, 'welcome'))
    assert messages[0][1]['text'] == 'welcome'


@pytest.mark.parametrize('change', [None, 'revoke', 'profile', 'direction'])
def test_started_job_rechecks_binding_after_capture_await(delivery, change):
    bot, users, messages = delivery
    users.append(principal(role='tester'))
    tasks = []
    async def scenario():
        captured = asyncio.Event()
        async def runner():
            await captured.wait()
            await bot._send_text(1, 'private captured result')
        async def invoke(revalidate):
            tasks.append(asyncio.create_task(runner()))
        entry = queued()
        entry.invoke = invoke
        await bot._invoke_queue_entry(entry, revalidate=False)
        if change == 'revoke':
            users[0]['enabled'] = False
        elif change == 'profile':
            users[0]['profile'] = 'other-dev'
        elif change == 'direction':
            users[0]['direction'] = 'beta'
        captured.set()
        await tasks[0]
    asyncio.run(scenario())
    assert [payload['text'] for _, payload in messages] == ([] if change else ['private captured result'])


@pytest.mark.parametrize('retry', ['rate_limit', 'proxy'])
def test_document_retry_rechecks_notification_binding(delivery, monkeypatch, retry):
    import logging
    import aiohttp
    from telegram_app.api import TelegramAPIClient, TelegramAPIError
    from telegram_app.command_queue import notification_delivery
    bot, users, uploads = delivery
    users.append(principal())
    bot._api_log = logging.getLogger(__name__)
    monkeypatch.setattr(bot, '_should_use_proxy', lambda: retry == 'proxy')
    async def upload(self, chat_id, **kwargs):
        uploads.append(kwargs)
        if retry == 'rate_limit':
            raise TelegramAPIError('sendDocument', 429, {'parameters': {'retry_after': 1}})
        users[0]['enabled'] = False
        raise aiohttp.ClientConnectionError('synthetic transport')
    async def sleep(delay):
        users[0]['enabled'] = False
    monkeypatch.setattr(TelegramAPIClient, '_send_document_once', upload)
    monkeypatch.setattr(bot, '_mark_proxy_transport_failure', lambda: None)
    monkeypatch.setattr(asyncio, 'sleep', sleep)
    async def scenario():
        with notification_delivery('sample-dev', ('alpha',)):
            with pytest.raises(PermissionError):
                await bot._send_document(1, filename='private.md', content=b'private')
    asyncio.run(scenario())
    assert len(uploads) == 1


@pytest.mark.parametrize('multipart', [False, True])
def test_notifier_rechecks_after_session_rotation(delivery, monkeypatch, multipart):
    import notifier
    from unittest.mock import Mock
    bot, users, _ = delivery
    users.append(principal())
    session = SimpleNamespace(post=Mock(side_effect=AssertionError('unauthorized send')))
    async def rotate(**kwargs):
        users[0]['profile'] = 'other-dev'
        return session
    monkeypatch.setattr(notifier, '_get_session', rotate)
    target = notifier._DeliveryTarget(('synthetic-token', (1,), ''),
                                     profile_name='sample-dev', direction='alpha')
    assert not asyncio.run(notifier._send_to_chats('sendPhoto' if multipart else 'sendMessage',
        lambda chat: {'chat_id': chat}, target=target, multipart=multipart))
    session.post.assert_not_called()


@pytest.mark.parametrize('role', ['admin', 'user', 'tester'])
def test_fallback_is_allowed_only_with_matching_registry_binding(delivery, monkeypatch, role):
    bot, users, _ = delivery
    users.append(principal(99, role=role))
    monkeypatch.setattr(telegram_access, 'list_users', lambda: [])
    assert bot._profile_recipient_ids('sample-dev', direction='alpha') == [99]


@pytest.mark.parametrize('binding', [principal(enabled=False), principal(profile='other-dev'),
                                   principal(role='tester', direction='beta')])
def test_daily_report_rechecks_binding_after_worker_await(delivery, monkeypatch, binding):
    bot, users, messages = delivery
    users.append(principal(role='tester'))
    monkeypatch.setattr(bot, '_daily_summary_due', lambda: (True, '2026-10-26'))
    monkeypatch.setattr(bot, '_profile_names', lambda: ['sample-dev'])
    state = {}
    monkeypatch.setattr(bot, '_load_state', lambda: state)
    monkeypatch.setattr(bot, '_update_state', lambda mutate: mutate(state))
    monkeypatch.setattr(bot, '_append_debug_log', lambda *a, **kw: None)
    monkeypatch.setattr(bot, '_menu_reply_markup', lambda p: {})
    def report(*args, **kwargs):
        users[:] = [binding]
        return 'private report'
    monkeypatch.setattr(bot, '_daily_summary_report', report)
    asyncio.run(bot._maybe_send_daily_summary())
    assert messages == []
    assert not state['daily_summary']['delivered']


def test_rate_limit_retry_rechecks_notification_binding(delivery, monkeypatch):
    import logging
    from telegram_app.api import TelegramAPIClient, TelegramAPIError
    bot, users, messages = delivery
    users.append(principal())
    bot._api_log = logging.getLogger(__name__)
    monkeypatch.setattr(bot, '_should_use_proxy', lambda: False)
    monkeypatch.setattr(bot, '_api_request', TelegramAPIClient._api_request.__get__(bot))
    async def request(self, method, payload, **kwargs):
        messages.append((method, payload))
        raise TelegramAPIError(method, 429, {'parameters': {'retry_after': 1}})
    async def sleep(delay):
        users[0]['profile'] = 'other-dev'
    monkeypatch.setattr(TelegramAPIClient, '_api_request_once', request)
    monkeypatch.setattr(asyncio, 'sleep', sleep)
    asyncio.run(bot._queue_entry_notice(queued(), 'private task result'))
    assert len(messages) == 1


def test_unknown_active_execution_prevents_tester_pause_broadcast(delivery):
    bot, users, messages = delivery
    users.append(principal(role='tester'))
    bot._command_queues['sample-dev'] = [queued()]
    bot._active_commands['sample-dev'] = SimpleNamespace(queue_entry=None)
    asyncio.run(bot._pause_queue('sample-dev', 'private reason'))
    assert messages == []


def test_rebinding_between_markup_and_notice_blocks_followup(delivery, monkeypatch):
    bot, users, messages = delivery
    users.append(principal())
    item = queued()
    async def edit(*args, **kwargs):
        users[0]['profile'] = 'other-dev'
    monkeypatch.setattr(bot, '_edit_reply_markup', edit)
    async def scenario():
        await bot._queue_markup(item)
        await bot._queue_entry_notice(item, 'private task result')
    asyncio.run(scenario())
    assert messages == []


@pytest.mark.parametrize('change', ['revoke', 'rebind', 'remove', 'multichunk'])
def test_health_revalidates_each_deferred_delivery(delivery, monkeypatch, change):
    import telegram_app.monitoring as monitoring
    bot, users, messages = delivery
    users.extend([principal(1), principal(2)])
    if change == 'multichunk':
        users.pop()
    monkeypatch.setattr(config, 'TELEGRAM_HEALTH_CHECK_ENABLED', True)
    monkeypatch.setattr(config, 'TELEGRAM_HEALTH_CHECK_INTERVAL_MIN', 5)
    state = {}
    monkeypatch.setattr(bot, '_load_state', lambda: state)
    def update(mutate):
        mutate(state)
        return state
    monkeypatch.setattr(bot, '_update_state', update)
    monkeypatch.setattr(bot, '_append_debug_log', lambda *a, **kw: None)
    monkeypatch.setattr(bot, '_menu_reply_markup', lambda p: {})
    async def diagnostics(profile):
        return [{'name': 'private reason', 'ok': False}]
    monkeypatch.setattr(bot, '_collect_diagnostics', diagnostics)
    if change == 'multichunk':
        monkeypatch.setattr(monitoring, 'build_diagnostics_text', lambda **kw: 'private\n' * 1500)
    async def send(method, payload, **kwargs):
        messages.append((method, payload))
        if change == 'remove':
            users.pop()
        elif change == 'rebind':
            users[1]['profile'] = 'other-dev'
        elif change == 'revoke':
            users[1]['enabled'] = False
        else:
            users[0]['enabled'] = False
        return {'message_id': 1}
    monkeypatch.setattr(bot, '_api_request', send)
    asyncio.run(bot._maybe_send_health_alert())
    assert len(messages) == 1
    assert messages[0][1]['chat_id'] == 1


@pytest.mark.parametrize('binding', [principal(role='tester', direction='beta'), principal(role='user')])
def test_tester_replay_rejects_changed_execution_binding(delivery, monkeypatch, binding):
    bot, users, _ = delivery
    original = principal(role='tester')
    users.append(original)
    # The replay test supplies a prepared profile without touching runtime files.
    import profile as profile_mod
    monkeypatch.setattr(profile_mod, 'load_profile', lambda name: SimpleNamespace(name=name))
    captured, executed = [], []
    monkeypatch.setattr(bot, '_profile_names', lambda: ['sample-dev'])
    monkeypatch.setattr(bot, '_selected_profile', lambda p: p['profile'])
    async def admit(entry):
        captured.append(entry)
        return True
    async def launch(*args, **kwargs):
        executed.append(True)
    monkeypatch.setattr(bot, '_admit_command', admit)
    monkeypatch.setattr(bot, '_start_cli_command', launch)
    async def scenario():
        await bot._dispatch(1, original, '/search', '')
        users[:] = [binding]
        await captured[0].invoke(True)
    asyncio.run(scenario())
    assert executed == []


def test_pause_proof_includes_completed_failure_direction(delivery, monkeypatch):
    bot, users, messages = delivery
    users.append(principal(role='tester'))
    bot._command_queues['sample-dev'] = [queued(direction='alpha')]
    monkeypatch.setattr(bot, '_schedule_queue', lambda profile: None)
    completed = SimpleNamespace(queue_entry=queued(direction='beta'), pause_reason='private beta failure')
    bot._queue_command_finished('sample-dev', completed)
    asyncio.run(bot._pause_queue('sample-dev', completed.pause_reason))
    assert messages == []
