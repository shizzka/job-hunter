"""User instructions name reachable actions and keep operations private, offline."""
import asyncio
import re
from unittest.mock import AsyncMock

import pytest
import telegram_bot_ui as ui

from telegram_app.callback_handlers import CallbackContext
from telegram_app.callback_handlers.search import handle_search, stop_token
from telegram_app.subprocesses import ActiveCommandState
from tests.test_telegram_menu_routing import menu_bot

USER = {"user_id": 42, "role": "user", "profile": "qa"}


def test_actual_busy_status_attaches_stop_action(menu_bot, monkeypatch):
    from telegram_app.callback_handlers.search import stop_token
    bot, _ = menu_bot
    active = ActiveCommandState("search", "qa", 42, 123.0, subprocess_pid=8123)
    monkeypatch.setattr(bot, "_active_command", lambda name: active)
    asyncio.run(bot._send_busy_status(42, USER, profile_name="qa"))
    call = bot._send_text.call_args
    assert "«⏹ Остановить»" in call.args[1]
    assert call.kwargs["reply_markup"] == {"inline_keyboard": [[{
        "text": "⏹ Остановить", "callback_data": "fs:stop:" + stop_token("qa", active)}]]}
    assert "8123" not in call.args[1] and "PID" not in call.args[1]


@pytest.mark.parametrize("role,owner", [("user", 99), ("tester", 99)])
def test_busy_status_without_cancel_permission_has_no_stop_instruction(menu_bot, monkeypatch, role, owner):
    bot, _ = menu_bot
    active = ActiveCommandState("search", "qa", owner, 123.0, subprocess_pid=8123)
    monkeypatch.setattr(bot, "_active_command", lambda name: active)
    asyncio.run(bot._send_busy_status(42, {**USER, "role": role}, profile_name="qa"))
    call = bot._send_text.call_args
    assert "Остановить" not in call.args[1] and "8123" not in call.args[1]
    assert "inline_keyboard" not in call.kwargs["reply_markup"]


def test_cancel_refusal_also_hides_other_command_pid(menu_bot, monkeypatch):
    bot, _ = menu_bot
    active = ActiveCommandState('search', 'qa', 99, 123.0, subprocess_pid=8123)
    monkeypatch.setattr(bot, '_active_command', lambda name: active)
    asyncio.run(bot._cancel_active_command(42, USER, profile_name='qa'))
    text = bot._send_text.call_args.args[1]
    assert '8123' not in text and 'PID' not in text and 'Остановить' not in text


@pytest.mark.parametrize("refusal", [None, "access", "owner", "group", "stale", "profile"])
def test_emitted_busy_stop_uses_existing_guards_and_never_stops_new_run(menu_bot, monkeypatch, refusal):
    bot, _ = menu_bot
    active = ActiveCommandState("search", "qa", 42, 123.0)
    state = {"active": active}
    monkeypatch.setattr(bot, "_active_command", lambda name: state["active"])
    monkeypatch.setattr(bot, "_answer_callback_query", AsyncMock())
    async def cancel(*args, **kwargs):
        state["active"] = None
    cancel_mock = AsyncMock(side_effect=cancel)
    monkeypatch.setattr(bot, "_cancel_active_command", cancel_mock)
    asyncio.run(bot._send_busy_status(42, USER, profile_name="qa"))
    data = bot._send_text.call_args.kwargs["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
    if refusal == "owner":
        active.owner_user_id = 99
    elif refusal == "stale":
        state["active"] = ActiveCommandState("search", "qa", 42, 124.0)
    elif refusal == "profile":
        monkeypatch.setattr(bot, "_selected_profile", lambda p: "other")
    context = CallbackContext("cb", 42, USER, data,
        {"chat": {"type": "group" if refusal == "group" else "private"}},
        42, 7, lambda name: refusal != "access" and name == "qa")
    for _ in range(2):
        asyncio.run(handle_search(bot, context))
    assert cancel_mock.await_count == (0 if refusal else 1)
    assert "завершён или недоступен" in bot._answer_callback_query.call_args.args[1]


def test_busy_and_progress_share_token_and_admin_keeps_pid(menu_bot, monkeypatch):
    bot, workers = menu_bot
    active = ActiveCommandState("search", "qa", 42, 123.0, subprocess_pid=8123)
    monkeypatch.setattr(bot, "_active_command", lambda name: active)
    asyncio.run(bot._send_busy_status(42, {**USER, "role": "admin"}, profile_name="qa"))
    assert "• PID: 8123" in bot._send_text.call_args.args[1]
    busy_markup = bot._send_text.call_args.kwargs["reply_markup"]
    bot._send_text.reset_mock()
    monkeypatch.setattr(bot, "_active_command", lambda name: None)
    async def run():
        await bot._start_cli_command(42, USER, "qa", "--search", "search", 10)
        for worker in workers:
            await worker.task
    asyncio.run(run())
    progress = next(call for call in bot._send_text.call_args_list if call.args[1].startswith("⏳") and
                    (call.kwargs.get("reply_markup") or {}).get("inline_keyboard"))
    assert progress.kwargs["reply_markup"] == busy_markup


@pytest.mark.parametrize("role", ["user", "tester", "admin"])
@pytest.mark.parametrize("command", ["/repeat_daily", "/repeat_off", "/schedule"])
def test_schedule_repeated_render_keeps_technical_details_admin_only(menu_bot, monkeypatch, role, command):
    bot, _ = menu_bot
    principal = {**USER, "role": role}
    monkeypatch.setattr(bot, "_update_profile_schedule", lambda *a, **kw: "/srv/private/profile.env")
    # Invoke real handlers: dispatch permissions are covered by menu/guard tests.
    handler = {"/repeat_daily": bot._route_repeat, "/repeat_off": bot._route_repeat_off,
               "/schedule": bot._route_schedule}[command]
    for _ in range(2):
        asyncio.run(handler(42, principal, command, "", "qa", None))
        text = bot._send_text.call_args.args[1]
        if role == "admin" and command == "/repeat_daily":
            assert "daemon pid: 123" in text and "/srv/private/profile.env" in text
        else:
            assert "pid" not in text.lower() and "profile.env" not in text
        if command in {"/repeat_off", "/schedule"}:
            assert "«🔎 Найти вакансии»" in text


@pytest.mark.parametrize("role", ["user", "tester", "admin"])
def test_actual_log_runs_and_completion_hide_run_ids_for_clients(menu_bot, monkeypatch, role):
    from run_summary import build_run_summary
    bot, _ = menu_bot
    principal = {**USER, "role": role}
    run = {"run_id": "internal-run-123", "mode": "search", "ok": True,
           "found": 3, "applied": 1, "created_at": "2026-10-26T08:00:00+00:00"}
    monkeypatch.setattr("analytics.get_run_summary", lambda *a, **kw: build_run_summary(run))
    monkeypatch.setattr(bot, "_recent_runs", lambda *a, **kw: [run])
    monkeypatch.setattr(bot, "_latest_run", lambda *a, **kw: run)
    async def show():
        await bot._dispatch(42, principal, "/log", "")
        await bot._dispatch(42, principal, "/runs", "")
    asyncio.run(show())
    texts = [call.args[1] for call in bot._send_text.call_args_list]
    texts.append(bot._cli_command_result("qa", principal, "--search", "search", {"ok": True}, role)[0])
    if role == "admin":
        assert all("internal-run-123" in text for text in texts if "Последний" in text or "Последние" in text)
        assert any("/log run_id" in text for text in texts)
    else:
        assert all("internal-run-123" not in text and "/log run_id" not in text for text in texts)


def assert_quoted_actions(texts, visible, *, content=()):
    """Every quote in these renders is classified; content is explicitly named."""
    quotes = {quote for text in texts for quote in re.findall("«([^»]+)»", text)}
    assert set(content) <= quotes
    assert quotes - set(content) <= visible, quotes - set(content) - visible


@pytest.mark.parametrize("role", ["user", "admin"])
def test_quoted_button_names_match_actual_reachable_keyboards(menu_bot, monkeypatch, role):
    from tests.test_f27_critic_navigation import traverse_menu_callbacks
    from tests.test_telegram_bot_ui import COMMON_SCREENS, ADMIN_SCREENS, MENU_COMMANDS
    from telegram_app.responses import _build_chat_ai_candidates_text
    from hh_response_counter import unavailable_text, HHResponseCounterProfileError
    import directions
    from tests.direction_view_fakes import make_direction
    bot, _ = menu_bot
    principal = {**USER, "role": role}
    monkeypatch.setattr("telegram_access.resolve_user", lambda uid: principal)
    monkeypatch.setattr(bot, "_answer_callback_query", AsyncMock())
    screens = dict(COMMON_SCREENS)
    screens.pop(ui.MENU_MONITOR)
    if role != "admin":
        screens.pop(ui.MENU_LOGS)
    if role == "admin":
        screens[ui.MENU_SETTINGS] = [*screens[ui.MENU_SETTINGS], ui.BUTTON_MENU_ADMIN]
        screens.update(ADMIN_SCREENS)
    asyncio.run(traverse_menu_callbacks(bot, principal, screens, MENU_COMMANDS))
    # The action vocabulary comes from the traversed user-visible keyboards,
    # never from a command map (which contains deliberately hidden old labels).
    visible = {b["text"].removeprefix("✓ ") for call in bot._send_text.call_args_list
               for kind in ("keyboard", "inline_keyboard")
               for row in (call.kwargs.get("reply_markup") or {}).get(kind, []) for b in row}
    async def prompts():
        await bot._direction_callback(42, principal, "dir:stale:save")
        await bot._direction_command_save(42, principal, "qa", bot._profile("qa"), None, None)
        await bot._route_repeat_off(42, principal, "/repeat_off", "", "qa", None)
        await bot._route_hh_resumes(42, principal, "/hh_resumes", "", "qa", None)
        if role == "admin":
            await bot._dispatch(42, principal, "/ai_limits", "")
    asyncio.run(prompts())
    make_direction(bot._profile("qa").home_dir)
    directions.set_active_direction(bot._profile("qa").home_dir, "qa")
    asyncio.run(bot._dispatch(42, principal, "/directions", ""))
    texts = [call.args[1] for call in bot._send_text.call_args_list]
    texts += [unavailable_text(), unavailable_text(HHResponseCounterProfileError()),
              _build_chat_ai_candidates_text({"ok": False, "url": "https://hh.ru/account/login"})]
    assert_quoted_actions(texts, visible, content={"QA Engineer"})
    # User-supplied quoted direction/company names aren't button instructions.
    assert_quoted_actions(["Направление «Acme QA»; компания «Acme»"], visible,
                          content={"Acme QA", "Acme"})
