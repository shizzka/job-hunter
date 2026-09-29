import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import company_blacklist as blacklist
import apply_orchestrator
import config
import manual_apply_queue
import profile
import telegram_bot


@pytest.fixture
def homes(monkeypatch, tmp_path):
    monkeypatch.setattr(config, 'JOB_HUNTER_HOME', str(tmp_path / 'alice'))
    def load(name):
        if name not in {'alice', 'bob'}:
            raise FileNotFoundError(name)
        return SimpleNamespace(home_dir=str(tmp_path / name))
    monkeypatch.setattr(profile, 'load_profile', load)
    return tmp_path


def test_normalization_isolation_and_removal(homes):
    blacklist.set_blocked('ПАО «Банк Санкт-Петербург»', profile_name='alice')
    assert blacklist.is_blocked('БАНК САНКТ ПЕТЕРБУРГ', 'alice')
    assert not blacklist.is_blocked('Банк Санкт-Петербург', 'bob')
    assert not blacklist.is_blocked('Другой банк Санкт-Петербург', 'alice')
    blacklist.set_blocked('Банк Санкт-Петербург', False, 'alice')
    assert blacklist.list_companies('alice') == []
    with pytest.raises(FileNotFoundError):
        blacklist.set_blocked('Acme', profile_name='missing')


def test_dispatch_rechecks_block_after_queue_creation(homes, monkeypatch):
    monkeypatch.setattr(config, 'MANUAL_APPLY_QUEUE_FILE', str(homes / 'alice/manual_apply_queue.json'))
    vacancy = {'company': 'Acme', 'source': 'hh', 'url': 'https://example.test/1'}
    manual_apply_queue.create_candidate(vacancy, {}, profile_name='alice')
    assert len(manual_apply_queue.list_candidates('alice')) == 1
    blacklist.set_blocked('Acme', profile_name='alice')
    assert manual_apply_queue.list_candidates('alice') == []
    client = SimpleNamespace(apply_to_vacancy=AsyncMock(return_value={"ok": True}))
    result = asyncio.run(apply_orchestrator.dispatch_apply(vacancy, '', hh_client=client))
    assert result['reason'] == 'company_blacklisted'
    client.apply_to_vacancy.assert_not_awaited()
    blacklist.set_blocked('Acme', False, 'alice')
    asyncio.run(apply_orchestrator.dispatch_apply(vacancy, '', hh_client=client))
    client.apply_to_vacancy.assert_awaited_once()


def test_corrupt_blacklist_fails_closed(homes):
    blacklist.set_blocked('Acme')
    (homes / 'alice/company_blacklist.json').write_text('invalid')
    client = SimpleNamespace(apply_to_vacancy=AsyncMock(return_value={"ok": True}))
    with pytest.raises(ValueError):
        asyncio.run(apply_orchestrator.dispatch_apply({'company': 'Acme'}, '', hh_client=client))
    client.apply_to_vacancy.assert_not_awaited()


def test_telegram_input_uses_selected_profile(homes, monkeypatch):
    bot = telegram_bot.TelegramBot('qa')
    principal = {'user_id': 42, 'role': telegram_bot.ROLE_USER, 'profile': 'bob'}
    monkeypatch.setattr(bot, '_search_settings_state', lambda uid: {'search_input': 'company_block'})
    monkeypatch.setattr(bot, '_selected_profile', lambda p: 'bob')
    monkeypatch.setattr(bot, '_clear_search_settings_state', lambda *args: None)
    monkeypatch.setattr(bot, '_menu_reply_markup', lambda *args, **kwargs: {})
    monkeypatch.setattr(bot, '_send_text', AsyncMock())
    assert asyncio.run(bot._accept_search_query_input(42, principal, 'Acme'))
    assert blacklist.is_blocked('Acme', 'bob')
    assert not blacklist.is_blocked('Acme', 'alice')
