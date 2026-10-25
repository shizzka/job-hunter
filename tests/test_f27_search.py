"""F27 launch choices use the existing guarded command pipeline, offline."""
import asyncio
from unittest.mock import AsyncMock

import pytest

from telegram_app.callback_handlers import CallbackContext
from telegram_app.routing import route_for_command
from tests.test_telegram_menu_routing import menu_bot

USER = {"user_id": 42, "role": "user", "profile": "qa"}


def context(data, *, access=True, private=True):
    return CallbackContext("cb", 42, USER, data,
                           {"chat": {"type": "private" if private else "group"}},
                           42, 7, lambda name: access)


def payload(bot):
    return bot._send_text.call_args.kwargs["reply_markup"]["inline_keyboard"]


@pytest.mark.parametrize("mode,dry", [("auto", True), ("confirm", False), ("preview", False)])
def test_launch_screen_has_dry_run_only_for_auto(menu_bot, monkeypatch, mode, dry):
    bot, _ = menu_bot
    bot._profile("qa").hh.application_mode = mode
    asyncio.run(bot._route_find(42, USER, "/find", "", "qa", None))
    rows = payload(bot)
    assert rows[0][0]["text"] == "🔎 Начать поиск"
    assert any("Только показать результаты" in b["text"] for row in rows for b in row) == dry
    assert bot._run_active_command_capture.await_count == 0
    assert route_for_command("/find").handler == "_route_find"


def test_start_callback_single_use_and_stale_profile(menu_bot, monkeypatch):
    from telegram_app.callback_handlers.search import handle_search
    bot, _ = menu_bot
    bot._profile("qa").hh.application_mode = "confirm"
    monkeypatch.setattr(bot, "_answer_callback_query", AsyncMock())
    monkeypatch.setattr(bot, "_start_cli_command", AsyncMock())
    asyncio.run(bot._route_find(42, USER, "/find", "", "qa", None))
    data = payload(bot)[0][0]["callback_data"]
    asyncio.run(handle_search(bot, context(data)))
    asyncio.run(handle_search(bot, context(data)))
    assert bot._start_cli_command.await_count == 1
    assert "устарела" in bot._answer_callback_query.call_args.args[1]
    asyncio.run(bot._route_find(42, USER, "/find", "", "qa", None))
    data = payload(bot)[0][0]["callback_data"]
    monkeypatch.setattr(bot, "_selected_profile", lambda p: "other")
    asyncio.run(handle_search(bot, context(data)))
    assert bot._start_cli_command.await_count == 1


@pytest.mark.parametrize("access,private", [(False, True), (True, False)])
def test_launch_refuses_foreign_profile_or_group(menu_bot, monkeypatch, access, private):
    from telegram_app.callback_handlers.search import handle_search
    bot, _ = menu_bot
    monkeypatch.setattr(bot, "_answer_callback_query", AsyncMock())
    monkeypatch.setattr(bot, "_start_cli_command", AsyncMock())
    asyncio.run(bot._route_find(42, USER, "/find", "", "qa", None))
    data = payload(bot)[0][0]["callback_data"]
    asyncio.run(handle_search(bot, context(data, access=access, private=private)))
    bot._start_cli_command.assert_not_awaited()


def test_directions_choice_and_all_respect_modes(menu_bot, monkeypatch):
    import directions
    from telegram_app.callback_handlers.search import handle_search
    bot, _ = menu_bot
    searches = [{"name": "qa", "title": "QA", "application_mode": "confirm"},
                {"name": "electric", "title": "Electric", "application_mode": "auto"}]
    monkeypatch.setattr(directions, "list_directions", lambda home: searches)
    monkeypatch.setattr(directions, "active_direction", lambda home: None)
    monkeypatch.setattr(bot, "_answer_callback_query", AsyncMock())
    monkeypatch.setattr(bot, "_start_cli_command", AsyncMock())
    asyncio.run(bot._route_find(42, USER, "/find", "", "qa", None))
    rows = payload(bot)
    assert [row[0]["text"] for row in rows] == ["QA", "Electric", "Все поиски"]
    asyncio.run(handle_search(bot, context(rows[-1][0]["callback_data"])))
    assert any("Только показать результаты" in b["text"] for row in payload(bot) for b in row)
    data = payload(bot)[0][0]["callback_data"]
    searches[1]["application_mode"] = "confirm"
    asyncio.run(handle_search(bot, context(data)))
    bot._start_cli_command.assert_not_awaited()


def test_selected_searches_restore_direction_on_success_and_cancel(menu_bot, monkeypatch):
    from types import SimpleNamespace
    import directions
    bot, _ = menu_bot
    active = {"value": {"name": "original"}}
    visits = []
    monkeypatch.setattr(bot, "_find_snapshot", lambda profile: "approved")
    monkeypatch.setattr(directions, "active_direction", lambda home: active["value"])
    def select(home, name):
        active["value"] = {"name": name} if name else None
    monkeypatch.setattr(directions, "set_active_direction", select)
    async def capture(*a, **kw):
        visits.append(active["value"]["name"])
        return {"ok": True, "stdout": "found"}
    monkeypatch.setattr(bot, "_run_active_command_capture", capture)
    command = SimpleNamespace(cancel_requested=False)
    result = asyncio.run(bot._run_selected_searches(command, "qa", "--search", 10, ["one", "two"], "approved"))
    assert result["ok"] and visits == ["one", "two"]
    assert active["value"] == {"name": "original"}
    async def cancelled(*a, **kw):
        raise asyncio.CancelledError()
    monkeypatch.setattr(bot, "_run_active_command_capture", cancelled)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(bot._run_selected_searches(command, "qa", "--search", 10, ["one"], "approved"))
    assert active["value"] == {"name": "original"}


def test_queued_launch_revalidates_before_switch_or_capture(menu_bot, monkeypatch):
    from types import SimpleNamespace
    bot, _ = menu_bot
    monkeypatch.setattr(bot, "_find_snapshot", lambda name: "changed")
    result = asyncio.run(bot._run_selected_searches(SimpleNamespace(cancel_requested=False), "qa", "--search", 10, ["one"], "old"))
    assert not result["ok"]
    bot._run_active_command_capture.assert_not_awaited()


def test_old_stop_cannot_cancel_a_new_search(menu_bot, monkeypatch):
    from types import SimpleNamespace
    from telegram_app.callback_handlers.search import handle_search, stop_token
    bot, _ = menu_bot
    active = SimpleNamespace(started_at=22.0)
    monkeypatch.setattr(bot, "_active_command", lambda p: active)
    monkeypatch.setattr(bot, "_can_cancel_active", lambda *a: True)
    monkeypatch.setattr(bot, "_answer_callback_query", AsyncMock())
    monkeypatch.setattr(bot, "_cancel_active_command", AsyncMock())
    asyncio.run(handle_search(bot, context("fs:stop:oldtoken")))
    bot._cancel_active_command.assert_not_awaited()
    asyncio.run(handle_search(bot, context("fs:stop:" + stop_token("qa", active))))
    bot._cancel_active_command.assert_awaited_once()


def test_duplicate_card_claim_reports_current_state(menu_bot, monkeypatch):
    import manual_apply_queue
    from telegram_app.guards import callback_guard
    bot, _ = menu_bot
    monkeypatch.setattr(bot, "_answer_callback_query", AsyncMock())
    monkeypatch.setattr("telegram_access.resolve_user", lambda uid: USER)
    monkeypatch.setattr(manual_apply_queue, "get_candidate", lambda *a, **kw: {"status": "applied"})
    calls = []
    @callback_guard
    async def mutate(self, cb):
        calls.append(cb)
    cb = {"id": "a", "from": {"id": 42}, "data": "rq:g:qa:token",
          "message": {"message_id": 7, "chat": {"id": 42, "type": "private"}}}
    asyncio.run(mutate(bot, cb))
    asyncio.run(mutate(bot, cb))
    assert len(calls) == 1
    assert "отклик отправлен" in bot._answer_callback_query.call_args.args[1]


def test_all_searches_stop_if_next_search_changes_during_first_run(menu_bot, monkeypatch):
    from types import SimpleNamespace
    import directions
    bot, _ = menu_bot
    active = {"name": "original"}
    snapshots = {"value": "approved"}
    visits = []
    monkeypatch.setattr(bot, "_find_snapshot", lambda name: snapshots["value"])
    monkeypatch.setattr(directions, "active_direction", lambda home: dict(active))
    monkeypatch.setattr(directions, "set_active_direction", lambda home, name: active.update(name=name))
    async def capture(*a, **kw):
        visits.append(active["name"])
        snapshots["value"] = "settings changed"
        return {"ok": True}
    monkeypatch.setattr(bot, "_run_active_command_capture", capture)
    result = asyncio.run(bot._run_selected_searches(SimpleNamespace(cancel_requested=False), "qa", "--search", 10, ["one", "two"], "approved"))
    assert not result["ok"] and visits == ["one"]
    assert active["name"] == "original"


@pytest.mark.parametrize('mode,dry', [('confirm', False), ('auto', False), ('auto', True)])
def test_real_router_launches_once_through_command_queue(menu_bot, monkeypatch, mode, dry):
    import runtime_control
    from tests.test_telegram_callbacks import _callback
    bot, workers = menu_bot
    bot._profile('qa').hh.application_mode = mode
    monkeypatch.setattr('telegram_access.resolve_user', lambda uid: USER)
    monkeypatch.setattr(bot, '_answer_callback_query', AsyncMock())
    monkeypatch.setattr(bot, '_delete_message', AsyncMock())
    async def scenario():
        await bot._dispatch(42, USER, '/find', '')
        rows = payload(bot)
        data = rows[-1 if dry else 0][0]['callback_data']
        callback = _callback(data, user_id=42, chat_type='private')
        await bot._handle_callback_query(callback)
        await bot._handle_callback_query(callback)
        # A copied keyboard's alternate launch action cannot create another run.
        alternate = data.replace(':dry:', ':go:') if dry else data.replace(':go:', ':dry:')
        await bot._handle_callback_query(_callback(alternate, user_id=42, message_id=8, chat_type='private'))
        for worker in workers:
            if worker.task:
                await worker.task
    asyncio.run(scenario())
    assert bot._run_active_command_capture.await_count == 1
    assert bot._run_active_command_capture.call_args.args[1] == runtime_control.agent_command_argv('qa', '--dry-run' if dry else '--search')
    assert not bot._search_settings_state(42).get('find_launch')


def test_repeat_after_tester_access_is_revoked_reports_refusal(menu_bot, monkeypatch):
    from contextlib import contextmanager
    from telegram_app.guards import _repeated_card_state
    bot, _ = menu_bot
    @contextmanager
    def revoked(principal):
        raise PermissionError('Binding removed')
        yield
    monkeypatch.setattr('profile_access.principal_scope', revoked)
    text = _repeated_card_state(bot, {'role': 'tester', 'user_id': 42},
                                {'chat': {'type': 'private'}}, 'rq:g:qa:token')
    assert 'недоступна' in text
