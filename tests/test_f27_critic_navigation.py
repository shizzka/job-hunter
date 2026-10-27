"""Exercise emitted navigation through the composed, guarded callback router."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, call

import pytest

import admin_llm
import profile as profile_mod
import telegram_access
import telegram_bot
import telegram_bot_ui as ui
from runtime_context import TelegramRuntimePaths
from tests.test_telegram_callbacks import _callback


@pytest.fixture
def navigation_bot(monkeypatch):
    # conftest places the registry, profiles and every captured state path in a
    # temporary home. Use real profile/registry/state stores, not renderer fakes.
    profile = profile_mod.create_profile("qa", search_queries=["Synthetic QA"])
    Path(profile.resume_file).write_text("Synthetic QA resume", encoding="utf-8")
    paths = TelegramRuntimePaths(
        bot_pid_file=profile.telegram_bot_pid_file, bot_state_file=profile.telegram_bot_state_file,
        bot_runtime_file=profile.telegram_bot_runtime_file, bot_log_file=profile.telegram_bot_log_file,
        bot_debug_log_file=profile.telegram_bot_debug_log_file,
    )
    bot = telegram_bot.TelegramBot("qa", runtime_paths=paths)
    monkeypatch.setattr(bot, "_send_text", AsyncMock(return_value={}))
    monkeypatch.setattr(bot, "_send_text_safely", bot._send_text)
    monkeypatch.setattr(bot, "_answer_callback_query", AsyncMock())
    # Paid diagnostics are the only external calls made by these paths.
    monkeypatch.setattr(admin_llm, "diagnostic", AsyncMock(return_value="Synthetic LLM diagnostic"))
    return bot


def register(role):
    telegram_access.upsert_user(42, profile="qa", role=role)
    return telegram_access.resolve_user(42)


def buttons(calls):
    return [button for call in calls
            for row in (call.kwargs.get("reply_markup") or {}).get("inline_keyboard", [])
            for button in row]


async def press(bot, data, *, user_id=42, message_id=7):
    start = bot._send_text.call_count
    await bot._handle_callback_query(_callback(data, user_id=user_id, chat_id=user_id,
                                              message_id=message_id, chat_type="private"))
    return bot._send_text.call_args_list[start:]


async def follow(bot, calls, command):
    emitted = [button["callback_data"] for button in buttons(calls)
               if button.get("callback_data", "").endswith(":" + command)]
    assert emitted, f"Actual response did not expose {command}"
    return await press(bot, emitted[-1])


def assert_actions(calls, expected):
    # Accept presentation's selected-mode checkmark, but require one actual
    # emitted action keyboard containing all required actions in their order.
    actual = [[button["text"].removeprefix("✓ ") for row in
               (call.kwargs.get("reply_markup") or {}).get("inline_keyboard", [])
               for button in row if button["text"] != ui.BUTTON_BACK] for call in calls]
    assert expected in actual, actual


async def traverse_menu_callbacks(bot, principal, screens, menu_commands):
    """Walk emitted local callbacks; only the four global buttons use dispatch.

    Back is resolved immediately while its source screen is selected. Other
    queued callbacks are absolute destinations bound to the same profile.
    """
    visited, queued = set(), []

    async def observe(calls):
        menu = bot._selected_menu(principal)
        if menu not in screens or menu in visited:
            return
        if menu == ui.MENU_MAIN:
            keyboards = [call.kwargs.get("reply_markup", {}) for call in calls]
            assert ui.build_reply_markup(principal["role"]) in keyboards
        else:
            assert_actions(calls, screens[menu])
        visited.add(menu)
        emitted = buttons(calls)
        for button in emitted:
            data = button.get("callback_data", "")
            command = "/" + data.rsplit(":", 1)[-1]
            target = menu_commands.get(command)
            if data.startswith("nav:") and target in screens and target not in visited:
                queued.append(data)
        # A real back callback from Schedule exposes the retained search menu.
        # This also verifies the parent links, rather than seeding hidden menus.
        parent = ui.MENU_PARENTS.get(menu)
        if parent in screens and parent not in visited:
            back = [b["callback_data"] for b in emitted if b["text"] == ui.BUTTON_BACK]
            assert back, f"No emitted way back from {menu} to {parent}"
            calls = await press(bot, back[-1])
            assert bot._selected_menu(principal) == parent
            await observe(calls)

    # Start at the actual main screen, then use its global destination buttons.
    start = bot._send_text.call_count
    await bot._dispatch(42, principal, "/menu", "")
    calls = bot._send_text.call_args_list[start:]
    await observe(calls)
    globals_ = [button["text"] for call in calls
                for row in (call.kwargs.get("reply_markup") or {}).get("keyboard", []) for button in row]
    for label in globals_:
        command, arg = ui._resolve_message_command(label, principal["role"])
        # Find and decision cards have their own callback contracts. Their
        # global buttons remain covered by the all-button dispatch tests.
        if command not in menu_commands:
            continue
        start = bot._send_text.call_count
        await bot._dispatch(42, principal, command, arg)
        await observe(bot._send_text.call_args_list[start:])
    while queued:
        data = queued.pop(0)
        target = menu_commands["/" + data.rsplit(":", 1)[-1]]
        if target not in visited:
            calls = await press(bot, data)
            assert bot._selected_menu(principal) == target
            await observe(calls)
    assert visited == set(screens)
    return visited


async def open_paid_llm(bot, principal):
    start = bot._send_text.call_count
    await bot._dispatch(42, principal, "/menu_settings", "")
    calls = await follow(bot, bot._send_text.call_args_list[start:], "menu_admin")
    assert bot._selected_menu(principal) == ui.MENU_ADMIN
    calls = await follow(bot, calls, "menu_llm")
    assert bot._selected_menu(principal) == ui.MENU_LLM
    assert_actions(calls, [ui.BUTTON_LLM_BALANCE, ui.BUTTON_LLM_TEST, ui.BUTTON_AI_LIMITS])
    return calls


def test_paid_llm_tools_remain_available_through_real_admin_callbacks(navigation_bot):
    bot = navigation_bot
    principal = register(ui.ROLE_ADMIN)

    async def scenario():
        calls = await open_paid_llm(bot, principal)
        for command in ("llm_balance", "llm_test", "ai_limits"):
            calls = await follow(bot, calls, command)
            assert bot._selected_menu(principal) == ui.MENU_LLM
            assert_actions(calls, [ui.BUTTON_LLM_BALANCE, ui.BUTTON_LLM_TEST, ui.BUTTON_AI_LIMITS])
        assert any("Лимиты ИИ" in call.args[1] for call in calls)
    asyncio.run(scenario())
    assert admin_llm.diagnostic.await_args_list == [call(), call(test=True)]


@pytest.mark.parametrize("role", [ui.ROLE_USER, ui.ROLE_ADMIN])
def test_settings_search_opens_real_direction_and_conditions(navigation_bot, role):
    import directions
    from tests.direction_view_fakes import make_direction

    bot = navigation_bot
    principal = register(role)
    home = bot._profile("qa").home_dir
    make_direction(home)
    directions.set_active_direction(home, "qa")
    before = directions.load_direction(home, "qa")

    async def scenario():
        start = bot._send_text.call_count
        await bot._dispatch(42, principal, "/menu_settings", "")
        calls = await follow(bot, bot._send_text.call_args_list[start:], "directions")
        assert any("🎯 Мои поиски" in item.args[1] for item in calls)
        choose = next(b["callback_data"] for b in buttons(calls) if b["callback_data"].endswith(":choose:0"))
        calls = await press(bot, choose)
        assert any("Поиск «QA Engineer»" in item.args[1] for item in calls)
        calls = await follow(bot, calls, "editmenu")
        assert {"Ищу по словам", "Где и как работать", "Зарплата"} <= {b["text"] for b in buttons(calls)}
        data = next(b["callback_data"] for b in buttons(calls) if b["callback_data"].endswith(":edit:work_formats"))
        calls = await press(bot, data)
        assert any("Где и как работать" in item.args[1] for item in calls)
        assert {"Готово", "Не сохранять"} <= {b["text"] for b in buttons(calls)}
    asyncio.run(scenario())
    assert directions.load_direction(home, "qa") == before
    admin_llm.diagnostic.assert_not_awaited()


@pytest.mark.parametrize("command", ["menu_admin", "menu_llm", "llm_balance", "llm_test", "ai_limits"])
def test_regular_user_cannot_reuse_emitted_admin_controls(navigation_bot, command):
    bot = navigation_bot
    principal = register(ui.ROLE_ADMIN)

    async def scenario():
        await open_paid_llm(bot, principal)
        # These are captured from actual admin responses before role revocation.
        data = next(button["callback_data"] for button in buttons(bot._send_text.call_args_list)
                    if button["callback_data"].endswith(":" + command))
        telegram_access.upsert_user(42, profile="qa", role=ui.ROLE_USER)
        user = telegram_access.resolve_user(42)
        start = bot._send_text.call_count
        await bot._dispatch(42, user, "/menu_settings", "")
        assert ui.BUTTON_MENU_ADMIN not in [b["text"] for b in buttons(bot._send_text.call_args_list[start:])]
        calls = await press(bot, data)
        assert any("недоступен" in call.args[1] for call in calls)
        assert bot._selected_menu(user) == ui.MENU_SETTINGS
        assert not buttons(calls)
    asyncio.run(scenario())
    admin_llm.diagnostic.assert_not_awaited()


def test_paid_llm_repeated_test_is_deduplicated_and_rate_limited(navigation_bot):
    bot = navigation_bot
    principal = register(ui.ROLE_ADMIN)

    async def scenario():
        calls = await open_paid_llm(bot, principal)
        data = next(b["callback_data"] for b in buttons(calls)
                    if b["callback_data"].endswith(":llm_test"))
        await press(bot, data)
        await press(bot, data)
        assert bot._answer_callback_query.call_args.args[1] == "Уже сделано"
        calls = await press(bot, data, message_id=8)
        assert any("не чаще раза в минуту" in call.args[1] for call in calls)
        assert_actions(calls, [ui.BUTTON_LLM_BALANCE, ui.BUTTON_LLM_TEST, ui.BUTTON_AI_LIMITS])
    asyncio.run(scenario())
    admin_llm.diagnostic.assert_awaited_once_with(test=True)


def test_paid_llm_submenu_can_be_reopened_from_the_same_admin_message(navigation_bot):
    bot = navigation_bot
    principal = register(ui.ROLE_ADMIN)

    async def scenario():
        calls = await open_paid_llm(bot, principal)
        data = next(b["callback_data"] for b in buttons(bot._send_text.call_args_list)
                    if b["callback_data"].endswith(":menu_llm"))
        for _ in range(2):
            calls = await follow(bot, calls, "back")
            assert bot._selected_menu(principal) == ui.MENU_ADMIN
            calls = await press(bot, data)
            assert bot._selected_menu(principal) == ui.MENU_LLM
            assert_actions(calls, [ui.BUTTON_LLM_BALANCE, ui.BUTTON_LLM_TEST, ui.BUTTON_AI_LIMITS])
    asyncio.run(scenario())
    admin_llm.diagnostic.assert_not_awaited()


@pytest.mark.parametrize("command", ["menu_llm", "llm_balance", "llm_test", "ai_limits"])
@pytest.mark.parametrize("binding", ["profile", "direction"])
def test_paid_llm_callbacks_reject_a_changed_binding(navigation_bot, command, binding):
    bot = navigation_bot
    principal = register(ui.ROLE_ADMIN)

    async def scenario():
        await open_paid_llm(bot, principal)
        data = next(button["callback_data"] for button in buttons(bot._send_text.call_args_list)
                    if button["callback_data"].endswith(":" + command))
        if binding == "profile":
            profile_mod.create_profile("other", search_queries=["Synthetic other"])
            bot._set_selected_profile(42, "other")
        else:
            import directions
            from tests.direction_view_fakes import make_direction
            home = bot._profile("qa").home_dir
            make_direction(home)
            directions.set_active_direction(home, "qa")
        calls = await press(bot, data)
        assert any("Экран изменился" in call.args[1] for call in calls)
        assert not buttons(calls)
    asyncio.run(scenario())
    admin_llm.diagnostic.assert_not_awaited()
