"""The callback table stays complete and preserves exact/prefix matching."""
import asyncio
import json
from pathlib import Path

import pytest

from telegram_app.callback_handlers.chats import _lists_several_chats
from telegram_app.callback_routing import (
    CALLBACK_ROUTES,
    CALLBACK_ROUTE_GROUPS,
    CallbackRoute,
    route_for_callback,
)
from telegram_app.callbacks import _lists_several_chats as legacy_lists_several_chats
from tests.test_telegram_callbacks import _callback, _configured_bot


def test_groups_have_unique_prefixes_and_characterized_handlers():
    prefixes = [prefix for routes in CALLBACK_ROUTE_GROUPS.values() for prefix in routes]
    assert len(prefixes) == len(set(prefixes))
    assert set(CALLBACK_ROUTES) == set(prefixes)
    cases = json.loads((Path(__file__).with_name("fixtures") / "telegram_callbacks.json").read_text())["cases"]
    for prefix, route in CALLBACK_ROUTES.items():
        assert callable(route.handler)
        assert route.phase in {"navigation", "profile", "normal"}
        assert route.user_access in {"", "manual", "ai"}
        if prefix != "*":
            assert any(case["data"].startswith(prefix) and route_for_callback(case["data"]) is route
                       for case in cases), prefix


@pytest.mark.parametrize("payload,prefix", [
    ("kbu:save", "kbu:save"),
    ("kbu:cancel", "kbu:cancel"),
    ("kbu:save:old", "*"),
    ("gs", "*"),
    ("gs:unknown", "gs:"),
    ("chat_ai_any_alt:qa:1:2", "chat_ai_any_alt:"),
    ("chat_ai_any:qa:1:2", "chat_ai_any:"),
    ("chat_send_any:qa:1:2~abcdef123456", "chat_send_any:"),
    (" ca:42 ", "*"),
    ("unknown:42", "*"),
])
def test_callback_lookup_keeps_payload_boundaries(payload, prefix):
    assert route_for_callback(payload) is CALLBACK_ROUTES[prefix]


def test_a_callback_can_be_added_with_only_table_data(monkeypatch):
    bot, calls = _configured_bot(monkeypatch)

    async def handler(bot, context):
        await bot._send_text(context.chat_id, "synthetic callback")

    monkeypatch.setitem(CALLBACK_ROUTES, "synthetic:", CallbackRoute(handler, phase="navigation"))
    asyncio.run(bot._handle_callback_query(_callback("synthetic:payload")))
    assert calls == {"answers": [], "edits": [], "messages": [(42, "synthetic callback", None)]}


def test_chat_keyboard_helper_keeps_its_old_import_path():
    assert legacy_lists_several_chats is _lists_several_chats
