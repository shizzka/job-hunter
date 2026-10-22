"""Recipient authorization is exercised without Telegram transports."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import config
import notifier
import telegram_access


def setup(monkeypatch, tmp_path, kind, profile_name, recipient, binding, role='user', enabled=True):
    monkeypatch.setattr(config, 'JOB_HUNTER_INSTANCE_KIND', kind)
    monkeypatch.setattr(config, 'NOTIFY_CHAT_ID', recipient)
    telegram_access.save_registry({'users': [{'user_id': recipient, 'profile': binding,
                                            'role': role, 'enabled': enabled}]})
    profile = SimpleNamespace(name=profile_name, home_dir=str(tmp_path),
                              notify=SimpleNamespace(chat_id=recipient, bot_token='synthetic-token', proxy=''))
    monkeypatch.setattr(notifier, '_active_profile', lambda: profile)
    deliver = AsyncMock(return_value=True)
    monkeypatch.setattr(notifier, '_deliver', deliver)
    return profile, deliver


@pytest.mark.parametrize('kind,name,binding', [('prod', 'qa', 'qa-dev'), ('dev', 'qa-dev', 'qa'),
                                               ('prod', 'qa', 'client')])
@pytest.mark.parametrize('role', ['user', 'admin'])
def test_configured_and_pinned_recipients_cannot_cross_profile_boundary(monkeypatch, tmp_path, kind, name, binding, role):
    profile, deliver = setup(monkeypatch, tmp_path, kind, name, 202, binding, role)
    assert notifier._resolve_target_chat_ids(profile) == []
    assert not asyncio.run(notifier._send_to_chats('sendMessage', notifier._build_text_payload('private', 'HTML', None),
                                                  target=('synthetic-token', (202,), '')))
    deliver.assert_not_awaited()


@pytest.mark.parametrize('enabled', [False, True])
def test_ordinary_recipient_requires_enabled_matching_binding(monkeypatch, tmp_path, enabled):
    profile, deliver = setup(monkeypatch, tmp_path, 'prod', 'qa', 202, 'qa', enabled=enabled)
    assert notifier._resolve_target_chat_ids(profile) == ([202] if enabled else [])
    assert asyncio.run(notifier.send_message('synthetic')) is enabled
    assert deliver.await_count == int(enabled)
    assert not telegram_access.notification_allowed(999, profile_name='qa', direction=None)


def test_captured_profile_target_survives_selection_change_but_not_recipient_rebinding(monkeypatch, tmp_path):
    profile, deliver = setup(monkeypatch, tmp_path, 'prod', 'qa', 202, 'qa')
    target = notifier.capture_delivery_target()
    monkeypatch.setattr(notifier, '_active_profile', lambda: SimpleNamespace(name='client'))
    build = notifier._build_text_payload('synthetic', 'HTML', None)
    assert asyncio.run(notifier._send_to_chats('sendMessage', build, target=target))
    telegram_access.save_registry({'users': [{'user_id': 202, 'profile': 'qa-dev', 'role': 'user'}]})
    assert not asyncio.run(notifier._send_to_chats('sendMessage', build, target=target))
    assert deliver.await_count == 1


@pytest.mark.parametrize('role', ['user', 'tester'])
def test_explicit_owner_route_never_accepts_client_or_tester_binding(monkeypatch, tmp_path, role):
    profile, deliver = setup(monkeypatch, tmp_path, 'dev', 'qa-dev', 202, 'qa-dev', role)
    if role == 'tester':
        telegram_access.save_registry({'users': [{'user_id': 202, 'profile': 'qa-dev', 'role': role, 'direction': 'qa'}]})
    monkeypatch.setenv('NOTIFY_CHAT_ID', '202')
    monkeypatch.setattr(config, 'TELEGRAM_CONTROL_BOT_TOKEN', 'synthetic-control')
    assert not asyncio.run(notifier.notify_audit_owner('private'))
    assert not asyncio.run(notifier.notify_audit_owner('private', target=('synthetic-control', (202,), '')))
    deliver.assert_not_awaited()
