"""The grouped routing data remains complete and independently editable."""
import asyncio
from unittest.mock import AsyncMock

import pytest

import telegram_bot_ui as ui
from telegram_app.routing import COMMAND_ROUTES, ROUTE_GROUPS, CommandRoute, route_for_command
from telegram_bot import TelegramBot
from tests.test_telegram_menu_routing import menu_bot  # noqa: F401


def test_route_groups_have_unique_commands_and_existing_handlers():
    commands = [command for routes in ROUTE_GROUPS.values() for command in routes]
    assert len(commands) == len(set(commands))
    assert set(COMMAND_ROUTES) == set(commands)
    for route in COMMAND_ROUTES.values():
        assert callable(getattr(TelegramBot, route.handler))


@pytest.mark.parametrize("button_map", [ui.ADMIN_BUTTON_MAP, ui.USER_BUTTON_MAP, ui.LEGACY_BUTTON_MAP])
def test_every_authenticated_keyboard_command_has_a_route(button_map):
    for command in button_map.values():
        # Client signup belongs to _dispatch_guest, which is outside this split.
        if command != "/client_start":
            assert route_for_command(command) is not None, command


def test_a_route_can_be_added_with_only_table_data(menu_bot, monkeypatch):
    bot, _ = menu_bot
    monkeypatch.setitem(COMMAND_ROUTES, "/synthetic_menu", CommandRoute("_route_menu", args=(ui.MENU_LOGS,)))
    monkeypatch.setattr(bot, "_send_menu", AsyncMock())
    principal = {"user_id": 42, "role": ui.ROLE_USER, "profile": "qa"}
    asyncio.run(bot._dispatch(42, principal, "/synthetic_menu", ""))
    bot._send_menu.assert_awaited_once_with(42, principal, profile_name="qa", menu=ui.MENU_LOGS)
