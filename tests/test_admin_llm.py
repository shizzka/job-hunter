import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
import admin_llm
import telegram_bot
import telegram_bot_ui as ui


@pytest.mark.parametrize('command', ['/menu_llm', '/llm_balance', '/llm_test'])
def test_user_cannot_access(monkeypatch, command):
    bot = telegram_bot.TelegramBot('qa')
    monkeypatch.setattr(bot, '_selected_profile', lambda p: 'qa')
    monkeypatch.setattr(bot, '_active_command', lambda p: None)
    monkeypatch.setattr(bot, '_menu_reply_markup', lambda *a, **k: {})
    send = AsyncMock()
    monkeypatch.setattr(bot, '_send_text', send)
    diagnostic = AsyncMock()
    monkeypatch.setattr(admin_llm, 'diagnostic', diagnostic)
    asyncio.run(bot._dispatch(1, {'user_id': 1, 'role': ui.ROLE_USER}, command, ''))
    assert 'администратора' in send.call_args.args[1]
    diagnostic.assert_not_called()


def test_menu_admin_only():
    assert ui.BUTTON_LLM in str(ui.build_reply_markup(ui.ROLE_ADMIN, menu=ui.MENU_ADMIN))
    assert ui.BUTTON_LLM_TEST in str(ui.build_reply_markup(ui.ROLE_ADMIN, menu=ui.MENU_LLM))
    assert ui.BUTTON_LLM_TEST not in str(ui.build_reply_markup(ui.ROLE_USER, menu=ui.MENU_LLM))


@pytest.mark.parametrize('available', [True, False])
def test_pinned_bounded_request(monkeypatch, available):
    calls = []
    def handler(request):
        calls.append(request)
        if request.url.path == '/models':
            return httpx.Response(200, json={'data': [{'id': admin_llm.TEST_MODEL}] if available else []})
        return httpx.Response(200, json={'model': admin_llm.TEST_MODEL, 'choices': [{'message': {'content': 'Связь работает'}}], 'usage': {'prompt_tokens': 8, 'completion_tokens': 3}})
    provider = SimpleNamespace(base_url='https://deepseek.test', api_key='secret', default_headers={})
    monkeypatch.setattr(admin_llm, 'deepseek_provider', lambda: provider)
    monkeypatch.setattr(admin_llm.proxy_utils, 'llm_http_client', lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    result = asyncio.run(admin_llm.diagnostic(test=True))
    assert len(calls) == (2 if available else 1)
    if available:
        import json
        body = json.loads(calls[-1].content)
        assert body['max_tokens'] == 256
        assert body['model'] == admin_llm.TEST_MODEL
        assert 'Связь работает' in result
    else:
        assert 'не отправлен' in result


def test_errors_do_not_leak_secrets(monkeypatch):
    monkeypatch.setattr(admin_llm, 'deepseek_provider', lambda: object())
    monkeypatch.setattr(admin_llm, '_request', AsyncMock(side_effect=RuntimeError('secret')))
    result = asyncio.run(admin_llm.diagnostic(test=True))
    assert 'secret' not in result and 'RuntimeError' in result


def test_admin_cooldown_and_back(monkeypatch):
    bot = telegram_bot.TelegramBot('qa')
    principal = {'user_id': 1, 'role': ui.ROLE_ADMIN}
    monkeypatch.setattr(bot, '_selected_profile', lambda p: 'qa')
    monkeypatch.setattr(bot, '_active_command', lambda p: None)
    monkeypatch.setattr(bot, '_selected_menu', lambda p: ui.MENU_LLM)
    monkeypatch.setattr(bot, '_set_selected_menu', lambda *a: None)
    monkeypatch.setattr(bot, '_menu_reply_markup', lambda *a, **k: {})
    monkeypatch.setattr(bot, '_send_text', AsyncMock())
    menu = AsyncMock()
    monkeypatch.setattr(bot, '_send_menu', menu)
    diagnostic = AsyncMock(return_value='OK')
    monkeypatch.setattr(admin_llm, 'diagnostic', diagnostic)
    async def run():
        await bot._dispatch(1, principal, '/llm_test', '')
        await bot._dispatch(1, principal, '/llm_test', '')
        await bot._dispatch(1, principal, '/back', '')
    asyncio.run(run())
    diagnostic.assert_awaited_once_with(test=True)
    assert menu.call_args.kwargs['menu'] == ui.MENU_ADMIN
