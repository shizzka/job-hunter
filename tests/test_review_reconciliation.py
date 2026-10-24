"""B36: reconcile review cards from synthetic local evidence, without HH."""
import asyncio
import json
import time
from types import SimpleNamespace

import pytest

import manual_apply_queue as queue
import seen
import telegram_bot


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


def write_journal(path, records):
    path.write_text("\n".join(json.dumps(row) for row in records) + "\n", encoding="utf-8")


@pytest.fixture
def homes(tmp_path, monkeypatch):
    homes = {name: tmp_path / name for name in ("qa", "other")}
    for home in homes.values():
        home.mkdir()
    monkeypatch.setattr(queue, "_queue_path", lambda profile_name=None:
                        homes[profile_name or "qa"] / "manual_apply_queue.json")
    monkeypatch.setattr(queue.company_blacklist, "_path", lambda profile_name=None:
                        homes[profile_name or "qa"] / "company_blacklist.json")
    monkeypatch.setattr(telegram_bot.TelegramBot, "_profile", lambda self, name:
                        SimpleNamespace(home_dir=str(homes[name])))
    return homes


def card(identity="123", *, profile="qa", **vacancy):
    return queue.create_candidate({"id": identity, "title": "Synthetic vacancy", **vacancy},
                                  {"score": 10}, profile_name=profile)


@pytest.mark.parametrize("action", ["applied", "already_applied", "manual_ai_applied", "apply_uncertain"])
@pytest.mark.parametrize("key", ["123", "hh:123", "direction:old:123", "direction:old:hh:123"])
def test_seen_outcomes_survive_later_skips_and_direction_changes(homes, action, key):
    item = card("hh:123", _direction_selection={"direction": "new"})
    write_json(homes["qa"] / "seen_vacancies.json", {
        key: {"action": action},
        "direction:new:123": {"action": "skipped_low_score", "vacancy_id": "123"},
    })

    listed = queue.list_candidates("qa")
    saved = queue.get_candidate(item["token"], profile_name="qa")
    if action == "apply_uncertain":
        assert [row["token"] for row in listed] == [item["token"]]
        assert saved["status"] == "uncertain"
        assert "Нужна сверка" in saved["message"]
    else:
        assert listed == []
        assert saved["status"] == "already_applied"
    assert queue.claim_candidate(item["token"], store=queue._store("qa")) is None


@pytest.mark.parametrize("filename,record", [
    ("negotiation_snapshots.jsonl", {"vacancyId": 123, "lastState": "response", "filter": "active"}),
    ("negotiation_snapshots.jsonl", {"vacancyId": "123", "archived": True, "filter": "archived"}),
    ("analytics_events.jsonl", {"event": "negotiation_observation", "vacancy_id": "hh:123", "source": "hh", "status_bucket": "pending"}),
    ("analytics_events.jsonl", {"event": "negotiation_status", "vacancy_id": "123", "source": "hh", "status": "Не просмотрен"}),
    ("analytics_events.jsonl", {"event": "negotiation_observation", "vacancyId": 123, "source": "hh"}),
])
def test_saved_negotiation_removes_card_without_seen_or_refresh(homes, monkeypatch, filename, record):
    import hh.negotiation_snapshot as snapshot
    from hh_client import HHClient

    def forbidden(*args, **kwargs):
        raise AssertionError("Listing must not call HH")

    monkeypatch.setattr(snapshot, "collect", forbidden)
    monkeypatch.setattr(snapshot, "ensure_fresh", forbidden)
    monkeypatch.setattr(snapshot, "refresh_for_chats", forbidden)
    monkeypatch.setattr(HHClient, "get_negotiation_statuses", forbidden)
    item = card()
    path = homes["qa"] / filename
    # Old/partial observations still prove presence. Completeness only matters
    # for absence; a later empty summary must not reopen an earlier response.
    write_journal(path, [dict(record, fetched_at="2026-10-19T12:00:00+03:00"),
                         {"kind": "snapshot_summary", "complete": True, "total": 0}])
    with path.open("a", encoding="utf-8") as stream:
        stream.write('broken tail\nnull\n[]\n')
    before = path.read_bytes()
    assert queue.list_candidates("qa") == []
    assert queue.get_candidate(item["token"], profile_name="qa")["status"] == "already_applied"
    assert path.read_bytes() == before


def test_reconciliation_is_profile_and_source_scoped_and_precedes_limit(homes, monkeypatch):
    # A different active config and another profile's history cannot consume qa.
    write_json(homes["other"] / "seen_vacancies.json", {"999": {"action": "applied"}})
    monkeypatch.setattr(seen.config, "SEEN_VACANCIES_FILE", str(homes["other"] / "seen_vacancies.json"))
    kept = card("999")
    applied = card("123")
    non_hh = card("123", source="superjob")
    other = card("123", profile="other")
    write_json(homes["qa"] / "seen_vacancies.json", {"123": {"action": "applied"}})
    assert len(queue.list_candidates("qa", limit=1)) == 1
    listed = queue.list_candidates("qa", limit=250)
    assert {row["token"] for row in listed} == {kept["token"], non_hh["token"]}
    assert queue.get_candidate(applied["token"], profile_name="qa")["status"] == "already_applied"
    assert queue.get_candidate(other["token"], profile_name="other")["status"] == "pending"


def test_offers_and_unrelated_observations_remain_pending(homes):
    item = card()
    write_json(homes["qa"] / "seen_vacancies.json", {
        f"direction:{index}:123": {"action": action}
        for index, action in enumerate(["manual_hh_confirm", "manual_hh_preview", "manual_yellow_score", "skipped_low_score"])
    })
    write_journal(homes["qa"] / "negotiation_snapshots.jsonl", [
        {"kind": "snapshot_summary", "vacancyId": 123}, {"vacancyId": 999}, {},
    ])
    write_journal(homes["qa"] / "analytics_events.jsonl", [
        {"event": "decision", "vacancy_id": "123", "decision": "manual_hh_confirm"},
        {"event": "negotiation_observation", "vacancy_id": "123", "source": "superjob"},
    ])
    assert [row["token"] for row in queue.list_candidates("qa")] == [item["token"]]
    assert queue.get_candidate(item["token"], profile_name="qa")["status"] == "pending"


@pytest.mark.parametrize("reverse", [False, True])
def test_uncertainty_wins_over_confirmed_history_and_is_not_pruned_or_hidden(homes, monkeypatch, reverse):
    item = card(company="Synthetic company")
    queue.snooze_candidate(item["token"], profile_name="qa")
    entries = {
        "direction:old:123": {"action": "apply_uncertain"},
        "123": {"action": "manual_ai_applied"},
    }
    write_json(homes["qa"] / "seen_vacancies.json", dict(reversed(list(entries.items()))) if reverse else entries)
    write_journal(homes["qa"] / "negotiation_snapshots.jsonl", [{"vacancyId": 123}])
    monkeypatch.setattr(queue.company_blacklist, "is_blocked", lambda *args: True)
    listed = queue.list_candidates("qa")
    assert len(listed) == 1 and listed[0]["status"] == "uncertain"
    now = time.time()
    monkeypatch.setattr(queue.time, "time", lambda: now + 8 * 24 * 3600)
    assert queue.list_candidates("qa")[0]["status"] == "uncertain"
    assert queue.mark_candidate(item["token"], "dismissed", profile_name="qa")["status"] == "uncertain"


@pytest.mark.parametrize("status", ["applying", "uncertain", "applied", "dismissed"])
def test_reconciliation_does_not_overwrite_concurrent_transition(homes, monkeypatch, status):
    item = card()
    write_json(homes["qa"] / "seen_vacancies.json", {"123": {"action": "applied"}})
    store = queue._store("qa")
    update = store.update

    def race(mutator):
        update(lambda data: data["items"][item["token"]].update(
            status=status, owner="synthetic-owner", external_started=True))
        return update(mutator)

    monkeypatch.setattr(store, "update", race)
    monkeypatch.setattr(queue, "_store", lambda *args: store)
    listed = queue.list_candidates("qa")
    assert len(listed) == (1 if status == "uncertain" else 0)
    saved = queue.get_candidate(item["token"], profile_name="qa")
    assert saved["status"] == status and saved["external_started"]


def test_pending_card_with_external_attempt_cannot_be_hidden_by_old_confirmation(homes):
    item = card()
    queue._store("qa").update(lambda data: data["items"][item["token"]].update(external_started=True))
    write_json(homes["qa"] / "seen_vacancies.json", {"123": {"action": "applied"}})
    assert queue.list_candidates("qa")[0]["status"] == "uncertain"


def test_corrupt_seen_does_not_consume_queue_or_erase_evidence(homes):
    item = card()
    path = homes["qa"] / "seen_vacancies.json"
    path.write_text("{broken", encoding="utf-8")
    with pytest.raises(RuntimeError, match="Corrupt state"):
        queue.list_candidates("qa")
    assert path.read_text(encoding="utf-8") == "{broken"
    assert queue.get_candidate(item["token"], profile_name="qa")["status"] == "pending"


def test_repeated_listing_does_not_rewrite_reconciled_queue(homes, monkeypatch):
    card()
    card("456")
    write_json(homes["qa"] / "seen_vacancies.json", {"123": {"action": "applied"}})
    assert len(queue.list_candidates("qa")) == 1

    def forbidden(*args):
        raise AssertionError("No changed outcomes; no JSON rewrite")

    monkeypatch.setattr(queue.ProtectedJsonStore, "_save_unlocked", forbidden)
    assert len(queue.list_candidates("qa")) == 1


@pytest.fixture
def crowded_queue(homes):
    now = time.time()
    items = {"old-uncertain": {
        "token": "old-uncertain", "profile_name": "qa", "status": "uncertain",
        "created_ts": now - 8 * 24 * 3600,
        "vacancy": {"id": "old", "title": "Old uncertain vacancy"},
        "evaluation": {"score": 100},
    }}
    items.update({f"pending-{index}": {
        "token": f"pending-{index}", "profile_name": "qa", "status": "pending",
        "created_ts": now - index,
        "vacancy": {"id": str(index), "title": f"Pending vacancy {index}"},
        "evaluation": {"score": 80},
    } for index in range(250)})
    write_json(homes["qa"] / "manual_apply_queue.json", {"items": items})


@pytest.mark.parametrize("limit", [8, 250])
def test_old_uncertain_is_exempt_from_listing_limit(crowded_queue, limit):
    listed = queue.list_candidates("qa", limit=limit)
    assert len(listed) == limit + 1
    assert [item["token"] for item in listed[:-1]] == [f"pending-{index}" for index in range(limit)]
    assert listed[-1]["token"] == "old-uncertain"


@pytest.mark.parametrize("sort", ["new", "score"])
def test_old_uncertain_remains_accessible_on_last_review_page(crowded_queue, monkeypatch, sort):
    bot = telegram_bot.TelegramBot("qa")
    principal = {"user_id": 42, "role": "user", "profile": "qa"}
    sent = []
    monkeypatch.setattr(bot, "_set_selected_menu", lambda *args: None)

    async def send(chat_id, text, **kwargs):
        sent.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(bot, "_send_text", send)
    asyncio.run(bot._show_review_queue(42, principal, profile_name="qa", page=41, sort=sort))
    text, markup = sent[-1]
    assert "Вакансии на проверку: 251" in text and "стр. 42/42" in text
    assert "251. Old uncertain vacancy" in text and "Нужна сверка" in text
    assert any(button.get("callback_data") == "rq:o:qa:old-uncertain"
               for row in markup["inline_keyboard"] for button in row)


def test_review_renders_uncertain_card_without_apply_or_bulk_dismiss(homes, monkeypatch):
    applied = card("123", title="Applied vacancy")
    uncertain = card("456", title="Uncertain vacancy", url="https://hh.ru/vacancy/456")
    pending = card("789", title="Pending vacancy")
    write_json(homes["qa"] / "seen_vacancies.json", {
        "123": {"action": "applied"}, "456": {"action": "apply_uncertain"},
    })
    bot = telegram_bot.TelegramBot("qa")
    principal = {"user_id": 42, "role": "user", "profile": "qa"}
    sent = []
    monkeypatch.setattr(bot, "_set_selected_menu", lambda *args: None)

    async def send(chat_id, text, **kwargs):
        sent.append((text, kwargs.get("reply_markup")))

    monkeypatch.setattr(bot, "_send_text", send)
    asyncio.run(bot._show_review_queue(42, principal, profile_name="qa"))
    assert "Вакансии на проверку: 2" in sent[-1][0]
    assert "Нужна сверка" in sent[-1][0] and "Applied vacancy" not in sent[-1][0]
    asyncio.run(bot._open_review_card(42, "qa", uncertain["token"]))
    text, markup = sent[-1]
    assert "Нужна сверка" in text
    callbacks = [button.get("callback_data", "") for row in markup["inline_keyboard"] for button in row]
    assert not any(cb.startswith(("manual_apply:", "manual_fb:", "manual_snooze:")) for cb in callbacks)
    assert any(button.get("url") for row in markup["inline_keyboard"] for button in row)
    asyncio.run(bot._review_dismiss_below(42, principal, "qa", 50, confirmed=True))
    assert queue.get_candidate(pending["token"], profile_name="qa")["status"] == "dismissed"
    assert queue.get_candidate(uncertain["token"], profile_name="qa")["status"] == "uncertain"
    assert queue.get_candidate(applied["token"], profile_name="qa")["status"] == "already_applied"
