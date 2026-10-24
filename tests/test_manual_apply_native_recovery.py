"""B39 critic: reconciliation must reach the real native claim, without HH."""
import asyncio
import time
from types import SimpleNamespace

import pytest

import manual_apply_queue as queue
from state_store.native_apply import NativeApplyRepository, run_native_attempt
from tests.test_manual_apply_recovery import homes, applying


URL = "https://hh.ru/vacancy/123"


def abandoned_native(homes, *, status="preparing", legacy=False):
    card = applying()
    repository = NativeApplyRepository(homes["qa"] / "hh_cookies.json", "hh")
    if legacy:
        native_owner = repository.claim(URL, "", "")
    else:
        native_owner = queue.claim_native_attempt(card["token"], card["owner"], repository, URL, store=queue._store())
        assert queue.get_candidate(card["token"])["native_attempt"]["owner"] == native_owner
    if status != "preparing":
        repository.transition(URL, native_owner, status)
    def abandon(state):
        record = state[repository.key(URL)]
        record.update(started_at=time.time() - 301)
        if legacy:
            record.pop("owner_pid")
            record.pop("owner_process_start")
        else:
            record["owner_pid"] = queue._read_queue()["items"][card["token"]]["owner_pid"]
    repository.store.update(abandon)
    return card, repository, native_owner


@pytest.mark.parametrize("status", ["preparing", "acting"])
@pytest.mark.parametrize("legacy", [False, True])
def test_confirmed_retry_reaches_one_native_claim_and_preserves_history(homes, status, legacy):
    # Seed while the queue ownership is still applying; then simulate death.
    card, repository, old_owner = abandoned_native(homes, status=status, legacy=legacy)
    assert repository.claim(URL, "", "") is None
    retry = queue.resolve_orphaned_apply(card["token"], profile_name="qa", user_id=42, confirmed_not_sent=True)
    assert retry and retry["status"] == "pending"
    assert repository.get(URL)["owner"] == old_owner
    assert repository.get(URL)["status"] == status
    assert repository.claim(URL, "", "") is None  # confirmation alone never reopens automatic apply
    claim = queue.claim_candidate(retry["token"])
    client = SimpleNamespace(_manual_apply_context=(queue._store(), retry["token"], claim["owner"]))
    operations = []
    async def operation():
        operations.append(client._external_attempt.owner)
        return {"ok": False, "uncertain": True}  # bounded offline boundary: no browser or submit
    result = asyncio.run(run_native_attempt(client, repository, URL, operation))
    assert result["uncertain"]
    assert len(operations) == 1 and operations[0] != old_owner
    current = repository.get(URL)
    assert current["manual_token"] == retry["token"]
    assert current["history"][-1]["owner"] == old_owner
    assert current["history"][-1]["status"] == status
    assert current["history"][-1]["manual_retry"]["confirmed_by"] == 42
    assert not repository.claim(URL, "", "")
    asyncio.run(run_native_attempt(client, repository, URL, operation))
    assert len(operations) == 1
    assert queue.resolve_orphaned_apply(card["token"], profile_name="qa", user_id=42,
                                       confirmed_not_sent=True) is None


@pytest.mark.parametrize("conflict", ["live", "foreign", "completed"])
def test_confirmation_cannot_override_live_foreign_or_delivered_native_attempt(homes, conflict):
    card, repository, old_owner = abandoned_native(homes)
    def conflict_record(state):
        record = state[repository.key(URL)]
        if conflict == "live":
            from state_store.process_owner import current_process_owner
            record.update(current_process_owner())
        elif conflict == "foreign":
            record["manual_token"] = "another-card"
        else:
            record["status"] = "completed"
    repository.store.update(conflict_record)
    before = repository.path.read_bytes()
    assert queue.resolve_orphaned_apply(card["token"], profile_name="qa", user_id=42,
                                       confirmed_not_sent=True) is None
    assert repository.path.read_bytes() == before
    assert queue.get_candidate(card["token"])["status"] == "uncertain"
    assert len(queue._read_queue()["items"]) == 1


def test_foreign_card_cannot_consume_native_retry_grant(homes):
    card, repository, _ = abandoned_native(homes)
    retry = queue.resolve_orphaned_apply(card["token"], profile_name="qa", user_id=42, confirmed_not_sent=True)
    wrong = queue.create_candidate({"id": "123", "url": URL}, {}, profile_name="qa")
    wrong_claim = queue.claim_candidate(wrong["token"])
    assert queue.claim_native_attempt(wrong["token"], wrong_claim["owner"], repository, URL, store=queue._store()) is None
    assert repository.get(URL)["manual_retry"]["token"] == retry["token"]


def test_legacy_attempt_predating_card_is_not_reopened(homes):
    card, repository, _ = abandoned_native(homes, legacy=True)
    repository.store.update(lambda data: data[repository.key(URL)].update(started_at=time.time() - 3600))
    assert queue.resolve_orphaned_apply(card["token"], profile_name="qa", user_id=42,
                                       confirmed_not_sent=True) is None
    assert not repository.get(URL).get("manual_retry")


def test_successor_crash_before_native_claim_requires_new_confirmation(homes):
    card, repository, _ = abandoned_native(homes)
    retry = queue.resolve_orphaned_apply(card["token"], profile_name="qa", user_id=42, confirmed_not_sent=True)
    queue.claim_candidate(retry["token"])
    dead_pid = queue._read_queue()["items"][card["token"]]["owner_pid"]
    queue._store().update(lambda data: data["items"][retry["token"]].update(owner_pid=dead_pid, applying_ts=time.time() - 301))
    assert queue.get_candidate(retry["token"])["status"] == "uncertain"
    assert repository.get(URL)["manual_retry"]["token"] == retry["token"]
    assert not repository.claim(URL, "", "")
    replacement = queue.resolve_orphaned_apply(retry["token"], profile_name="qa", user_id=42, confirmed_not_sent=True)
    assert replacement["status"] == "pending"
    assert repository.get(URL)["manual_retry"]["token"] == replacement["token"]
    assert repository.get(URL)["manual_retry_history"][-1]["token"] == retry["token"]
    claim = queue.claim_candidate(replacement["token"])
    assert queue.claim_native_attempt(replacement["token"], claim["owner"], repository, URL, store=queue._store())


def test_retry_grant_is_not_created_before_owner_confirmation(homes):
    card, repository, old_owner = abandoned_native(homes, status="acting")
    assert queue.get_candidate(card["token"])["status"] == "uncertain"
    assert not repository.get(URL).get("manual_retry")
    assert not repository.claim(URL, "", "")
    queue.resolve_orphaned_apply(card["token"], profile_name="qa", user_id=42)
    assert repository.get(URL)["owner"] == old_owner
    assert not repository.get(URL).get("manual_retry")


def test_manual_agent_binds_its_card_before_native_operation(tmp_path, monkeypatch):
    import agent
    from tests.test_agent_manual_apply import _stub_setup
    token, _, _ = _stub_setup(tmp_path, monkeypatch, allow_stub=True)
    repository = NativeApplyRepository(tmp_path / "hh_cookies.json", "hh")
    operations = []
    async def dispatch(vacancy, cover, *, hh_client):
        store, bound_token, owner = hh_client._manual_apply_context
        assert bound_token == token
        assert queue.approval_valid(token, owner, store=store)
        async def operation():
            operations.append(hh_client._external_attempt.owner)
            saved = queue.get_candidate(token)
            assert saved["native_attempt"]["owner"] == operations[-1]
            assert repository.get(vacancy["url"])["manual_owner"] == owner
            return {"ok": False, "message": "Synthetic stop before external action"}
        return await run_native_attempt(hh_client, repository, vacancy["url"], operation)
    monkeypatch.setattr(agent.apply_orchestrator, "dispatch_apply", dispatch)
    asyncio.run(agent.do_manual_apply_token(token))
    assert len(operations) == 1


def test_concurrent_claims_consume_manual_grant_once(homes):
    from concurrent.futures import ThreadPoolExecutor
    card, repository, _ = abandoned_native(homes)
    retry = queue.resolve_orphaned_apply(card["token"], profile_name="qa", user_id=42, confirmed_not_sent=True)
    claim = queue.claim_candidate(retry["token"])
    def acquire(_):
        return queue.claim_native_attempt(retry["token"], claim["owner"], repository, URL, store=queue._store())
    with ThreadPoolExecutor(max_workers=2) as pool:
        owners = list(pool.map(acquire, range(2)))
    assert sum(bool(owner) for owner in owners) == 1
    assert len(repository.get(URL)["history"]) == 1


def test_queue_binding_write_failure_never_runs_operation_and_remains_recoverable(homes, monkeypatch):
    card = applying()
    repository = NativeApplyRepository(homes["qa"] / "hh_cookies.json", "hh")
    store = queue._store()
    def fail_save(data):
        raise OSError("Synthetic queue binding persistence failure")
    monkeypatch.setattr(store, "_save_unlocked", fail_save)
    client = SimpleNamespace(_manual_apply_context=(store, card["token"], card["owner"]))
    async def operation():
        pytest.fail("An unpersisted binding must not dispatch")
    result = asyncio.run(run_native_attempt(client, repository, URL, operation))
    assert result["reason"] == "native_ownership_unproven" and result["uncertain"]
    assert not queue._read_queue()["items"][card["token"]].get("native_attempt")
    dead_pid = queue._read_queue()["items"][card["token"]]["owner_pid"]
    repository.store.update(lambda data: data[repository.key(URL)].update(owner_pid=dead_pid, started_at=time.time() - 301))
    retry = queue.resolve_orphaned_apply(card["token"], profile_name="qa", user_id=42, confirmed_not_sent=True)
    assert retry["status"] == "pending"
    assert repository.get(URL)["manual_retry"]["token"] == retry["token"]
