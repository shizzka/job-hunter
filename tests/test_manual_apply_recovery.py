"""B39: recover abandoned manual cards locally, without sending or retrying HH."""
import asyncio
import os
import subprocess
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import agent
import manual_apply_queue as queue
import telegram_bot
from tests.test_telegram_callbacks import _callback, _configured_bot


@pytest.fixture
def homes(tmp_path, monkeypatch):
    homes = {name: tmp_path / name for name in ("qa", "other")}
    for home in homes.values():
        home.mkdir()
    monkeypatch.setattr(queue, "_queue_path", lambda profile_name=None:
                        homes[profile_name or "qa"] / "manual_apply_queue.json")
    monkeypatch.setattr(telegram_bot.TelegramBot, "_profile", lambda self, name:
                        SimpleNamespace(home_dir=str(homes[name])))
    return homes


def applying(*, profile="qa", legacy=False, live=False, age=None):
    item = queue.create_candidate({"id": "123", "title": "Synthetic QA", "url": "https://hh.ru/vacancy/123"},
                                  {"score": 55}, profile_name=profile)
    store = queue._store(profile)
    claimed = queue.claim_candidate(item["token"], store=store)
    assert claimed["owner_pid"] == os.getpid()
    assert claimed["owner_process_start"]
    if not live:
        child = subprocess.Popen([sys.executable, "-c", "pass"])
        child.wait(timeout=10)
    def mutate(data):
        current = data["items"][item["token"]]
        current["applying_ts"] = time.time() - (queue.ORPHAN_APPLY_GRACE_SECONDS + 1 if age is None else age)
        current["owner_pid"] = os.getpid() if live else child.pid
        current["external_started"] = True
        if legacy:
            current.pop("owner_pid")
            current.pop("owner_process_start")
            current.pop("applying_ts")
            current["created_ts"] = time.time() - queue.ORPHAN_APPLY_GRACE_SECONDS - 1
    store.update(mutate)
    return claimed


@pytest.mark.parametrize("entry", ["startup", "open", "list", "claim", "agent"])
@pytest.mark.parametrize("legacy", [False, True])
def test_dead_owner_recovers_to_uncertain(homes, monkeypatch, entry, legacy):
    item = applying(legacy=legacy)
    token = item["token"]
    bot = telegram_bot.TelegramBot("qa")
    monkeypatch.setattr(bot, "_profile_names", lambda: ["qa", "other"])
    monkeypatch.setattr(bot, "_send_text", AsyncMock())
    monkeypatch.setattr(agent, "HHClient", lambda: pytest.fail("Recovery must not open HH"))
    if entry == "startup":
        bot._recover_manual_applies()
    elif entry == "open":
        asyncio.run(bot._open_review_card(42, "qa", token))
        text = bot._send_text.call_args.args[1]
        assert queue.ORPHAN_APPLY_MESSAGE in text
        buttons = bot._send_text.call_args.kwargs["reply_markup"]["inline_keyboard"]
        assert not any(b.get("callback_data", "").startswith("manual_apply:") for row in buttons for b in row)
        assert any(b.get("callback_data", "").endswith(":retry") for row in buttons for b in row)
    elif entry == "list":
        assert [row["token"] for row in queue.list_candidates("qa")] == [token]
    elif entry == "claim":
        assert queue.claim_candidate(token) is None
    else:
        result = asyncio.run(agent.do_manual_apply_token(token))
        assert result["uncertain"] and result["message"] == queue.ORPHAN_APPLY_MESSAGE
    saved = queue._read_queue("qa")["items"][token]
    assert saved["status"] == "uncertain"
    assert saved["message"] == queue.ORPHAN_APPLY_MESSAGE
    assert not queue.approval_valid(token, item["owner"])
    assert not queue.begin_external(token, item["owner"])
    assert not queue.finish_candidate(token, item["owner"], "applied")
    assert queue.get_candidate(token)["status"] == "uncertain"


@pytest.mark.parametrize("live,age", [(True, 100000), (False, 1)])
def test_live_or_young_owner_untouched(homes, live, age):
    item = applying(live=live, age=age)
    path = queue._queue_path("qa")
    before = path.read_bytes()
    queue.recover_orphaned_applies(profile_name="qa")
    assert queue.get_candidate(item["token"])["status"] == "applying"
    assert path.read_bytes() == before


def test_live_owner_without_timestamps_is_untouched(homes):
    item = applying(live=True)
    def mutate(data):
        current = data["items"][item["token"]]
        current.pop("created_ts")
        current.pop("applying_ts")
    queue._store().update(mutate)
    path = queue._queue_path("qa")
    before = path.read_bytes()
    assert queue.get_candidate(item["token"])["status"] == "applying"
    assert path.read_bytes() == before


def test_reused_pid_does_not_keep_card_applying(homes):
    item = applying(live=True)
    queue._store().update(lambda data: data["items"][item["token"]].update(owner_process_start="previous-boot:1"))
    assert queue.get_candidate(item["token"])["status"] == "uncertain"


def test_recovery_is_profile_scoped(homes):
    item = applying(profile="other")
    queue.recover_orphaned_applies(profile_name="qa")
    assert queue._read_queue("other")["items"][item["token"]]["status"] == "applying"


def test_no_process_access_is_not_proof_of_death(homes, monkeypatch):
    item = applying(live=True)
    def denied(pid, signal):
        raise PermissionError()
    monkeypatch.setattr(queue.os, "kill", denied)
    assert queue.get_candidate(item["token"])["status"] == "applying"


def test_legacy_missing_timestamp_gets_a_grace_period(homes, monkeypatch):
    item = applying(legacy=True)
    token = item["token"]
    def mutate(data):
        data["items"][token].pop("created_ts")
    queue._store().update(mutate)
    now = time.time()
    monkeypatch.setattr(queue.time, "time", lambda: now)
    assert queue.get_candidate(token)["status"] == "applying"
    monkeypatch.setattr(queue.time, "time", lambda: now + queue.ORPHAN_APPLY_GRACE_SECONDS + 1)
    assert queue.get_candidate(token)["status"] == "uncertain"


def test_reconciliation_cannot_reopen_live_or_other_uncertain_attempt(homes):
    live = applying(live=True)
    assert queue.resolve_orphaned_apply(live["token"], profile_name="qa", user_id=42,
                                       confirmed_not_sent=True) is None
    historical = queue.create_candidate({"id": "456"}, {}, profile_name="qa")
    queue.mark_candidate(historical["token"], "uncertain")
    assert queue.resolve_orphaned_apply(historical["token"], profile_name="qa", user_id=42,
                                       confirmed_not_sent=True) is None


def test_handle_and_retry_are_atomic_and_keep_old_attempt_closed(homes):
    old = applying()
    token = old["token"]
    assert queue.resolve_orphaned_apply(token, profile_name="other", user_id=42) is None
    retry = queue.resolve_orphaned_apply(token, profile_name="qa", user_id=42, confirmed_not_sent=True)
    assert retry["status"] == "pending" and retry["token"] != token
    assert retry["retry_of"] == token
    assert not retry.get("external_started") and not retry.get("owner")
    assert queue.get_candidate(token)["status"] == "handled"
    assert queue.get_candidate(token)["resolved_by"] == 42
    assert queue.get_candidate(token)["resolution"] == "confirmed_not_sent"
    queue.mark_candidate(token, "pending")
    assert queue.get_candidate(token)["status"] == "handled"
    assert queue.claim_candidate(token) is None
    assert queue.resolve_orphaned_apply(token, profile_name="qa", user_id=42, confirmed_not_sent=True) is None
    assert queue.claim_candidate(retry["token"])
    another = applying()
    assert queue.resolve_orphaned_apply(another["token"], profile_name="qa", user_id=42)["status"] == "handled"


def test_spent_apply_button_shows_uncertainty_without_dispatch(homes, monkeypatch):
    item = applying(live=True)
    bot, calls = _configured_bot(monkeypatch)
    launch = AsyncMock()
    monkeypatch.setattr(bot, "_start_manual_ai_apply", launch)
    callback = _callback(f"manual_apply:qa:{item['token']}", chat_type="private")
    asyncio.run(bot._handle_callback_query(callback))
    assert launch.await_count == 1
    queue._store().update(lambda data: data["items"][item["token"]].update(owner_pid=None))
    asyncio.run(bot._handle_callback_query(callback))
    assert launch.await_count == 1
    assert queue.ORPHAN_APPLY_MESSAGE in calls["messages"][-1][1]


def test_retry_ui_requires_confirmation_then_separate_apply(homes, monkeypatch):
    item = applying()
    token = item["token"]
    bot, calls = _configured_bot(monkeypatch)
    launch = AsyncMock()
    monkeypatch.setattr(bot, "_start_manual_ai_apply", launch)
    def press(action):
        asyncio.run(bot._handle_callback_query(_callback(f"mar:qa:{token}:{action}", chat_type="private")))
    press("retry")
    assert queue.get_candidate(token)["status"] == "uncertain"
    assert len(queue._read_queue()["items"]) == 1
    assert "Подтверждаете" in calls["messages"][-1][1]
    press("confirm")
    assert queue.get_candidate(token)["status"] == "handled"
    assert len(queue._read_queue()["items"]) == 2
    assert launch.await_count == 0
    retry_token = queue.get_candidate(token)["retry_token"]
    asyncio.run(bot._handle_callback_query(_callback(f"manual_apply:qa:{retry_token}", chat_type="private")))
    assert launch.await_count == 1


def test_other_user_cannot_resolve_orphan(homes, monkeypatch):
    item = applying(profile="other")
    principal = {"user_id": 1, "role": "user", "profile": "qa"}
    bot, calls = _configured_bot(monkeypatch, principal)
    monkeypatch.setattr(bot, "_profile_names", lambda: ["qa", "other"])
    asyncio.run(bot._handle_callback_query(_callback(f"mar:other:{item['token']}:confirm", chat_type="private")))
    assert queue._read_queue("other")["items"][item["token"]]["status"] == "applying"
    assert len(queue._read_queue("other")["items"]) == 1
