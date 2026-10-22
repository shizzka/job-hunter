"""B7: synthetic instance boundaries and tester requests, without live profiles."""
import asyncio
import json
import subprocess
import sys
from unittest.mock import AsyncMock

import pytest

import chat_context
import config
import directions
from direction_history import HistoryScope
import profile as profiles
import profile_access as access
import runtime_control
import telegram_access
import telegram_bot
import telegram_bot_ui as ui
import telegram_clients
from tests.direction_view_fakes import two_directions


@pytest.fixture
def synthetic_profiles(tmp_path, monkeypatch):
    monkeypatch.setenv('JOB_HUNTER_HOME', str(tmp_path))
    monkeypatch.setattr(config, 'JOB_HUNTER_HOME', str(tmp_path))
    monkeypatch.setattr(config, 'NOTIFY_CHAT_ID', 0)
    monkeypatch.setattr(config, 'TELEGRAM_ACCESS_FILE', str(tmp_path / 'telegram-users.json'))
    monkeypatch.setattr(config, 'TELEGRAM_CLIENTS_FILE', str(tmp_path / 'telegram-clients.json'))
    for name in ('qa', 'client', 'qa-dev', 'synthetic-dev'):
        home = tmp_path / 'profiles' / name
        home.mkdir(parents=True)
        (home / 'profile.env').write_text('HH_ENABLED=0\n')
    two_directions(tmp_path / 'profiles' / 'qa-dev')
    return tmp_path


@pytest.mark.parametrize('kind,expected,forbidden', [
    ('dev', ['qa-dev', 'synthetic-dev'], 'client'),
    ('prod', ['qa', 'client', 'default'], 'qa-dev'),
    ('unrecognized', [], 'qa'),
    ('', [], 'qa'),
])
def test_profile_data_access_boundary(synthetic_profiles, monkeypatch, caplog, kind, expected, forbidden):
    monkeypatch.setattr(config, 'JOB_HUNTER_INSTANCE_KIND', kind)
    access._instance_kind.cache_clear()
    assert profiles.list_profiles() == expected
    assert all(profiles.load_profile(name).name == name for name in expected)
    for operation in (profiles.load_profile, profiles.activate_no_lock, profiles.profile_env_path,
                      profiles.create_profile, runtime_control.agent_command_argv):
        with pytest.raises(PermissionError):
            operation(forbidden)
    with pytest.raises(PermissionError):
        profiles.update_profile_env(forbidden, {'HH_ENABLED': '1'})
    assert (synthetic_profiles / 'profiles' / forbidden / 'profile.env').read_text() == 'HH_ENABLED=0\n'
    if kind not in {'dev', 'prod'}:
        for name in ('qa', 'default', 'qa-dev', 'client'):
            with pytest.raises(PermissionError):
                profiles.load_profile(name)
            with pytest.raises(PermissionError):
                runtime_control.agent_command_argv(name)
        with pytest.raises(PermissionError):
            access.worker_environment()
        assert not caplog.text


def test_dev_never_falls_back_to_default(synthetic_profiles, monkeypatch):
    monkeypatch.setattr(config, 'JOB_HUNTER_INSTANCE_KIND', 'dev')
    for name in ('qa-dev', 'synthetic-dev'):
        (synthetic_profiles / 'profiles' / name / 'profile.env').unlink()
    assert profiles.list_profiles() == []
    bot = telegram_bot.TelegramBot('qa-dev')
    assert bot._profile_names() == []
    with pytest.raises(PermissionError):
        profiles.load_default_profile()


@pytest.fixture
def dev_bot(synthetic_profiles, monkeypatch):
    monkeypatch.setattr(config, 'JOB_HUNTER_INSTANCE_KIND', 'dev')
    bot = telegram_bot.TelegramBot('qa-dev')
    bot._send_text = AsyncMock(return_value={})
    bot._answer_callback_query = AsyncMock()
    bot._edit_reply_markup = AsyncMock()
    return bot


@pytest.mark.parametrize('kind,forbidden', [('dev', 'client'), ('prod', 'qa-dev'), ('unknown', 'qa-dev')])
def test_admin_crafted_command_and_callback_refused(dev_bot, monkeypatch, kind, forbidden):
    monkeypatch.setattr(config, 'JOB_HUNTER_INSTANCE_KIND', kind)
    principal = {'user_id': 101, 'role': 'admin', 'profile': 'default'}
    monkeypatch.setattr(telegram_access, 'resolve_user', lambda uid: principal)
    dev_bot._start_chat_ai_reply = AsyncMock()
    asyncio.run(dev_bot._dispatch(101, principal, '/profile', forbidden))
    expected = 'Профиль недоступен' if kind == 'unknown' else 'Неизвестный профиль'
    assert expected in dev_bot._send_text.call_args.args[1]
    asyncio.run(dev_bot._handle_callback_query({
        'id': 'synthetic', 'from': {'id': 101}, 'data': f'chat_ai:{forbidden}:123:456',
        'message': {'chat': {'id': 101, 'type': 'private'}, 'message_id': 1},
    }))
    dev_bot._start_chat_ai_reply.assert_not_awaited()
    assert 'selected_profile' not in str(dev_bot._load_state())


def test_admin_stale_selection_uses_visible_profile(dev_bot, monkeypatch):
    principal = {'user_id': 101, 'role': 'admin', 'profile': 'qa'}
    dev_bot._update_user_state('user_state', 101, fields={'selected_profile': 'client'})
    assert dev_bot._selected_profile(principal) == 'qa-dev'


def test_dev_user_list_projects_default_owner_into_visible_namespace(dev_bot):
    telegram_access.save_registry({'users': [
        {'user_id': 101, 'role': 'admin', 'profile': 'default'},
        {'user_id': 303, 'role': 'user', 'profile': 'client'},
        {'user_id': 404, 'role': 'user', 'profile': 'synthetic-dev'},
    ]})
    assert [(u['user_id'], u['profile']) for u in telegram_access.list_users()] == [
        (101, 'qa-dev'), (404, 'synthetic-dev')]
    assert telegram_access.load_registry()['users'][0]['profile'] == 'default'


@pytest.fixture
def tester(dev_bot, monkeypatch):
    principal = telegram_access.upsert_user(202, profile='qa-dev', role='tester', direction='qa')
    # A client binding must never override the tester's fixed profile.
    telegram_clients.save_registry({'clients': [{'user_id': 202, 'profile_name': 'synthetic-dev'},
                                               {'user_id': 303, 'profile_name': 'qa-dev'}]})
    dev_bot._update_user_state('user_state', 202, fields={'selected_profile': 'synthetic-dev', 'menu': ui.MENU_ADMIN})
    return dev_bot, principal


@pytest.mark.parametrize('profile,direction', [('qa', 'qa'), ('synthetic-dev', 'qa'), ('qa-dev', 'electric'), ('qa-dev', '')])
def test_invalid_tester_binding_is_rejected(profile, direction):
    assert telegram_access._normalize_entry({'user_id': 202, 'role': 'tester',
                                            'profile': profile, 'direction': direction}) is None


def test_tester_scope_confines_profile_direction_and_registries(tester):
    bot, principal = tester
    assert telegram_access.resolve_user(202) == principal
    assert bot._selected_profile(principal) == 'qa-dev'
    assert bot._selected_menu(principal) != ui.MENU_ADMIN
    home = bot._profile('qa-dev').home_dir
    with access.principal_scope(principal):
        assert profiles.list_profiles() == ['qa-dev']
        assert [d['name'] for d in directions.list_directions(home)] == ['qa']
        assert telegram_clients.list_clients() == []
        assert [u['user_id'] for u in telegram_access.list_users()] == [202]
        with pytest.raises(PermissionError):
            profiles.load_profile('synthetic-dev')
        with pytest.raises(PermissionError):
            telegram_clients.get_client(303)
        with pytest.raises(PermissionError):
            bot._set_selected_profile(202, 'qa-dev')
        with pytest.raises(PermissionError):
            directions.set_active_direction(home, 'qa')
        with pytest.raises(PermissionError):
            directions.load_direction(home, 'electric')
        with pytest.raises(PermissionError):
            HistoryScope(home, '*')
    assert not access.current_scope()[0]


@pytest.mark.parametrize('command', sorted(ui.ADMIN_ONLY_COMMANDS - {'/cancel_active'}) +
                         ['/direction_switch', '/direction_new'])
def test_tester_admin_and_switch_commands_refused(tester, command):
    bot, principal = tester
    asyncio.run(bot._dispatch(202, principal, command, 'synthetic-dev'))
    assert '🔒' in bot._send_text.call_args.args[1]
    assert not bot._active_command('qa-dev')


def test_tester_owner_menu_and_profile_callback(tester, monkeypatch):
    bot, principal = tester
    asyncio.run(bot._handle_update({'message': {'chat': {'id': 202, 'type': 'private'},
        'from': {'id': 202}, 'text': '/menu'}}))
    assert 'qa-dev' in bot._send_text.call_args_list[0].args[1]
    assert 'synthetic-dev' not in str(bot._send_text.call_args_list)
    bot._start_chat_ai_reply = AsyncMock()
    asyncio.run(bot._handle_callback_query({'id': 'test', 'from': {'id': 202},
        'data': 'chat_ai:synthetic-dev:123:456',
        'message': {'chat': {'id': 202, 'type': 'private'}, 'message_id': 1}}))
    bot._start_chat_ai_reply.assert_not_awaited()
    assert 'недоступен' in bot._answer_callback_query.call_args.args[1]
    assert not access.current_scope()[0]


def test_tester_cannot_use_stale_direction_callback(tester):
    bot, principal = tester
    home = bot._profile('qa-dev').home_dir
    bot._direction_bind(principal, mode='switch', choices=directions.list_directions(home))
    state = bot._direction_sessions()[202]
    assert state['choices'][0]['name'] == 'electric'
    asyncio.run(bot._handle_callback_query({'id': 'test', 'from': {'id': 202},
        'data': f"dir:{state['token']}:choose:0",
        'message': {'chat': {'id': 202, 'type': 'private'}, 'message_id': 1}}))
    assert 'QA' in bot._send_text.call_args.args[1]
    assert directions.active_direction(home)['name'] == 'qa'


def test_tester_refuses_changed_active_direction_before_command(tester):
    bot, principal = tester
    home = bot._profile('qa-dev').home_dir
    directions.set_active_direction(home, 'electric')
    bot._cancel_active_command = AsyncMock()
    asyncio.run(bot._dispatch(202, principal, '/cancel_active', ''))
    bot._cancel_active_command.assert_not_awaited()
    assert 'QA' in bot._send_text.call_args.args[1]


@pytest.mark.parametrize('kind,forbidden', [('dev', 'client'), ('prod', 'synthetic-dev')])
def test_client_registry_boundary(dev_bot, monkeypatch, kind, forbidden):
    monkeypatch.setattr(config, 'JOB_HUNTER_INSTANCE_KIND', kind)
    telegram_clients.save_registry({'clients': [{'user_id': 303, 'profile_name': forbidden}]})
    assert not telegram_clients.list_clients()
    with pytest.raises(PermissionError):
        telegram_clients.get_client(303)
    with pytest.raises(PermissionError):
        telegram_clients.set_status(303, status='approved')


def test_tester_scope_inherited_by_task_and_worker(tester):
    _, principal = tester
    async def inspect_child():
        async def child():
            env = access.worker_environment()
            env['JOB_HUNTER_HOME'] = config.JOB_HUNTER_HOME
            code = ('import profile, directions; '
                    'p=profile.load_profile("qa-dev"); '
                    'print(profile.list_profiles(), p.direction_name); '
                    'profile.load_profile("synthetic-dev")')
            result = await asyncio.to_thread(subprocess.run, [sys.executable, '-c', code],
                                             env=env, capture_output=True, text=True)
            assert "['qa-dev'] qa" in result.stdout
            assert result.returncode != 0 and 'PermissionError' in result.stderr
        with access.principal_scope(principal):
            task = asyncio.create_task(child())
        await task
    asyncio.run(inspect_child())


@pytest.mark.parametrize('kind,home,other', [('dev', 'qa-dev', 'client'), ('prod', 'qa', 'synthetic-dev')])
def test_chat_owner_lookup_does_not_reveal_other_instance(synthetic_profiles, monkeypatch, kind, home, other):
    monkeypatch.setattr(config, 'JOB_HUNTER_INSTANCE_KIND', kind)
    own = synthetic_profiles / 'profiles' / home
    other_home = synthetic_profiles / 'profiles' / other / 'directions'
    other_home.mkdir()
    (other_home / 'x.json').write_text(json.dumps({'name': 'x', 'variants': [
        {'hh_resume_numeric_id': 987, 'title': 'hidden title'}]}))
    (own / 'negotiation_snapshots.jsonl').write_text(json.dumps({'chatId': '123', 'resumeId': 987}) + '\n')
    owner = chat_context.resolve(str(own), '123')
    assert owner.status == chat_context.OTHER
    assert not owner.owner_profile and not owner.title


def test_limits_filter_profile_names_without_resetting_shared_credit(dev_bot):
    import telegram_resume_limits as limits
    limits.save_registry({'users': [{'user_id': 404, 'free_total': 4, 'free_used': 3,
        'profiles': {'client': {'analysis_count': 2}, 'qa-dev': {'analysis_count': 1}}}],
        'events': [{'user_id': 404, 'profile_name': 'client', 'action': 'analysis_used'},
                   {'user_id': 404, 'profile_name': 'qa-dev', 'action': 'analysis_used'}]})
    snapshot = limits.get_user_snapshot(404)
    assert list(snapshot['profiles']) == ['qa-dev']
    assert snapshot['available_soft'] == 1 and snapshot['analysis_total'] == 3
    assert list(limits.load_registry()['users'][0]['profiles']) == ['client', 'qa-dev']
    assert [event['profile_name'] for event in limits.recent_events()] == ['qa-dev']


def test_dev_cannot_revoke_or_rebind_prod_user(dev_bot):
    telegram_access.save_registry({'users': [{'user_id': 505, 'role': 'user', 'profile': 'client'}]})
    with pytest.raises(PermissionError):
        telegram_access.remove_user(505)
    with pytest.raises(PermissionError):
        telegram_access.upsert_user(505, profile='qa-dev')
    assert telegram_access.load_registry()['users'][0]['profile'] == 'client'


def test_queue_replay_rebinds_owner_and_refuses_revoked_tester(tester, monkeypatch):
    bot, principal = tester
    owner = telegram_access.upsert_user(606, profile='synthetic-dev', role='admin')
    entries = []
    async def admit(entry):
        entries.append(entry)
    monkeypatch.setattr(bot, '_admit_command', admit)
    bot._start_cli_command = AsyncMock()
    async def scenario():
        await bot._dispatch(606, owner, '/search', '')
        with access.principal_scope(principal):
            await entries.pop().invoke(True)
        assert bot._start_cli_command.call_args.args[2] == 'synthetic-dev'
        bot._start_cli_command.reset_mock()
        await bot._dispatch(202, principal, '/search', '')
        telegram_access.upsert_user(202, profile='qa-dev', role='tester', direction='qa', enabled=False)
        await entries.pop().invoke(True)
        bot._start_cli_command.assert_not_awaited()
        assert 'доступ изменились' in bot._send_text.call_args.args[1]
    asyncio.run(scenario())
