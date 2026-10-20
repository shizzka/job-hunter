import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import analytics
import config
import telegram_bot
import telegram_bot_ui as ui
import telegram_resume_limits as limits

import telegram_access


class TestTelegramAccess:
    def test_normalize_entry_defaults(self):
        entry = telegram_access._normalize_entry({"user_id": 123})
        assert entry["user_id"] == 123
        assert entry["role"] == telegram_access.ROLE_USER
        assert entry["profile"] == "default"

    def test_normalize_registry_adds_owner(self, monkeypatch):
        monkeypatch.setattr(telegram_access.config, "NOTIFY_CHAT_ID", 999)
        registry = telegram_access._normalize_registry({"users": []})
        assert registry["users"][0]["user_id"] == 999
        assert registry["users"][0]["role"] == telegram_access.ROLE_ADMIN

@pytest.fixture
def user_bot(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "TELEGRAM_AI_LIMITS_FILE", str(tmp_path / "ai.json"))
    monkeypatch.setattr(config, "TELEGRAM_AI_FREE_ANALYSES", 1)
    monkeypatch.setattr(config, "TELEGRAM_BOT_STATE_FILE", str(tmp_path / "bot-state.json"))
    bot = telegram_bot.TelegramBot("qa")
    monkeypatch.setattr(bot, "_selected_profile", lambda principal: "qa")
    monkeypatch.setattr(bot, "_profile", lambda name: SimpleNamespace(home_dir=str(tmp_path)))
    monkeypatch.setattr(bot, "_active_command", lambda profile: None)
    monkeypatch.setattr(bot, "_has_active_command", lambda profile: False)
    monkeypatch.setattr(bot, "_menu_reply_markup", lambda *a, **kw: {})
    monkeypatch.setattr(bot, "_send_text", AsyncMock(return_value={}))
    monkeypatch.setattr(bot, "_send_text_safely", AsyncMock(return_value={}))
    monkeypatch.setattr(bot, "_run_active_command_capture", AsyncMock(return_value={"ok": True}))
    monkeypatch.setattr(bot, "_mark_active_command", lambda **kw: SimpleNamespace(task=None))
    monkeypatch.setattr(bot, "_clear_active_command", lambda *a: None)
    monkeypatch.setattr(bot, "_append_chat_ai_audit_event", lambda *a, **kw: None)
    return bot, {"role": ui.ROLE_USER, "user_id": 42, "profile": "qa"}


@pytest.fixture
def owned_user_chat(user_bot, tmp_path):
    from tests.chat_ownership_fakes import owned_telegram_chats
    owned_telegram_chats(tmp_path, ["123"])
    return user_bot


def test_only_agreed_commands_are_opened():
    assert ui.ADMIN_ONLY_COMMANDS == {
        "/direction_csv",  # /direction_stats открыт клиенту (DIR-3: статистика своих резюме)
        "/menu_llm", "/llm_balance", "/llm_test", "/profiles", "/profile", "/users", "/grant", "/revoke",
        "/digest", "/backfill", "/daemon_on", "/daemon_off", "/ai_limits", "/ai_grant", "/ai_reset",
        "/clients", "/client_approve", "/client_reject", "/client_hh_auth", "/menu_admin", "/cancel_active", "/diagnostics",
        "/market_research", "/seen_reset",
    }


@pytest.mark.parametrize("command,method", [("/forms", "_list_forms"), ("/chat_ai", "_start_chat_ai_candidate_list"),
    ("/ai_chat", "_start_chat_ai_candidate_list"), ("/chat_answer", "_start_chat_ai_candidate_list"),
    ("/grabresume", "_start_cli_command")])
def test_opened_commands_reach_handlers_for_regular_user(user_bot, monkeypatch, command, method):
    bot, principal = user_bot
    handler = AsyncMock()
    monkeypatch.setattr(bot, method, handler)
    asyncio.run(bot._dispatch(42, principal, command, ""))
    handler.assert_awaited_once()
    if command == "/forms":
        bot._send_text.assert_awaited_once_with(42, "📝 Анкеты", reply_markup={})
    else:
        bot._send_text.assert_not_awaited()


@pytest.mark.parametrize("command", sorted(ui.ADMIN_ONLY_COMMANDS - {"/cancel_active"}))
def test_other_admin_commands_remain_denied(user_bot, command):
    bot, principal = user_bot
    asyncio.run(bot._dispatch(42, principal, command, ""))
    assert "Нужны права администратора" in bot._send_text.call_args.args[1]
    bot._run_active_command_capture.assert_not_awaited()


@pytest.mark.parametrize("kind", ["chat", "alternative", "form"])
def test_user_ai_operations_share_existing_limit_and_stop_before_subprocess(user_bot, kind):
    bot, principal = user_bot
    assert limits.consume_ai_usage(42, profile_name="qa", action="used")
    async def run():
        if kind == "form":
            await bot._start_google_form_command(42, principal, profile_name="qa", label="preview", argv=["--google-form-preview"])
        else:
            await bot._start_chat_ai_reply(42, principal, profile_name="qa", hh_chat_id="123", hh_message_id="456", alternative=kind == "alternative")
    asyncio.run(run())
    assert "Лимит ИИ исчерпан" in bot._send_text.call_args.args[1]
    bot._run_active_command_capture.assert_not_awaited()
    assert limits.get_user_snapshot(42)["free_used"] == 1


@pytest.mark.parametrize("kind", ["chat", "form"])
def test_granted_credit_allows_one_user_ai_operation(owned_user_chat, kind):
    bot, principal = owned_user_chat
    assert limits.consume_ai_usage(42, profile_name="qa", action="used")
    limits.grant_bonus(42, amount=1, actor_user_id=1)
    async def run():
        if kind == "chat":
            await bot._start_chat_ai_reply(42, principal, profile_name="qa", hh_chat_id="123", hh_message_id="456")
        else:
            await bot._start_google_form_command(42, principal, profile_name="qa", label="preview", argv=["--google-form-preview"])
        await asyncio.sleep(0)
    asyncio.run(run())
    bot._run_active_command_capture.assert_awaited_once()
    assert limits.get_user_snapshot(42)["free_used"] == 2
    assert limits.get_user_snapshot(42)["available_soft"] == 0


def test_user_can_send_approved_chat_draft_without_another_ai_credit(owned_user_chat):
    bot, principal = owned_user_chat
    assert limits.consume_ai_usage(42, profile_name="qa", action="chat_ai_used")
    async def run():
        await bot._start_chat_ai_reply(42, principal, profile_name="qa", hh_chat_id="123",
                                     hh_message_id="456~abcdef123456", force_send=True)
        await asyncio.sleep(0)
    asyncio.run(run())
    bot._run_active_command_capture.assert_awaited_once()
    assert limits.get_user_snapshot(42)["free_used"] == 1


def test_ai_credits_cannot_be_double_spent_across_threads(user_bot):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=4) as pool:
        result = list(pool.map(lambda i: limits.consume_ai_usage(42, profile_name=f"p{i}", action="chat_ai_used"), range(8)))
    assert sum(result) == 1
    assert limits.get_user_snapshot(42)["free_used"] == 1


def test_ai_credit_does_not_consume_another_users_allowance(user_bot):
    assert limits.consume_ai_usage(42, profile_name="qa", action="chat_ai_used")
    assert limits.get_user_snapshot(43)["available_soft"] == 1


@pytest.mark.parametrize("menu,parent", [(ui.MENU_EXPERIENCE, ui.MENU_SEARCH_CONDITIONS),
    (ui.MENU_SEARCH_CONDITIONS, ui.MENU_SEARCH), (ui.MENU_COMPANIES, ui.MENU_SEARCH),
    (ui.MENU_APPLICATION_MODE, ui.MENU_SEARCH), (ui.MENU_REPEAT, ui.MENU_SEARCH),
    (ui.MENU_LOGS, ui.MENU_MONITOR), (ui.MENU_RESPONSES, ui.MENU_MAIN), (ui.MENU_LLM, ui.MENU_ADMIN)])
def test_back_follows_menu_tree(user_bot, monkeypatch, menu, parent):
    bot, principal = user_bot
    monkeypatch.setattr(bot, "_selected_menu", lambda principal: menu)
    monkeypatch.setattr(bot, "_send_menu", AsyncMock())
    asyncio.run(bot._dispatch(42, principal, "/back", ""))
    assert bot._send_menu.call_args.kwargs["menu"] == parent


def test_new_diagnosis_action_uses_latest_selected_profile_run(user_bot, monkeypatch):
    bot, principal = user_bot
    run = {"ok": True, "applied": 0, "found": 3, "reason_breakdown": {"low_score": 3}}
    monkeypatch.setattr(bot, "_latest_run", lambda profile: run if profile == "qa" else None)
    asyncio.run(bot._dispatch(42, principal, "/why_no_apply", ""))
    assert bot._send_text.call_args.args[1] == analytics.zero_apply_diagnosis(run)


@pytest.mark.parametrize('reason,retryable', [('unsupported_claims', True), ('model_unavailable', True),
                                              ('stale_question', False), ('attempt_blocked', False)])
@pytest.mark.parametrize('allow_any', [False, True])
def test_chat_preview_failure_is_honest_and_retry_preserves_approval_lane(owned_user_chat, monkeypatch, reason, retryable, allow_any):
    import json
    from chat_reply_result import reply_result
    bot, principal = owned_user_chat
    metadata = reply_result({'ok': False, 'reason': reason, 'dry_run': True})
    bot._run_active_command_capture.return_value = {
        'ok': True, 'returncode': 0, 'stdout': json.dumps({'chat_reply_result': metadata})}
    async def run():
        await bot._start_chat_ai_reply(42, principal, profile_name='qa', hh_chat_id='123',
                                     hh_message_id='456', allow_any=allow_any)
        await asyncio.sleep(0)
    asyncio.run(run())
    text = bot._send_text.call_args.args[1]
    assert text.startswith('Не удалось подготовить ответ: ')
    assert '✅' not in text and 'завершён' not in text
    markup = bot._send_text.call_args.kwargs['reply_markup']
    if retryable:
        action = 'chat_ai_any' if allow_any else 'chat_ai'
        assert markup == {'inline_keyboard': [[{'text': 'Повторить', 'callback_data': f'{action}:qa:123:456'}]]}
    else:
        assert markup == {}
