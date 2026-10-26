"""Manual HH attestations close uncertain cards without dispatching an apply."""
import asyncio
import json
import threading
import time
from unittest.mock import AsyncMock

import pytest

import manual_apply_queue as queue
from state_store.native_apply import NativeApplyRepository
from tests.test_manual_apply_recovery import homes, applying
from tests.test_telegram_callbacks import _callback, _configured_bot
from tests.test_bot_profile_isolation import synthetic_profiles, dev_bot, tester


URL = "https://hh.ru/vacancy/123"


def uncertain(*, profile="qa", orphan=False):
    if orphan:
        item = applying(profile=profile)
        return queue.get_candidate(item["token"], profile_name=profile)
    item = queue.create_candidate({"id": "123", "url": URL, "title": "Synthetic vacancy"},
                                  {"score": 70, "reason": "Synthetic reason"}, profile_name=profile)
    return queue.mark_candidate(item["token"], "uncertain", profile_name=profile)


def press(bot, data, *, user_id=1, private=True, message_id=7):
    asyncio.run(bot._handle_callback_query(_callback(
        data, user_id=user_id, message_id=message_id,
        chat_type="private" if private else "group")))


@pytest.mark.parametrize("orphan", [False, True])
def test_every_uncertain_card_offers_both_attestations_why_and_navigation(homes, orphan):
    item = uncertain(orphan=orphan)
    buttons = [b for row in queue.build_review_card_markup(item, "qa")["inline_keyboard"] for b in row]
    labels = {b["text"] for b in buttons}
    assert {"Проверил — отклик есть", "Проверил — отклика нет", "❓ Почему", "◀", "▶", "☰ Вся очередь"} <= labels
    assert not any(b.get("callback_data", "").startswith("manual_apply:") for b in buttons)


@pytest.mark.parametrize("orphan", [False, True])
def test_exists_attestation_closes_card_once_without_apply(homes, monkeypatch, orphan):
    item = uncertain(orphan=orphan)
    bot, _ = _configured_bot(monkeypatch)
    monkeypatch.setattr(bot, "_start_manual_ai_apply", AsyncMock())
    data = f"mar:qa:{item['token']}:handled"
    press(bot, data)
    saved = queue.get_candidate(item["token"], profile_name="qa")
    assert saved["status"] == "handled" and saved["resolved_by"] == 1
    before = queue._queue_path("qa").read_bytes()
    press(bot, data)
    press(bot, data, message_id=8)
    queue.mark_candidate(item["token"], "pending", profile_name="qa")
    assert queue.claim_candidate(item["token"], store=queue._store("qa")) is None
    assert queue._queue_path("qa").read_bytes() == before
    bot._start_manual_ai_apply.assert_not_awaited()


@pytest.mark.parametrize("saved_uncertainty", [False, True])
def test_not_sent_confirmation_only_creates_fresh_pending_card_once(homes, monkeypatch, saved_uncertainty):
    item = uncertain()
    if saved_uncertainty:
        (homes["qa"] / "seen_vacancies.json").write_text(json.dumps({"123": {"action": "apply_uncertain"}}))
    bot, calls = _configured_bot(monkeypatch)
    monkeypatch.setattr(bot, "_start_manual_ai_apply", AsyncMock())
    prefix = f"mar:qa:{item['token']}:"
    press(bot, prefix + "retry")
    assert queue.get_candidate(item["token"], profile_name="qa")["status"] == "uncertain"
    assert len(queue._read_queue("qa")["items"]) == 1
    assert "Подтверждаете" in calls["messages"][-1][1]
    press(bot, prefix + "confirm")
    original = queue.get_candidate(item["token"], profile_name="qa")
    assert original["status"] == "handled"
    retry = queue.get_candidate(original["retry_token"], profile_name="qa")
    assert retry["status"] == "pending" and retry["retry_of"] == item["token"]
    assert any(b.get("callback_data") == f"manual_apply:qa:{retry['token']}"
               for row in calls["messages"][-1][2]["inline_keyboard"] for b in row)
    before = queue._queue_path("qa").read_bytes()
    press(bot, prefix + "confirm")
    press(bot, prefix + "confirm", message_id=8)
    assert queue._queue_path("qa").read_bytes() == before
    assert len(queue._read_queue("qa")["items"]) == 2
    bot._start_manual_ai_apply.assert_not_awaited()


def test_uncertain_why_is_read_only_and_keeps_card_return(homes, monkeypatch):
    item = uncertain()
    bot, _ = _configured_bot(monkeypatch)
    edit = AsyncMock()
    monkeypatch.setattr(bot, "_edit_text", edit)
    monkeypatch.setattr(bot, "_review_visible_items", lambda *a, **kw: pytest.fail("Why needs no queue scan"))
    before = queue._queue_path("qa").read_bytes()
    press(bot, f"rq:w:qa:{item['token']}")
    assert "Synthetic reason" in edit.call_args.args[2]
    assert edit.call_args.kwargs["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == f"rq:c:qa:{item['token']}"
    assert queue._queue_path("qa").read_bytes() == before


def test_attested_successor_still_yields_to_new_confirmed_history(homes):
    item = uncertain()
    path = homes["qa"] / "seen_vacancies.json"
    path.write_text(json.dumps({"123": {"action": "apply_uncertain"}}))
    retry = queue.resolve_orphaned_apply(item["token"], profile_name="qa", user_id=1, confirmed_not_sent=True)
    assert [row["token"] for row in queue.list_candidates("qa")] == [retry["token"]]
    path.write_text(json.dumps({"123": {"action": "applied"}}))
    assert queue.list_candidates("qa") == []
    assert queue.get_candidate(retry["token"], profile_name="qa")["status"] == "already_applied"


def test_uncertain_navigation_offloads_queue_scans(homes, monkeypatch):
    item = uncertain()
    bot, _ = _configured_bot(monkeypatch)
    monkeypatch.setattr(bot, "_edit_text", AsyncMock())
    main_thread = threading.get_ident()
    original = bot._review_visible_items
    calls = []
    def checked(*args, **kwargs):
        assert threading.get_ident() != main_thread
        calls.append(True)
        return original(*args, **kwargs)
    monkeypatch.setattr(bot, "_review_visible_items", checked)
    press(bot, f"rq:v:qa:{item['token']}")
    assert calls


@pytest.mark.parametrize("conflict", ["native_completed", "native_live", "native_foreign", "queue_live", "seen_confirmed"])
def test_not_sent_cannot_override_confirmed_or_other_live_attempt(homes, conflict):
    item = uncertain()
    if conflict == "seen_confirmed":
        (homes["qa"] / "seen_vacancies.json").write_text(json.dumps({"123": {"action": "applied"}}))
    elif conflict == "queue_live":
        applying(live=True)
    else:
        repository = NativeApplyRepository(homes["qa"] / "hh_cookies.json", "hh")
        owner = repository.claim(URL, "", "")
        if conflict == "native_completed":
            repository.transition(URL, owner, "completed")
        elif conflict == "native_foreign":
            repository.store.update(lambda data: data[repository.key(URL)].update(
                owner_pid=None, started_at=time.time() - 301, manual_token="other", manual_owner="other"))
    before = queue._queue_path("qa").read_bytes()
    assert queue.resolve_orphaned_apply(item["token"], profile_name="qa", user_id=1, confirmed_not_sent=True) is None
    assert queue._queue_path("qa").read_bytes() == before


@pytest.mark.parametrize("private,profile", [(False, "qa"), (True, "other")])
def test_attestation_rejects_group_or_foreign_profile(homes, monkeypatch, private, profile):
    item = uncertain(profile=profile)
    bot, _ = _configured_bot(monkeypatch, {"user_id": 1, "role": "user", "profile": "qa"})
    monkeypatch.setattr(bot, "_profile_names", lambda: ["qa", "other"])
    before = queue._queue_path(profile).read_bytes()
    press(bot, f"mar:{profile}:{item['token']}:handled", private=private)
    assert queue._queue_path(profile).read_bytes() == before


def test_tester_cannot_attest_other_direction(tester, monkeypatch):
    bot, _ = tester
    item = queue.create_candidate({"id": "123", "url": URL, "_direction_selection": {"direction": "electric"}},
                                  {}, profile_name="qa-dev")
    queue.mark_candidate(item["token"], "uncertain", profile_name="qa-dev")
    before = queue._queue_path("qa-dev").read_bytes()
    monkeypatch.setattr(bot, "_start_manual_ai_apply", AsyncMock())
    press(bot, f"mar:qa-dev:{item['token']}:handled", user_id=202)
    assert queue._queue_path("qa-dev").read_bytes() == before
    bot._start_manual_ai_apply.assert_not_awaited()
