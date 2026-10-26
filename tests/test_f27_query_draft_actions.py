"""Completed query drafts expose usable, revision-bound Save/Edit controls."""
import asyncio
from unittest.mock import Mock, AsyncMock

import pytest

import directions
import telegram_access
import telegram_bot
from tests.test_telegram_menu_routing import menu_bot
from tests.test_f27_critic_navigation import buttons, press
from tests.direction_view_fakes import make_direction


@pytest.mark.parametrize("action", ["save", "edit"])
@pytest.mark.parametrize("role", ["admin", "user"])
def test_completed_draft_actions_traverse_router_and_cannot_repeat(menu_bot, monkeypatch, action, role):
    bot, workers = menu_bot
    monkeypatch.setattr(bot, "_answer_callback_query", AsyncMock())
    principal = telegram_access.upsert_user(42, profile="qa", role=role)
    writes = Mock(return_value="synthetic.env")
    monkeypatch.setattr(telegram_bot.profile_mod, "update_profile_env", writes)
    async def scenario():
        await bot._dispatch(42, principal, "/search_suggest", "")
        for worker in workers:
            await worker.task
        completed = [c for c in bot._send_text.call_args_list if "ИИ предложил черновик" in c.args[1]][-1]
        emitted = {b["text"]: b["callback_data"] for b in buttons([completed])}
        assert set(emitted) == {"✅ Сохранить предложенное", "✏️ Изменить запросы"}
        data = emitted["✅ Сохранить предложенное" if action == "save" else "✏️ Изменить запросы"]
        await press(bot, data)
        if action == "save":
            writes.assert_called_once_with("qa", {"HH_SEARCH_QUERIES": "QA engineer"})
        else:
            writes.assert_not_called()
            assert bot._search_settings_state(42)["search_input"] == "queries"
        await press(bot, data)
        await press(bot, data, message_id=8)
        assert writes.call_count == (1 if action == "save" else 0)
        assert not bot._search_settings_state(42).get("search_query_draft")
    asyncio.run(scenario())


@pytest.mark.parametrize("change", ["replacement", "direction", "revoke", "group", "replacement_during_ack", "direction_during_ack", "revoke_during_ack"])
def test_draft_controls_reject_stale_or_unauthorized_press(menu_bot, monkeypatch, change):
    from tests.test_telegram_callbacks import _callback
    bot, workers = menu_bot
    monkeypatch.setattr(bot, "_answer_callback_query", AsyncMock())
    principal = telegram_access.upsert_user(42, profile="qa", role="user")
    writes = Mock(return_value="synthetic.env")
    monkeypatch.setattr(telegram_bot.profile_mod, "update_profile_env", writes)
    async def scenario():
        await bot._dispatch(42, principal, "/search_suggest", "")
        await workers[-1].task
        completed = [c for c in bot._send_text.call_args_list if "ИИ предложил черновик" in c.args[1]][-1]
        data = next(b["callback_data"] for b in buttons([completed]) if b["text"] == "✅ Сохранить предложенное")
        if change == "replacement":
            await bot._dispatch(42, principal, "/search_suggest", "")
            await workers[-1].task
        elif change == "direction":
            make_direction(bot._profile("qa").home_dir)
            directions.set_active_direction(bot._profile("qa").home_dir, "qa")
        elif change == "revoke":
            telegram_access.upsert_user(42, profile="qa", role="user", enabled=False)
        if change.endswith("_during_ack"):
            async def change_during_ack(*args, **kwargs):
                if change == "replacement_during_ack":
                    bot._set_search_settings_state(42, search_query_draft_token="replacement")
                elif change == "direction_during_ack":
                    make_direction(bot._profile("qa").home_dir)
                    directions.set_active_direction(bot._profile("qa").home_dir, "qa")
                else:
                    telegram_access.upsert_user(42, profile="qa", role="user", enabled=False)
            monkeypatch.setattr(bot, "_answer_callback_query", change_during_ack)
        await bot._handle_callback_query(_callback(data, user_id=42, chat_id=42, message_id=9,
                                                  chat_type="group" if change == "group" else "private"))
        writes.assert_not_called()
    asyncio.run(scenario())
