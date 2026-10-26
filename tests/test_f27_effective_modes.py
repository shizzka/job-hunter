"""Launch review follows the same direction inheritance as the search pipeline."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import directions
from tests.direction_view_fakes import two_directions
from tests.test_f27_search import USER, context, payload
from tests.test_telegram_menu_routing import menu_bot
from tests.test_bot_profile_isolation import synthetic_profiles, dev_bot, tester


@pytest.fixture
def inherited_searches(menu_bot, monkeypatch):
    bot, workers = menu_bot
    base = bot._profile("qa")
    two_directions(Path(base.home_dir))
    explicit = directions.load_direction(base.home_dir, "qa")
    directions.save_direction(base.home_dir, {**explicit, "application_mode": "confirm"})
    base.hh.application_mode = "auto"
    monkeypatch.setattr(bot, "_profile", lambda name: directions.overlay_profile(base))
    monkeypatch.setattr(bot, "_answer_callback_query", AsyncMock())
    return bot, base, workers


async def choose(bot, selection):
    from telegram_app.callback_handlers.search import handle_search
    await bot._route_find(42, USER, "/find", "", "qa", None)
    rows = payload(bot)
    # Directions are sorted by name: electric inherits the base; qa overrides it.
    index = {"electric": 0, "qa": 1, "all": 2}[selection]
    await handle_search(bot, context(rows[index][0]["callback_data"]))


@pytest.mark.parametrize("selection", ["electric", "all"])
@pytest.mark.parametrize("mode", ["auto", "confirm", "preview"])
def test_omitted_mode_inherits_base_even_when_active_direction_overrides_it(inherited_searches, selection, mode):
    bot, base, _ = inherited_searches
    base.hh.application_mode = mode
    assert bot._profile("qa").hh.application_mode == "confirm"
    assert "application_mode" not in directions.load_direction(base.home_dir, "electric")
    asyncio.run(choose(bot, selection))
    text = bot._send_text.call_args.args[1]
    assert "Поиск: Электромонтажник\nРежим: " + bot._application_mode_label(mode) in text
    assert any("Только показать результаты" in b["text"] for row in payload(bot) for b in row) == (mode == "auto")


def test_explicit_confirm_does_not_offer_dry_run_under_base_auto(inherited_searches):
    bot, _, _ = inherited_searches
    asyncio.run(choose(bot, "qa"))
    assert "Режим: Подтверждать отклик" in bot._send_text.call_args.args[1]
    assert len(payload(bot)) == 1


def test_single_direction_without_mode_offers_inherited_auto_launch(inherited_searches):
    bot, base, _ = inherited_searches
    directions.delete_direction(base.home_dir, "qa", expected=directions.load_direction(base.home_dir, "qa"))
    directions.set_active_direction(base.home_dir, "electric")
    asyncio.run(bot._route_find(42, USER, "/find", "", "qa", None))
    assert "Поиск: Электромонтажник\nРежим: Автоматически" in bot._send_text.call_args.args[1]
    assert "Только показать результаты" in payload(bot)[1][0]["text"]


@pytest.mark.parametrize("change", ["base_mode", "salary", "remote", "queries", "direction_mode", "active"])
def test_review_refuses_changed_base_or_effective_settings(inherited_searches, monkeypatch, change):
    from telegram_app.callback_handlers.search import handle_search
    bot, base, _ = inherited_searches
    monkeypatch.setattr(bot, "_start_cli_command", AsyncMock())
    asyncio.run(choose(bot, "all"))
    data = payload(bot)[0][0]["callback_data"]
    if change == "base_mode":
        base.hh.application_mode = "preview"
    elif change == "salary":
        base.hh.search_salary = 150000
    elif change == "remote":
        base.hh.search_remote_only = not base.hh.search_remote_only
    elif change == "queries":
        base.hh.search_queries = ["Changed base query"]
    elif change == "direction_mode":
        item = directions.load_direction(base.home_dir, "electric")
        directions.save_direction(base.home_dir, {**item, "application_mode": "preview"})
    else:
        directions.set_active_direction(base.home_dir, "electric")
    asyncio.run(handle_search(bot, context(data)))
    asyncio.run(handle_search(bot, context(data)))
    bot._start_cli_command.assert_not_awaited()
    assert "устарела" in bot._answer_callback_query.call_args.args[1]


@pytest.mark.parametrize("setting,value", [("application_mode", "preview"), ("search_salary", 150000)])
def test_queued_search_refuses_changed_inherited_settings_before_switch(inherited_searches, setting, value):
    bot, base, _ = inherited_searches
    asyncio.run(choose(bot, "electric"))
    state = bot._search_settings_state(42)["find_launch"]
    setattr(base.hh, setting, value)
    result = asyncio.run(bot._run_selected_searches(
        SimpleNamespace(cancel_requested=False), "qa", "--search", 10,
        state["selected"], state["snapshot"]))
    assert not result["ok"]
    bot._run_active_command_capture.assert_not_awaited()
    assert directions.active_direction(base.home_dir)["name"] == "qa"


@pytest.mark.parametrize("selection", ["electric", "all"])
@pytest.mark.parametrize("dry", [False, True])
def test_inherited_auto_launches_selected_searches_once_through_real_router(inherited_searches, monkeypatch, selection, dry):
    from tests.test_telegram_callbacks import _callback
    bot, base, workers = inherited_searches
    monkeypatch.setattr("telegram_access.resolve_user", lambda uid: USER)
    monkeypatch.setattr(bot, "_delete_message", AsyncMock())
    async def scenario():
        await choose(bot, selection)
        rows = payload(bot)
        data = rows[-1 if dry else 0][0]["callback_data"]
        await bot._handle_callback_query(_callback(data, user_id=42, chat_type="private"))
        await bot._handle_callback_query(_callback(data, user_id=42, chat_type="private"))
        alternate = data.replace(":dry:", ":go:") if dry else data.replace(":go:", ":dry:")
        await bot._handle_callback_query(_callback(alternate, user_id=42, message_id=8, chat_type="private"))
        for worker in workers:
            if worker.task:
                await worker.task
    asyncio.run(scenario())
    assert bot._run_active_command_capture.await_count == (2 if selection == "all" else 1)
    assert all(("--dry-run" if dry else "--search") in call.args[1]
               for call in bot._run_active_command_capture.call_args_list)
    assert directions.active_direction(base.home_dir)["name"] == "qa"
    assert not bot._search_settings_state(42).get("find_launch")


@pytest.mark.parametrize("change", ["revoke", "profile", "direction", "active"])
def test_tester_launch_revalidates_access_and_binding(tester, monkeypatch, change):
    import telegram_access
    from tests.test_telegram_callbacks import _callback
    bot, principal = tester
    monkeypatch.setattr(bot, "_start_cli_command", AsyncMock())
    asyncio.run(bot._dispatch(202, principal, "/find", ""))
    text = bot._send_text.call_args.args[1]
    assert "Поиск: QA Engineer" in text
    assert "Электромонтажник" not in text and "Все поиски" not in text
    data = payload(bot)[0][0]["callback_data"]
    if change == "revoke":
        telegram_access.upsert_user(202, profile="qa-dev", role="tester", direction="qa", enabled=False)
    elif change == "profile":
        telegram_access.upsert_user(202, profile="synthetic-dev", role="user")
    elif change == "direction":
        telegram_access.upsert_user(202, profile="qa-dev", role="tester", direction="electric")
    else:
        directions.set_active_direction(bot._profile("qa-dev").home_dir, "electric")
    asyncio.run(bot._handle_callback_query(_callback(data, user_id=202, chat_type="private")))
    bot._start_cli_command.assert_not_awaited()
