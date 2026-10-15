"""Exercise every persistent button through real dispatch with offline dependencies."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import config
import profile as profile_mod
import telegram_bot
import telegram_bot_ui as ui
from runtime_context import TelegramRuntimePaths
from tests.test_telegram_bot_ui import ADMIN_SCREENS, COMMON_SCREENS, MENU_COMMANDS


def button_cases():
    for role in (ui.ROLE_USER, ui.ROLE_ADMIN):
        for menu in [*COMMON_SCREENS, *(ADMIN_SCREENS if role == ui.ROLE_ADMIN else {})]:
            for row in ui.build_reply_markup(role, menu=menu)["keyboard"]:
                for button in row:
                    if button["text"] not in {ui.BUTTON_BACK, ui.BUTTON_MENU}:
                        yield role, menu, button["text"]


@pytest.fixture
def menu_bot(tmp_path, monkeypatch):
    profile = profile_mod.Profile(name="qa", home_dir=str(tmp_path), resume_file=str(tmp_path / "resume.md"))
    (tmp_path / "resume.md").write_text("Synthetic resume", encoding="utf-8")
    paths = TelegramRuntimePaths(
        bot_pid_file=profile.telegram_bot_pid_file, bot_state_file=profile.telegram_bot_state_file,
        bot_runtime_file=profile.telegram_bot_runtime_file, bot_log_file=profile.telegram_bot_log_file,
        bot_debug_log_file=profile.telegram_bot_debug_log_file,
    )
    monkeypatch.setattr(config, "TELEGRAM_AI_LIMITS_FILE", str(tmp_path / "ai.json"))
    monkeypatch.setattr(config, "TELEGRAM_AI_FREE_ANALYSES", 100)
    bot = telegram_bot.TelegramBot("qa", runtime_paths=paths)
    monkeypatch.setattr(bot, "_profile", lambda name: profile)
    monkeypatch.setattr(bot, "_profile_names", lambda: ["qa"])
    monkeypatch.setattr(bot, "_selected_profile", lambda principal: "qa")
    monkeypatch.setattr(bot, "_client_record", lambda user_id: {"status": "pending"})
    monkeypatch.setattr(bot, "_active_command", lambda name: None)
    monkeypatch.setattr(bot, "_has_active_command", lambda name: False)
    monkeypatch.setattr(bot, "_clear_active_command", lambda name: None)
    monkeypatch.setattr(bot, "_daemon_state", lambda name: {"running": False, "pid": 0})
    monkeypatch.setattr(bot, "_bot_state", lambda: {"running": False})
    monkeypatch.setattr(bot, "_runtime_status", lambda name: None)
    monkeypatch.setattr(bot, "_latest_run", lambda name: None)
    monkeypatch.setattr(bot, "_collect_diagnostics", AsyncMock(return_value=[]))
    monkeypatch.setattr(bot, "_reload_profile_daemon", lambda name: False)
    monkeypatch.setattr(bot, "_send_text", AsyncMock(return_value={}))
    monkeypatch.setattr(bot, "_send_text_safely", bot._send_text)
    monkeypatch.setattr(bot, "_send_chat_action", AsyncMock())
    monkeypatch.setattr(bot, "_send_document", AsyncMock())
    monkeypatch.setattr(bot, "_run_active_command_capture", AsyncMock(return_value={"ok": True, "stdout": ""}))
    monkeypatch.setattr(bot, "_analysis_document_payload", lambda *a: ("analysis.md", b"synthetic"))
    workers = []
    def mark(**kwargs):
        worker = SimpleNamespace(task=None)
        workers.append(worker)
        return worker
    monkeypatch.setattr(bot, "_mark_active_command", mark)
    monkeypatch.setattr(profile_mod, "update_profile_env", lambda *a, **kw: str(tmp_path / "settings.env"))
    monkeypatch.setattr(telegram_bot.client_hh_auth, "load_hh_resume_catalog", lambda name: [])
    monkeypatch.setattr(telegram_bot.manual_apply_queue, "list_candidates", lambda *a, **kw: [])
    monkeypatch.setattr(telegram_bot.company_blacklist, "list_companies", lambda name: [])
    monkeypatch.setattr(telegram_bot.telegram_clients, "list_clients", lambda: [{"user_id": 43, "status": telegram_bot.telegram_clients.STATUS_PENDING_REVIEW}])
    monkeypatch.setattr(telegram_bot.telegram_access, "list_users", lambda: [])
    monkeypatch.setattr(telegram_bot.hh_response_counter, "refresh", lambda **kw: {"total": 0})
    monkeypatch.setattr(telegram_bot.candidate_interview, "build_plan", AsyncMock(return_value={"questions": []}))
    monkeypatch.setattr(telegram_bot.candidate_interview, "render_facts", lambda **kw: "Synthetic facts")
    monkeypatch.setattr(telegram_bot.candidate_interview, "facts", lambda *a: [])
    monkeypatch.setattr(telegram_bot.search_query_suggester, "suggest_queries", AsyncMock(return_value=["QA engineer"]))
    monkeypatch.setattr(telegram_bot.admin_llm, "overview", lambda: "Synthetic overview")
    monkeypatch.setattr(telegram_bot.admin_llm, "diagnostic", AsyncMock(return_value="Synthetic diagnostic"))
    monkeypatch.setattr(telegram_bot.runtime_control, "start_background_process", lambda *a, **kw: {"ok": True, "pid": 123, "already_running": False})
    monkeypatch.setattr(telegram_bot.runtime_control, "stop_process", lambda *a, **kw: {"pid": 123})
    return bot, workers


@pytest.mark.parametrize("role,menu,label", list(button_cases()))
def test_every_button_returns_its_owner_or_target_submenu(menu_bot, role, menu, label):
    bot, workers = menu_bot
    principal = {"user_id": 42, "role": role, "profile": "qa"}
    bot._set_selected_menu(42, menu)
    command, arg = ui._resolve_message_command(label, role)
    async def run():
        await bot._dispatch(42, principal, command, arg)
        for worker in workers:
            if worker.task:
                await worker.task
    asyncio.run(run())
    expected = MENU_COMMANDS.get(command, menu)
    assert bot._selected_menu(principal) == expected
    keyboards = [call.kwargs["reply_markup"] for call in bot._send_text.call_args_list + bot._send_document.call_args_list
                 if "keyboard" in (call.kwargs.get("reply_markup") or {})]
    assert keyboards, (command, bot._send_text.call_args_list)
    if command == "/profiles":
        # The existing one-time profile picker is this button's target screen.
        assert keyboards[-1] == ui.build_reply_markup(role, profile_buttons=["qa"])
    elif command in MENU_COMMANDS:
        assert all(markup == ui.build_reply_markup(role, menu=expected) for markup in keyboards)
    else:
        assert all(label in [b["text"] for row in markup["keyboard"] for b in row] for markup in keyboards)


@pytest.mark.parametrize("role,menu,label", list(button_cases()))
def test_direct_command_recovers_owner_from_another_selected_menu(menu_bot, role, menu, label):
    bot, _ = menu_bot
    principal = {"user_id": 42, "role": role, "profile": "qa"}
    # Slash commands and cached keyboards can arrive after another section was selected.
    bot._set_selected_menu(42, ui.MENU_MONITOR if menu != ui.MENU_MONITOR else ui.MENU_SEARCH)
    command, arg = ui._resolve_message_command(label, role)
    async def run():
        await bot._dispatch(42, principal, command, arg)
        for worker in menu_bot[1]:
            if worker.task:
                await worker.task
    asyncio.run(run())
    assert bot._selected_menu(principal) == MENU_COMMANDS.get(command, menu)


@pytest.mark.parametrize("command", ["/analyze", "/search_suggest"])
@pytest.mark.parametrize("access", ["exhausted", "free", "bonus", "admin"])
def test_analysis_and_suggestions_use_the_shared_ai_limit(menu_bot, command, access):
    import telegram_resume_limits as limits
    bot, workers = menu_bot
    principal = {"user_id": 42, "role": ui.ROLE_ADMIN if access == "admin" else ui.ROLE_USER, "profile": "qa"}
    limits.reset_free_limit(42, free_total=1)
    if access != "free":
        assert limits.consume_ai_usage(42, profile_name="qa", action="form_ai_used")
    if access == "bonus":
        limits.grant_bonus(42, amount=1)
    before = limits.get_user_snapshot(42)
    async def run():
        await bot._dispatch(42, principal, command, "")
        for worker in workers:
            if worker.task:
                await worker.task
    asyncio.run(run())
    if access == "exhausted":
        assert not workers
        bot._run_active_command_capture.assert_not_awaited()
        telegram_bot.search_query_suggester.suggest_queries.assert_not_awaited()
        assert "Лимит ИИ исчерпан" in bot._send_text.call_args.args[1]
        assert limits.get_user_snapshot(42) == before
    else:
        assert len(workers) == 1
        after = limits.get_user_snapshot(42)
        assert after["free_used"] - before["free_used"] == (0 if access == "admin" else 1)
        if command == "/analyze":
            bot._run_active_command_capture.assert_awaited_once()
            assert after["analysis_total"] == 1
        else:
            telegram_bot.search_query_suggester.suggest_queries.assert_awaited_once()
        if access in {"free", "bonus"}:
            assert after["available_soft"] == 0
            # Both commands spend the same allowance, so the other one must now stop.
            other = "/search_suggest" if command == "/analyze" else "/analyze"
            asyncio.run(bot._dispatch(42, principal, other, ""))
            assert len(workers) == 1
            assert "Лимит ИИ исчерпан" in bot._send_text.call_args.args[1]


@pytest.mark.parametrize("command", ["/analyze", "/search_suggest"])
def test_busy_ai_commands_do_not_reserve_a_credit(menu_bot, monkeypatch, command):
    import telegram_resume_limits as limits
    bot, workers = menu_bot
    principal = {"user_id": 42, "role": ui.ROLE_USER, "profile": "qa"}
    monkeypatch.setattr(bot, "_active_command", lambda name: SimpleNamespace(label="busy", owner_user_id=42))
    monkeypatch.setattr(bot, "_has_active_command", lambda name: True)
    monkeypatch.setattr(bot, "_send_busy_status", AsyncMock())
    before = limits.get_user_snapshot(42)
    asyncio.run(bot._dispatch(42, principal, command, ""))
    bot._send_busy_status.assert_awaited_once()
    assert not workers
    assert limits.get_user_snapshot(42) == before


@pytest.mark.parametrize("role,menu,label", [
    case for case in button_cases()
    if ui._resolve_message_command(case[2], case[0])[0] not in {*MENU_COMMANDS, "/help"}
])
def test_moving_any_leaf_button_changes_its_dispatch_menu(menu_bot, monkeypatch, role, menu, label):
    original = ui.reply_menu_layouts
    target = ui.MENU_HH if menu != ui.MENU_HH else ui.MENU_SEARCH
    def rearranged(role):
        layouts = original(role)
        for row in layouts[menu]:
            if label in row:
                row.remove(label)
        layouts[target].append([label])
        return layouts
    monkeypatch.setattr(ui, "reply_menu_layouts", rearranged)
    test_every_button_returns_its_owner_or_target_submenu(menu_bot, role, target, label)


def test_previous_llm_button_label_still_routes_to_the_same_command():
    assert ui._resolve_message_command("🧪 Тест", ui.ROLE_ADMIN) == ("/llm_test", "")


@pytest.mark.parametrize("role", [ui.ROLE_USER, ui.ROLE_ADMIN])
@pytest.mark.parametrize("command,menu,label", [
    ("/hh_response_count", ui.MENU_RESPONSES, ui.BUTTON_HH_RESPONSES),
    ("/hunter_log", ui.MENU_LOGS, ui.BUTTON_LOG),
    ("/hunter_chat_log", ui.MENU_LOGS, ui.BUTTON_CHAT_LOG),
    ("/ai_chat", ui.MENU_RESPONSES, ui.BUTTON_CHAT_AI),
    ("/chat_answer", ui.MENU_RESPONSES, ui.BUTTON_CHAT_AI),
])
def test_alias_command_returns_the_same_owner(menu_bot, role, command, menu, label):
    bot, workers = menu_bot
    principal = {"user_id": 42, "role": role, "profile": "qa"}
    bot._set_selected_menu(42, ui.MENU_MONITOR)
    async def run():
        await bot._dispatch(42, principal, command, "")
        for worker in workers:
            if worker.task:
                await worker.task
    asyncio.run(run())
    assert bot._selected_menu(principal) == menu
    keyboards = [call.kwargs["reply_markup"] for call in bot._send_text.call_args_list
                 if "keyboard" in (call.kwargs.get("reply_markup") or {})]
    assert keyboards
    assert all(label in [b["text"] for row in markup["keyboard"] for b in row] for markup in keyboards)
