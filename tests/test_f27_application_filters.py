"""Response filters traverse emitted callbacks over saved applications only."""
import asyncio
import json
from pathlib import Path

import pytest

import analytics
import directions
import telegram_access
from tests.test_f27_critic_navigation import buttons, press
from tests.test_bot_profile_isolation import synthetic_profiles, dev_bot
from tests.direction_view_fakes import two_directions


@pytest.fixture
def saved_responses(dev_bot, monkeypatch):
    bot = dev_bot
    def external(*args, **kwargs):
        pytest.fail("Saved-history filters must not refresh HH or run commands")
    monkeypatch.setattr(bot, "_start_cli_command", external)
    monkeypatch.setattr("hh_response_counter.refresh", external)
    from unittest.mock import AsyncMock
    monkeypatch.setattr(bot, "_send_chat_action", AsyncMock())
    profile = bot._profile("qa-dev")
    two_directions(profile.home_dir)
    rows = []
    now = analytics._now().isoformat()
    for identity, owner, bucket in [("Sent item", "qa", "pending"),
                                    ("Invited item", "qa", "positive"),
                                    ("Rejected item", "qa", "rejected"),
                                    ("Foreign item", "electric", "positive")]:
        common = {"vacancy_id": identity, "source": "hh", "direction": owner,
                  "run_id": "saved", "title": identity, "company": "Synthetic", "created_at": now}
        rows.extend([{**common, "event": "application_result", "outcome": "sent"},
                     {**common, "event": "decision", "decision": "applied_auto"},
                     {**common, "event": "negotiation_status", "status_bucket": bucket}])
    rows += [{"event": "negotiation_status", "vacancy_id": "Status without send", "title": "Status without send",
              "direction": "qa", "status_bucket": "positive", "created_at": now}]
    Path(profile.analytics_events_file).write_text("".join(json.dumps(row) + "\n" for row in rows))
    return bot, profile


@pytest.mark.parametrize("role", ["admin", "user", "tester"])
@pytest.mark.parametrize("label,expected", [("📨 Все", {"Sent item", "Invited item", "Rejected item"}),
                                            ("📬 Приглашения", {"Invited item"}),
                                            ("❌ Отказы", {"Rejected item"})])
def test_emitted_filters_list_only_matching_saved_applications(saved_responses, role, label, expected):
    bot, _ = saved_responses
    telegram_access.upsert_user(42, profile="qa-dev", role=role, direction="qa")
    principal = telegram_access.resolve_user(42)
    async def scenario():
        await bot._dispatch(42, principal, "/menu_responses", "")
        data = next(b["callback_data"] for b in buttons(bot._send_text.call_args_list) if b["text"] == label)
        for _ in range(2):
            calls = await press(bot, data)
            text = "\n".join(call.args[1] for call in calls)
            for title in ("Sent item", "Invited item", "Rejected item", "Foreign item", "Status without send"):
                assert (title in text) == (title in expected), text
            assert "1–" not in text
    asyncio.run(scenario())


@pytest.mark.parametrize("binding", ["direction", "profile"])
def test_filter_callback_rejects_changed_direction_and_profile(saved_responses, binding):
    bot, profile = saved_responses
    principal = telegram_access.upsert_user(42, profile="qa-dev", role="admin")
    async def scenario():
        await bot._dispatch(42, principal, "/menu_responses", "")
        data = next(b["callback_data"] for b in buttons(bot._send_text.call_args_list) if b["text"] == "📨 Все")
        if binding == "direction":
            directions.set_active_direction(profile.home_dir, "electric")
        else:
            bot._set_selected_profile(42, "synthetic-dev")
        calls = await press(bot, data)
        assert any("изменился" in call.args[1] for call in calls)
        assert not any("item" in call.args[1] for call in calls)
    asyncio.run(scenario())


def test_filtered_history_pages_through_emitted_controls(saved_responses):
    bot, profile = saved_responses
    principal = telegram_access.upsert_user(42, profile="qa-dev", role="user")
    with Path(profile.analytics_events_file).open("a") as stream:
        for number in range(8):
            stream.write(json.dumps({"event": "application_result", "outcome": "sent", "direction": "qa",
                                    "title": f"Paged {number}", "vacancy_id": f"page-{number}",
                                    "created_at": analytics._now().isoformat()}) + "\n")
    async def scenario():
        await bot._dispatch(42, principal, "/menu_responses", "")
        data = next(b["callback_data"] for b in buttons(bot._send_text.call_args_list) if b["text"] == "📨 Все")
        calls = await press(bot, data)
        first = "\n".join(c.args[1] for c in calls)
        next_page = next(b["callback_data"] for b in buttons(calls) if b["text"] == "▶")
        calls = await press(bot, next_page)
        second = "\n".join(c.args[1] for c in calls)
        assert first != second
        assert len(first) < 4096 and len(second) < 4096
        back = next(b["callback_data"] for b in buttons(calls) if b["text"] == "◀")
        calls = await press(bot, back)
        assert first == "\n".join(c.args[1] for c in calls)
    asyncio.run(scenario())


@pytest.mark.parametrize("failure", ["group", "revoke", "direction", "filter", "page"])
def test_application_paging_refuses_stale_or_unauthorized_payloads(saved_responses, failure):
    from telegram_app import history
    from tests.test_telegram_callbacks import _callback
    bot, profile = saved_responses
    telegram_access.upsert_user(42, profile="qa-dev", role="user")
    data = f"apps:{history.token(profile.home_dir)}:all:1"
    if failure == "revoke":
        telegram_access.upsert_user(42, profile="qa-dev", role="user", enabled=False)
    elif failure == "direction":
        directions.set_active_direction(profile.home_dir, "electric")
    elif failure == "filter":
        data = data.replace(":all:", ":unknown:")
    elif failure == "page":
        data = data[:-1] + "-1"
    asyncio.run(bot._handle_callback_query(_callback(data, user_id=42, chat_id=42,
        chat_type="group" if failure == "group" else "private")))
    assert not any("item" in call.args[1] for call in bot._send_text.call_args_list)
    assert bot._answer_callback_query.called
