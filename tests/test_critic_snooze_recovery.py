"""Real-router regressions for legacy snooze isolation and manual recovery access."""
import asyncio
import time
from unittest.mock import AsyncMock

import pytest

import config
import manual_apply_queue as queue
import profile as profiles
from state_store.native_apply import NativeApplyRepository
from state_store.process_owner import current_process_owner
from tests.direction_view_fakes import two_directions
from tests.test_telegram_callbacks import _callback, _configured_bot


@pytest.fixture
def router(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOB_HUNTER_INSTANCE_KIND", "dev")
    monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))
    monkeypatch.setattr(config, "ACTIVE_DIRECTION", None, raising=False)
    monkeypatch.setattr(config, "HH_PRIMARY_RESUME_ID", "")
    monkeypatch.setattr(profiles, "_profiles_root_home", str(tmp_path))
    homes = {}
    for name in ("qa-dev", "other-dev"):
        home = tmp_path / "profiles" / name
        home.mkdir(parents=True)
        (home / "profile.env").write_text("HH_ENABLED=0\n")
        two_directions(home)
        homes[name] = home
    monkeypatch.setattr(queue, "_queue_path", lambda profile_name=None:
                        homes[profile_name or "qa-dev"] / "manual_apply_queue.json")
    principal = {"user_id": 1, "role": "tester", "profile": "qa-dev", "direction": "qa"}
    bot, calls = _configured_bot(monkeypatch, principal)
    monkeypatch.setattr(bot, "_profile_names", lambda: list(homes))
    monkeypatch.setattr(bot, "_selected_profile", lambda principal: principal["profile"])
    monkeypatch.setattr(bot, "_open_review_card", AsyncMock())
    monkeypatch.setattr(bot, "_start_manual_ai_apply", AsyncMock())
    return bot, calls, principal, homes


def candidate(*, direction="qa", profile="qa-dev", uncertain=False, orphan=True):
    vacancy = {"id": "123", "title": "Synthetic card", "url": "https://hh.ru/vacancy/123"}
    if direction:
        vacancy["_direction_selection"] = {"direction": direction}
    item = queue.create_candidate(vacancy, {}, profile_name=profile)
    if uncertain:
        queue._store(profile).update(lambda data: data["items"][item["token"]].update(
            status="uncertain", orphan_recovered=orphan, owner="abandoned-owner", owner_pid=0,
            external_started=True, applying_ts=time.time() - 600))
    return item


def press(bot, data, *, message_id=7, chat_type="private"):
    asyncio.run(bot._handle_callback_query(_callback(data, message_id=message_id, chat_type=chat_type)))


@pytest.mark.parametrize("direction,allowed", [("qa", True), ("electric", False), (None, False)])
def test_legacy_snooze_rechecks_direction_through_real_router(router, direction, allowed):
    bot, calls, _, homes = router
    item = candidate(direction=direction)
    path = homes["qa-dev"] / "manual_apply_queue.json"
    before = path.read_bytes()
    press(bot, f"manual_snooze:qa-dev:{item['token']}")
    current = queue._read_queue("qa-dev")["items"][item["token"]]
    if allowed:
        assert current["snoozed_until"] > time.time()
        assert "Отложил" in calls["answers"][-1][1]
    else:
        assert path.read_bytes() == before
        assert "snoozed_until" not in current
        assert calls["edits"] == []
        assert "уже не ждёт" in calls["answers"][-1][1]


def test_legacy_snooze_rejects_direction_reassigned_at_mutation_boundary(router, monkeypatch):
    bot, calls, _, homes = router
    item = candidate()
    token = item["token"]
    real_store = queue._store
    path = homes["qa-dev"] / "manual_apply_queue.json"
    checked = []

    class ReassignedStore:
        def __init__(self, store):
            self.store = store
            self.path = store.path

        def load(self):
            return self.store.load()

        def update(self, mutate):
            # Simulate a writer winning the queue lock after any caller-side
            # ownership read, before the snooze transaction reads its card.
            def reassign(data):
                current = data["items"][token]
                current["direction"] = "electric"
                current["vacancy"]["_direction_selection"]["direction"] = "electric"
            self.store.update(reassign)
            before = path.read_bytes()
            result = self.store.update(mutate)
            checked.append(path.read_bytes() == before)
            return result

    monkeypatch.setattr(queue, "_store", lambda profile_name=None: ReassignedStore(real_store(profile_name)))
    press(bot, f"manual_snooze:qa-dev:{token}")
    assert checked == [True]
    assert calls["edits"] == []
    assert "уже не ждёт" in calls["answers"][-1][1]


@pytest.mark.parametrize("role", ["admin", "user", "tester"])
@pytest.mark.parametrize("action", ["handled", "retry"])
@pytest.mark.parametrize("orphan", [False, True])
def test_manual_recovery_buttons_traverse_for_all_roles_and_remain_spent(router, role, action, orphan):
    bot, calls, principal, _ = router
    principal["role"] = role
    item = candidate(uncertain=True, orphan=orphan)
    token = item["token"]
    buttons = queue.build_orphaned_apply_markup(item, "qa-dev")["inline_keyboard"]
    data = next(button["callback_data"] for row in buttons for button in row
                if button.get("callback_data", "").endswith(":" + action))
    press(bot, data)
    if action == "retry":
        assert queue._read_queue("qa-dev")["items"][token]["status"] == "uncertain"
        assert len(queue._read_queue("qa-dev")["items"]) == 1
        assert "Подтверждаете" in calls["messages"][-1][1]
        data = calls["messages"][-1][2]["inline_keyboard"][0][0]["callback_data"]
        press(bot, data)
    saved = queue._read_queue("qa-dev")["items"]
    assert saved[token]["status"] == "handled"
    assert saved[token]["resolved_by"] == principal["user_id"]
    if action == "retry":
        retry = saved[saved[token]["retry_token"]]
        assert retry["status"] == "pending" and retry["retry_of"] == token
        bot._open_review_card.assert_awaited_once_with(42, "qa-dev", retry["token"])
    else:
        assert len(saved) == 1
    before = queue._queue_path("qa-dev").read_bytes()
    press(bot, data)  # same callback is durably spent
    press(bot, data, message_id=8)  # copied stale keyboard reaches the state guard
    assert queue._queue_path("qa-dev").read_bytes() == before
    assert "уже разобрана" in calls["answers"][-1][1]
    bot._start_manual_ai_apply.assert_not_awaited()
    if action == "retry":
        # Reconciliation grants a new decision; dispatch requires its own tap.
        press(bot, f"manual_apply:qa-dev:{retry['token']}", message_id=9)
        bot._start_manual_ai_apply.assert_awaited_once_with(
            42, principal, profile_name="qa-dev", token=retry["token"])


@pytest.mark.parametrize("role,guard", [
    (role, guard) for role in ("admin", "user", "tester")
    for guard in ("group", "native_completed", "native_live", "live_queue")
] + [(role, "foreign_profile") for role in ("user", "tester")])
def test_recovery_access_keeps_private_profile_and_attempt_guards(router, role, guard):
    bot, _, principal, _ = router
    principal["role"] = role
    profile = "other-dev" if guard == "foreign_profile" else "qa-dev"
    item = candidate(profile=profile, uncertain=True)
    token = item["token"]
    if guard.startswith("native_"):
        repository = NativeApplyRepository(queue._queue_path(profile).with_name("hh_cookies.json"), "hh")
        owner = repository.claim(item["vacancy"]["url"], "", "", manual_token=token,
                                 manual_owner="abandoned-owner")
        def native_state(state):
            current = state[repository.key(item["vacancy"]["url"])]
            current.update(status="completed" if guard == "native_completed" else "uncertain",
                           started_at=time.time() - 600)
            if guard == "native_completed":
                current["owner_pid"] = 0
        repository.store.update(native_state)
        queue._store(profile).update(lambda data: data["items"][token].update(native_attempt={
            "path": str(repository.path), "url": item["vacancy"]["url"], "owner": owner}))
    if guard == "live_queue":
        queue._store(profile).update(lambda data: data["items"][token].update(**current_process_owner()))
    before = queue._queue_path(profile).read_bytes()
    press(bot, f"mar:{profile}:{token}:confirm", chat_type="group" if guard == "group" else "private")
    assert queue._queue_path(profile).read_bytes() == before
    bot._open_review_card.assert_not_awaited()
    bot._start_manual_ai_apply.assert_not_awaited()


@pytest.mark.parametrize("direction", ["electric", None])
@pytest.mark.parametrize("action", ["handled", "retry", "confirm"])
def test_tester_recovery_cannot_resolve_foreign_or_unknown_direction(router, direction, action):
    bot, _, _, _ = router
    item = candidate(direction=direction, uncertain=True)
    before = queue._queue_path("qa-dev").read_bytes()
    press(bot, f"mar:qa-dev:{item['token']}:{action}")
    assert queue._queue_path("qa-dev").read_bytes() == before
    bot._open_review_card.assert_not_awaited()
    bot._start_manual_ai_apply.assert_not_awaited()


@pytest.mark.parametrize("action", ["handled", "confirm"])
@pytest.mark.parametrize("reassigned_status", ["uncertain", "applying"])
def test_tester_recovery_rechecks_reassigned_card_before_any_locked_write(
        router, monkeypatch, action, reassigned_status):
    bot, _, _, _ = router
    item = candidate(uncertain=True)
    token = item["token"]
    store = queue._store("qa-dev")
    repository = NativeApplyRepository(store.path.with_name("hh_cookies.json"), "hh")
    url = item["vacancy"]["url"]
    owner = repository.claim(url, "", "", manual_token=token, manual_owner="abandoned-owner")
    repository.store.update(lambda data: data[repository.key(url)].update(
        status="uncertain", owner_pid=0, started_at=time.time() - 600))
    store.update(lambda data: data["items"][token].update(native_attempt={
        "path": str(repository.path), "url": url, "owner": owner}))
    before = []
    answer = bot._answer_callback_query

    async def reassign_after_router_read(*args, **kwargs):
        # The router has accepted an own uncertain card and now awaits its
        # acknowledgement, before read_thread enters the recovery transaction.
        def reassign(data):
            current = data["items"][token]
            current["direction"] = "electric"
            current["vacancy"]["_direction_selection"]["direction"] = "electric"
            current["status"] = reassigned_status
        store.update(reassign)
        before.append((store.path.read_bytes(), repository.path.read_bytes()))
        await answer(*args, **kwargs)

    monkeypatch.setattr(bot, "_answer_callback_query", reassign_after_router_read)
    press(bot, f"mar:qa-dev:{token}:{action}")
    assert len(before) == 1
    assert (store.path.read_bytes(), repository.path.read_bytes()) == before[0]
    assert len(store.load()["items"]) == 1
    assert not repository.get(url).get("manual_retry")
    bot._open_review_card.assert_not_awaited()
    bot._start_manual_ai_apply.assert_not_awaited()
