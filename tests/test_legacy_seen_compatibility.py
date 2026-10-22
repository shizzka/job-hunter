"""Synthetic pre-F15 file shapes must not consume quota or repeat applications."""
import asyncio
import json
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import agent
import apply_orchestrator
import config
import directions
import evaluation_cache
import search_pipeline
import seen
import seen_reset
from tests.direction_view_fakes import two_directions
from tests.test_matcher_deferred_safety import search_harness  # noqa: F401


TERMINAL_ACTIONS = [
    "applied", "already_applied", "dismissed", "мимо", "skipped_feedback_bad",
    "manual_hh", "manual_hh_confirm", "manual_yellow_hh", "manual_habr", "manual_geekjob",
    "manual_ai_applied", "manual_future_variant", "manual_hh_guard_stop", "apply_uncertain",
    "apply_failed", "apply_failed:captcha after apply", "apply_failed_exception:TimeoutError",
    "apply_failed_future_variant", "apply_unconfirmed_no_manual",
]


def write_seen(home, entries):
    path = home / "seen.json"
    path.write_text(json.dumps(entries, ensure_ascii=False), encoding="utf-8")
    return path.read_bytes()


def select(home, monkeypatch, name):
    directions.set_active_direction(home, name)
    monkeypatch.setattr(config, "ACTIVE_DIRECTION", directions.active_direction(home), raising=False)


def entry(action, **extra):
    return {"title": "Synthetic vacancy", "company": "Example", "action": action,
            "date": "2026-10-20T12:00:00", **extra}


@pytest.mark.parametrize("direction", ["qa", "electric"])
def test_legacy_only_search_performs_zero_filtering_scoring_or_delivery(search_harness, monkeypatch, direction):
    home, collected, _ = search_harness
    two_directions(home)
    select(home, monkeypatch, direction)
    actions = ["skipped_keyword_filter", "skipped_low_score", "skipped_red_flags", "archived", *TERMINAL_ACTIONS]
    entries = {str(i): entry(action) for i, action in enumerate(actions, 1)}
    before = write_seen(home, entries)
    collected[0] = [{"id": key, "source": "hh", "title": f"Synthetic {key}"} for key in entries]
    evaluate = AsyncMock()
    dispatch = AsyncMock()
    monkeypatch.setattr(agent, "evaluate_vacancy", evaluate)
    monkeypatch.setattr(apply_orchestrator, "dispatch_apply", dispatch)
    original_filter = search_pipeline.keyword_filter
    passed_to_filter = []
    def keyword_filter(vacancies, *args):
        passed_to_filter.extend(vacancies)
        return original_filter(vacancies, *args)
    monkeypatch.setattr(search_pipeline, "keyword_filter", keyword_filter)

    result = asyncio.run(agent.do_search(dry_run=True))

    assert result["found"] == 0
    assert passed_to_filter == []
    evaluate.assert_not_awaited()
    dispatch.assert_not_awaited()
    agent.apply_orchestrator.fetch_vacancy_details.assert_not_awaited()
    assert (home / "seen.json").read_bytes() == before


@pytest.mark.parametrize("action", TERMINAL_ACTIONS)
@pytest.mark.parametrize("owner", [None, "qa", "electric", "removed-direction"])
def test_terminal_outcomes_stay_hidden_in_both_directions(search_harness, monkeypatch, action, owner):
    home, _, _ = search_harness
    two_directions(home)
    key = f"direction:{owner}:7" if owner else "7"
    before = write_seen(home, {key: entry(action, **({"direction": owner, "vacancy_id": "7"} if owner else {}))})
    for name in ("qa", "electric"):
        select(home, monkeypatch, name)
        assert seen.is_seen("7")
    assert (home / "seen.json").read_bytes() == before


@pytest.mark.parametrize("action", ["applied", "already_applied", "manual_ai_applied"])
@pytest.mark.parametrize("owner", [None, "electric", "removed-direction"])
def test_dispatch_cannot_repeat_application_even_if_later_direction_has_skip(search_harness, monkeypatch, action, owner):
    home, _, _ = search_harness
    two_directions(home)
    select(home, monkeypatch, "qa")
    key = f"direction:{owner}:7" if owner else "7"
    before = write_seen(home, {
        key: entry(action, **({"direction": owner, "vacancy_id": "7"} if owner else {})),
        "direction:qa:7": entry("skipped_low_score", direction="qa", vacancy_id="7"),
    })
    client = SimpleNamespace(apply_to_vacancy=AsyncMock(), get_resume_ids=AsyncMock())
    for identity in ("7", "hh:7"):
        result = asyncio.run(apply_orchestrator.dispatch_apply(
            {"id": identity, "source": "hh", "url": "https://hh.ru/vacancy/7"}, "", hh_client=client))
        assert result["reason"] == "already_applied" and result["already_applied"] and result["ok"]
    client.apply_to_vacancy.assert_not_awaited()
    client.get_resume_ids.assert_not_awaited()
    assert (home / "seen.json").read_bytes() == before


def test_dispatch_rechecks_application_after_awaited_resume_catalog(search_harness, monkeypatch):
    home, _, _ = search_harness
    monkeypatch.setattr(config, "ACTIVE_DIRECTION", None, raising=False)
    monkeypatch.setattr(config, "HH_PRIMARY_RESUME_ID", "")
    monkeypatch.setattr(apply_orchestrator, "create_hh_apply_trace", lambda *a, **kw: None)
    async def catalog():
        write_seen(home, {"7": entry("applied")})
        return [{"id": "synthetic", "title": "Synthetic resume"}]
    client = SimpleNamespace(apply_to_vacancy=AsyncMock(), get_resume_ids=catalog)
    result = asyncio.run(apply_orchestrator.dispatch_apply(
        {"id": "7", "source": "hh", "url": "https://hh.ru/vacancy/7"}, "", hh_client=client,
        preferred_resume_title="Synthetic resume"))
    assert result["reason"] == "already_applied"
    client.apply_to_vacancy.assert_not_awaited()


def test_legacy_cache_fallback_preserves_ttl_direction_and_new_key_precedence(search_harness, monkeypatch):
    home, _, _ = search_harness
    two_directions(home)
    select(home, monkeypatch, "qa")
    vacancy = {"id": "7", "source": "hh"}
    path = home / evaluation_cache.CACHE_FILE
    path.write_text(json.dumps({"hh:7:qa": {"at": time.time(), "evaluation": {"score": 72}}}))
    before = path.read_bytes()
    assert evaluation_cache.lookup(vacancy) == {"score": 72, "evaluation_cached": True}
    assert path.read_bytes() == before
    select(home, monkeypatch, "electric")
    assert evaluation_cache.lookup(vacancy) is None
    select(home, monkeypatch, "qa")
    evaluation_cache.store(vacancy, {"score": 31})
    assert evaluation_cache.lookup(vacancy)["score"] == 31
    path.write_bytes(before)
    monkeypatch.setattr(evaluation_cache.time, "time", lambda: 10**12)
    assert evaluation_cache.lookup(vacancy) is None


def test_reset_cannot_remove_dismissed_feedback(tmp_path):
    (tmp_path / "seen_vacancies.json").write_text(json.dumps({
        "7": entry("skipped_feedback_bad"), "8": entry("skipped_low_score"),
    }))
    assert seen_reset.plan(str(tmp_path), "all", direction_scope="*") == ["8"]
