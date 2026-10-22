"""Terminal seen outcomes cannot be replayed by saved cards or preflight races."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import agent
import apply_orchestrator
import config
import direction_runtime
import manual_apply_queue
import seen
from outcome import application_outcome
from state_store.private_journal import read_json_records
from tests.direction_view_fakes import two_directions
from tests.test_legacy_seen_compatibility import TERMINAL_ACTIONS, entry, select, write_seen
from tests.test_matcher_deferred_safety import search_harness  # noqa: F401
from tests.test_observability import configure_synthetic_search


def terminal_record(home, action, owner):
    key = f"direction:{owner}:7" if owner else "7"
    return write_seen(home, {
        key: entry(action, **({"direction": owner, "vacancy_id": "7"} if owner else {})),
        # A non-terminal verdict must not mask the raw terminal record.
        "direction:other:7": entry("skipped_low_score", direction="other", vacancy_id="7"),
    })


def vacancy():
    return {"id": "7", "source": "hh", "title": "Synthetic vacancy", "company": "Example",
            "url": "https://hh.ru/vacancy/7"}


@pytest.mark.parametrize("checkpoint", ["dispatch_apply", "_dispatch_apply"])
@pytest.mark.parametrize("owner", [None, "qa", "electric"])
@pytest.mark.parametrize("action", TERMINAL_ACTIONS)
def test_every_terminal_outcome_blocks_both_dispatch_checkpoints(search_harness, monkeypatch, action, owner, checkpoint):
    home, _, _ = search_harness
    two_directions(home)
    select(home, monkeypatch, "qa")
    before = terminal_record(home, action, owner)
    client = SimpleNamespace(apply_to_vacancy=AsyncMock(), get_resume_ids=AsyncMock())
    result = asyncio.run(getattr(apply_orchestrator, checkpoint)(vacancy(), "", hh_client=client))
    assert result["already_handled"] and result["handled_action"] == action
    assert result["already_applied"] == (action in seen.APPLIED_ACTIONS)
    assert application_outcome(result) == ("already_applied" if action in seen.APPLIED_ACTIONS else "blocked")
    client.apply_to_vacancy.assert_not_awaited()
    client.get_resume_ids.assert_not_awaited()
    assert (home / "seen.json").read_bytes() == before


@pytest.mark.parametrize("owner", [None, "qa", "electric"])
@pytest.mark.parametrize("action", TERMINAL_ACTIONS)
def test_saved_card_cannot_reopen_terminal_outcome(search_harness, monkeypatch, action, owner):
    home, _, _ = search_harness
    two_directions(home)
    select(home, monkeypatch, "qa")
    monkeypatch.setattr(manual_apply_queue, "_queue_path", lambda profile_name=None: home / "queue.json")
    card_vacancy = vacancy()
    direction_runtime.prepare(card_vacancy)
    card = manual_apply_queue.create_candidate(card_vacancy, {"score": 60}, details="Synthetic description")
    before = terminal_record(home, action, owner)
    client = SimpleNamespace(start=AsyncMock(), apply_to_vacancy=AsyncMock())
    factory = Mock(return_value=client)
    monkeypatch.setattr(agent, "HHClient", factory)

    result = asyncio.run(agent.do_manual_apply_token(card["token"]))

    assert result["already_handled"] and result["handled_action"] == action
    assert result["already_applied"] == (action in seen.APPLIED_ACTIONS)
    assert manual_apply_queue.get_candidate(card["token"])["status"] == (
        "already_applied" if action in seen.APPLIED_ACTIONS else "dismissed")
    factory.assert_not_called()
    client.apply_to_vacancy.assert_not_awaited()
    assert (home / "seen.json").read_bytes() == before


def racing_client(home, monkeypatch, action, owner):
    monkeypatch.setattr(config, "ACTIVE_DIRECTION", None, raising=False)
    monkeypatch.setattr(config, "HH_PRIMARY_RESUME_ID", "")
    monkeypatch.setattr(config, "HH_PRIMARY_RESUME_TITLE", "Synthetic resume")
    monkeypatch.setattr(config, "HH_APPLICATION_MODE", "auto")
    monkeypatch.setattr(apply_orchestrator, "create_hh_apply_trace", lambda *a, **kw: None)
    monkeypatch.setattr(agent, "generate_cover_letter", AsyncMock(return_value="Synthetic approved cover"))
    before = []
    async def catalog():
        await asyncio.sleep(0)
        before.append(terminal_record(home, action, owner))
        return [{"id": "synthetic", "title": "Synthetic resume"}]
    client = SimpleNamespace(_page=None, start=AsyncMock(), stop=AsyncMock(),
        is_logged_in=AsyncMock(return_value=True), get_negotiation_statuses=AsyncMock(return_value=[]),
        get_resume_ids=AsyncMock(side_effect=catalog), apply_to_vacancy=AsyncMock())
    monkeypatch.setattr(agent, "HHClient", lambda: client)
    return client, before


@pytest.mark.parametrize("owner", [None, "qa", "electric"])
@pytest.mark.parametrize("action", ["applied", "manual_ai_applied", "dismissed", "apply_uncertain", "apply_failed:captcha after apply"])
def test_manual_caller_preserves_terminal_record_found_after_await(search_harness, monkeypatch, action, owner):
    home, _, _ = search_harness
    monkeypatch.setattr(manual_apply_queue, "_queue_path", lambda profile_name=None: home / "queue.json")
    client, before = racing_client(home, monkeypatch, action, owner)
    card = manual_apply_queue.create_candidate(vacancy(), {"score": 60}, details="Synthetic description")
    notice = AsyncMock()
    monkeypatch.setattr(agent, "notify_needs_manual", notice)

    result = asyncio.run(agent.do_manual_apply_token(card["token"]))

    assert result["already_handled"] and result["handled_action"] == action
    assert result["already_applied"] == (action in seen.APPLIED_ACTIONS)
    assert manual_apply_queue.get_candidate(card["token"])["status"] == (
        "already_applied" if action in seen.APPLIED_ACTIONS else "dismissed")
    client.get_resume_ids.assert_awaited_once()
    client.apply_to_vacancy.assert_not_awaited()
    notice.assert_not_awaited()
    assert (home / "seen.json").read_bytes() == before[0]


@pytest.mark.parametrize("owner", [None, "qa", "electric"])
@pytest.mark.parametrize("action", ["applied", "manual_ai_applied", "dismissed", "apply_uncertain", "apply_failed:captcha after apply"])
def test_search_caller_preserves_terminal_record_found_after_await(search_harness, monkeypatch, action, owner):
    home, _, _ = search_harness
    real_dispatch = apply_orchestrator.dispatch_apply
    configure_synthetic_search(monkeypatch, home)
    monkeypatch.setattr(apply_orchestrator, "dispatch_apply", real_dispatch)
    monkeypatch.setattr(agent.search_pipeline, "collect_all", AsyncMock(return_value=[vacancy()]))
    monkeypatch.setattr(config, "ANALYTICS_EVENTS_FILE", str(home / "events.jsonl"))
    client, before = racing_client(home, monkeypatch, action, owner)

    result = asyncio.run(agent.do_search())

    assert result["applied"] == result["funnel"]["failed"] == 0
    assert result["skipped"] == 1
    client.get_resume_ids.assert_awaited_once()
    client.apply_to_vacancy.assert_not_awaited()
    agent.notify_needs_manual.assert_not_awaited()
    assert (home / "seen.json").read_bytes() == before[0]
    decisions = [row["decision"] for row in read_json_records(home / "events.jsonl") if row["event"] == "decision"]
    assert decisions == ["already_applied" if action in seen.APPLIED_ACTIONS else "not_processed"]


def test_mark_seen_cannot_downgrade_applied_evidence_but_preserves_uncertainty(search_harness):
    home, _, _ = search_harness
    before = write_seen(home, {"7": entry("applied")})
    seen.mark_seen("7", vacancy(), "apply_failed:stale result")
    seen.mark_seen("7", vacancy(), "already_applied")
    assert (home / "seen.json").read_bytes() == before
    seen.mark_seen("7", vacancy(), "apply_uncertain")
    assert json.loads((home / "seen.json").read_text())["7"]["action"] == "apply_uncertain"
