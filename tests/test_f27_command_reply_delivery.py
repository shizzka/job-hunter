"""Command replies retain captured targets and recheck current actor authority."""
import asyncio
import logging
from types import SimpleNamespace

import pytest

import config
import profile_access
import telegram_access
from telegram_app.api import TelegramAPIClient
from telegram_app.command_queue import QueueEntry, notification_delivery
from telegram_bot import TelegramBot


@pytest.fixture
def command_delivery(monkeypatch):
    monkeypatch.setattr(config, 'JOB_HUNTER_INSTANCE_KIND', 'dev')
    monkeypatch.setattr(config, 'NOTIFY_CHAT_ID', 0)
    users, messages, state = [], [], {'user_state': {}}
    monkeypatch.setattr(telegram_access, 'load_registry', lambda: {'users': users})
    monkeypatch.setattr(telegram_access, '_load_impersonation', lambda: {})
    def prepared(principal):
        profile, direction = profile_access.tester_binding(principal)
        profile_access.require_profile(profile)
        if direction not in {'alpha', 'beta'}:
            raise PermissionError('direction missing')
        return profile, direction
    monkeypatch.setattr(profile_access, 'require_tester_binding', prepared)
    import profile as profile_mod
    monkeypatch.setattr(profile_mod, 'load_profile', lambda name: SimpleNamespace(name=name))
    bot = TelegramBot.__new__(TelegramBot)
    bot._init_command_queue()
    bot._active_commands = {}
    bot._api_log = logging.getLogger(__name__)
    bot.profile_name = 'target-dev'
    monkeypatch.setattr(bot, '_profile_names', lambda: ['target-dev', 'registry-dev'])
    monkeypatch.setattr(bot, '_default_profile_name', lambda: 'target-dev')
    monkeypatch.setattr(bot, '_load_state', lambda: state)
    monkeypatch.setattr(bot, '_client_record', lambda uid: None)
    monkeypatch.setattr(bot, '_set_selected_menu', lambda *args: None)
    monkeypatch.setattr(bot, '_schedule_queue', lambda profile: None)
    monkeypatch.setattr(bot, '_should_use_proxy', lambda: False)
    monkeypatch.setattr(bot, '_profile_recipient_ids', lambda *args, **kwargs: [])
    async def transport(self, method, payload, **kwargs):
        messages.append((method, payload))
        return {'message_id': len(messages)}
    # Keep TelegramBot's real per-delivery and per-attempt guards in the path.
    monkeypatch.setattr(TelegramAPIClient, '_api_request_once', transport)
    return bot, users, messages, state


def actor(*, role='admin', profile='registry-dev', direction='alpha'):
    return dict(user_id=7, role=role, profile=profile, direction=direction, enabled=True)


def select(state, profile):
    state['user_state']['7'] = {'selected_profile': profile}


@pytest.mark.parametrize('role,binding', [('user', 'target-dev'), ('admin', 'registry-dev'),
                                         ('admin', 'default')])
def test_real_dispatch_delivers_progress_controls_and_deferred_result(command_delivery, monkeypatch, role, binding):
    bot, users, messages, state = command_delivery
    users.append(actor(role=role, profile=binding))
    select(state, 'target-dev')
    tasks = []
    async def route(chat, principal, command, arg, profile, reply_menu):
        assert profile == 'target-dev'
        await bot._send_text(chat, 'progress', reply_markup={'inline_keyboard': [[
            {'text': 'Stop', 'callback_data': 'cancel:target-dev'}]]})
        async def result():
            await asyncio.sleep(0)
            await bot._send_text(chat, 'result and sent letter')
        tasks.append(asyncio.create_task(result()))
    monkeypatch.setattr(bot, '_route_cli', route)
    async def scenario():
        await bot._dispatch(7, dict(users[0]), '/search', '')
        # Selection is UI state; it does not change authority over this run.
        select(state, 'registry-dev')
        await asyncio.gather(*tasks)
    asyncio.run(scenario())
    assert [payload['text'] for _, payload in messages] == ['progress', 'result and sent letter']
    assert messages[0][1]['reply_markup']['inline_keyboard'][0][0]['text'] == 'Stop'


@pytest.mark.parametrize('binding', ['registry-dev', 'default'])
def test_authorized_admin_receives_execution_refusal(command_delivery, monkeypatch, binding):
    bot, users, messages, state = command_delivery
    users.append(actor(profile=binding))
    select(state, 'target-dev')
    async def refuse(chat, *args):
        await bot._send_text(chat, 'refusal: session repair required')
    monkeypatch.setattr(bot, '_route_cli', refuse)
    asyncio.run(bot._dispatch(7, dict(users[0]), '/search', ''))
    assert [payload['text'] for _, payload in messages] == ['refusal: session repair required']
    assert not bot._active_commands


@pytest.mark.parametrize('change', ['disable', 'remove', 'demote', 'instance', 'impersonate',
                                   'user_profile', 'tester_profile', 'tester_direction'])
def test_deferred_command_result_rechecks_current_authority(command_delivery, monkeypatch, change):
    bot, users, messages, state = command_delivery
    role = 'tester' if change.startswith('tester') else 'user' if change == 'user_profile' else 'admin'
    users.append(actor(role=role, profile='target-dev' if role != 'admin' else 'registry-dev'))
    select(state, 'target-dev')
    tasks = []
    async def route(chat, *args):
        await bot._send_text(chat, 'progress')
        async def result():
            await asyncio.sleep(0)
            await bot._send_text(chat, 'private result')
        tasks.append(asyncio.create_task(result()))
    monkeypatch.setattr(bot, '_route_cli', route)
    async def scenario():
        await bot._dispatch(7, dict(users[0]), '/search', '')
        if change == 'disable':
            users[0]['enabled'] = False
        elif change == 'remove':
            users.clear()
        elif change == 'demote':
            users[0]['role'] = 'user'
        elif change == 'instance':
            monkeypatch.setattr(config, 'JOB_HUNTER_INSTANCE_KIND', 'prod')
        elif change == 'impersonate':
            monkeypatch.setattr(telegram_access, '_load_impersonation', lambda: {'7': {'profile': 'registry-dev'}})
        elif change.endswith('profile'):
            users[0]['profile'] = 'registry-dev'
        else:
            users[0]['direction'] = 'beta'
        await asyncio.gather(*tasks)
    asyncio.run(scenario())
    assert [payload['text'] for _, payload in messages] == ['progress']


@pytest.mark.parametrize('operation', ['notice', 'markup', 'pause', 'summary'])
def test_switched_admin_queue_deliveries_use_actor_authority(command_delivery, operation):
    bot, users, messages, _ = command_delivery
    users.append(actor())
    item = QueueEntry('target-dev', 7, 7, 'search', None, direction='alpha',
                      source={'message': {'message_id': 5}, 'data': 'manual_apply:target-dev:token'})
    async def scenario():
        if operation == 'notice':
            await bot._queue_entry_notice(item, 'queued')
        elif operation == 'markup':
            await bot._queue_markup(item)
        elif operation == 'pause':
            bot._command_queues['target-dev'] = [item]
            await bot._pause_queue('target-dev', 'guard paused')
        else:
            bot._queue_history['target-dev'] = [item]
            await bot._queue_summary('target-dev')
    asyncio.run(scenario())
    assert len(messages) == 1
    assert messages[0][1]['chat_id'] == 7


def test_admin_command_scope_does_not_authorize_unsolicited_notifications(command_delivery):
    bot, users, messages, _ = command_delivery
    users.append(actor())
    users.append({**actor(role='user', profile='target-dev'), 'user_id': 88})
    async def invoke(revalidate):
        with notification_delivery('target-dev', ('alpha',)):
            await bot._send_text(7, 'unsolicited private event')
        await bot._send_text(88, 'unrelated recipient')
    entry = QueueEntry('target-dev', 7, 7, 'search', invoke)
    asyncio.run(bot._invoke_queue_entry(entry, revalidate=False))
    assert messages == []
    assert not telegram_access.notification_allowed(7, profile_name='target-dev', direction='alpha')


@pytest.mark.parametrize('operation', ['message_retry', 'document_retry', 'multichunk'])
def test_admin_command_revalidates_each_chunk_and_transport_retry(command_delivery, monkeypatch, operation):
    from telegram_app.api import TelegramAPIError
    bot, users, messages, _ = command_delivery
    users.append(actor())
    async def attempt(self, method, payload, **kwargs):
        messages.append((method, payload))
        users[0]['enabled'] = False
        if operation != 'multichunk':
            raise TelegramAPIError(method, 429, {'parameters': {'retry_after': 1}})
        return {'message_id': 1}
    async def document(self, chat_id, **kwargs):
        return await attempt(self, 'sendDocument', {'chat_id': chat_id})
    async def sleep(delay):
        pass
    monkeypatch.setattr(TelegramAPIClient, '_api_request_once', attempt)
    monkeypatch.setattr(TelegramAPIClient, '_send_document_once', document)
    monkeypatch.setattr(asyncio, 'sleep', sleep)
    async def invoke(revalidate):
        if operation == 'document_retry':
            with pytest.raises(PermissionError):
                await bot._send_document(7, filename='letter.md', content=b'synthetic letter')
        elif operation == 'message_retry':
            with pytest.raises(PermissionError):
                await bot._send_text(7, 'result')
        else:
            await bot._send_text(7, 'private\n' * 1500)
    entry = QueueEntry('target-dev', 7, 7, 'search', invoke)
    asyncio.run(bot._invoke_queue_entry(entry, revalidate=False))
    assert len(messages) == 1


def test_admin_sent_letter_document_uses_command_authority(command_delivery, monkeypatch):
    bot, users, messages, _ = command_delivery
    users.append(actor(profile='default'))
    async def document(self, chat_id, **kwargs):
        messages.append(('sendDocument', {'chat_id': chat_id, **kwargs}))
        return {'message_id': 1}
    monkeypatch.setattr(TelegramAPIClient, '_send_document_once', document)
    async def invoke(revalidate):
        await bot._send_document(7, filename='letter.md', content=b'synthetic letter')
    entry = QueueEntry('target-dev', 7, 7, 'search', invoke)
    asyncio.run(bot._invoke_queue_entry(entry, revalidate=False))
    assert [method for method, _ in messages] == ['sendDocument']


def test_queue_delivery_rebinds_actor_scope_inherited_from_another_tester(command_delivery):
    bot, users, messages, _ = command_delivery
    users.append(actor())
    other = {**actor(role='tester', profile='registry-dev'), 'user_id': 88}
    item = QueueEntry('target-dev', 7, 7, 'search', None)
    async def scenario():
        with profile_access.principal_scope(other):
            await bot._queue_entry_notice(item, 'queued')
    asyncio.run(scenario())
    assert [payload['text'] for _, payload in messages] == ['queued']


@pytest.mark.parametrize('role,binding', [('user', 'target-dev'), ('admin', 'registry-dev'),
                                         ('admin', 'default')])
@pytest.mark.parametrize('revoke', [False, True])
def test_real_cli_runner_replies_to_authorized_initiator_after_capture(
        command_delivery, monkeypatch, role, binding, revoke):
    """Keep dispatch, admission, command starter, runner and transport guards real."""
    import runtime_control
    bot, users, messages, state = command_delivery
    users.append(actor(role=role, profile=binding))
    select(state, 'target-dev')
    monkeypatch.setattr(bot, '_daemon_state', lambda name: {'running': False})
    monkeypatch.setattr(bot, '_menu_reply_markup', lambda p: {})
    monkeypatch.setattr(bot, '_sync_active_runtime', lambda: None)
    monkeypatch.setattr(runtime_control, 'agent_command_argv', lambda *args: ['synthetic'])
    monkeypatch.setattr(bot, '_cli_command_result', lambda *args: ('captured result', False, '', b''))
    async def scenario():
        captured = asyncio.Event()
        async def capture(command, argv, **kwargs):
            assert command.profile_name == 'target-dev'
            await captured.wait()
            return {'ok': True}
        monkeypatch.setattr(bot, '_run_active_command_capture', capture)
        assert await bot._dispatch(7, dict(users[0]), '/search', '')
        task = bot._active_commands['target-dev'].task
        assert task is not None
        select(state, 'registry-dev')
        if revoke:
            users[0]['enabled'] = False
        captured.set()
        await task
    asyncio.run(scenario())
    text_outputs = [payload for method, payload in messages if method == 'sendMessage']
    assert len(text_outputs) == (1 if revoke else 2)
    assert text_outputs[0]['reply_markup']['inline_keyboard']
    if not revoke:
        assert text_outputs[1]['text'] == 'captured result'
    assert not bot._active_commands
