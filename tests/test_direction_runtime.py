"""Variant binding at the matcher/queue/application boundary, entirely offline."""
import asyncio
import copy
import hashlib
import json

import pytest

import analytics
import apply_orchestrator
import config
import direction_runtime
import directions
import facts
import hh_client
import manual_apply_queue
import matcher
import prompt_blocks
import resume_versions


@pytest.fixture
def active_direction(tmp_path, monkeypatch):
    value = {
        "name": "electrical", "title": "Электрика", "filter_policy": "generic",
        "search_queries": ["Электрик"], "relevant_keywords": ["электрик"], "exclude_keywords": [],
        "selection_strategy": "ab",
        "variants": [{"hh_resume_hash": f"hash-{i}", "hh_resume_numeric_id": 100 + i,
                      "title": f"Вариант {i}", "status": "active", "tags": [],
                      "fetched_at": "2026-10-17T10:00:00+00:00"} for i in range(3)],
    }
    texts = {f"hash-{i}": f"# Variant {i}\nПроверяю электрические щиты.\n" for i in range(3)}
    direction = directions.save_direction(tmp_path, value, texts=texts)
    directions.set_active_direction(tmp_path, direction["name"])
    monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))
    monkeypatch.setattr(config, "ACTIVE_DIRECTION", direction, raising=False)
    monkeypatch.setattr(config, "RESUME_FILE", direction["variants"][0]["text_file"])
    return direction, texts


def test_matcher_and_cover_use_one_bound_variant(active_direction):
    direction, texts = active_direction
    vacancy = {"id": "700", "source": "hh"}
    first = matcher._resume_for_vacancy(vacancy)
    selected = copy.deepcopy(vacancy["_direction_selection"])
    resume_versions.record_input(vacancy, first, "matcher")
    assert first == texts[selected["hh_resume_hash"]].strip()
    # Ordering changes cannot silently select another variant between stages.
    # Computing another possible choice must not mutate the vacancy's binding.
    reordered = copy.deepcopy(direction)
    reordered["variants"].reverse()
    directions.select_variant(reordered, vacancy["id"])
    second = matcher._resume_for_vacancy(vacancy)
    resume_versions.record_input(vacancy, second, "cover_letter")
    assert first == second
    assert vacancy["_direction_selection"] == selected
    payload = resume_versions.payload(vacancy)
    assert payload["matcher_resume_sha256"] == payload["cover_letter_resume_sha256"]
    assert payload["variant_text_sha256"] == hashlib.sha256(texts[selected["hh_resume_hash"]].encode()).hexdigest()


def test_bound_variant_survives_manual_queue(active_direction, tmp_path, monkeypatch):
    monkeypatch.setattr(manual_apply_queue, "_queue_path", lambda profile_name=None: tmp_path / "queue.json")
    vacancy = {"id": "710", "source": "hh"}
    direction_runtime.prepare(vacancy)
    item = manual_apply_queue.create_candidate(vacancy, {}, profile_name="client")
    restored = manual_apply_queue.get_candidate(item["token"])["vacancy"]
    assert restored["_direction_selection"] == vacancy["_direction_selection"]
    assert matcher._resume_for_vacancy(restored) == matcher._resume_for_vacancy(vacancy)


@pytest.mark.parametrize("mutation", ["pause", "remove", "sha", "profile", "direction", "vacancy"])
def test_stale_bound_variant_requires_new_evaluation(active_direction, monkeypatch, mutation):
    direction, _ = active_direction
    vacancy = {"id": "720", "source": "hh"}
    bound = direction_runtime.prepare(vacancy)
    selected = next(v for v in direction["variants"] if v["hh_resume_hash"] == bound["hh_resume_hash"])
    if mutation == "pause":
        selected["status"] = "paused"
    elif mutation == "remove":
        direction["variants"].remove(selected)
    elif mutation == "sha":
        selected["text_sha256"] = "0" * 64
    elif mutation == "profile":
        monkeypatch.setattr(config, "JOB_HUNTER_HOME", "/another-profile")
    elif mutation == "direction":
        direction["name"] = "other"
    else:
        vacancy["id"] = "721"
    with pytest.raises(ValueError):
        matcher._resume_for_vacancy(vacancy)


def test_removed_active_direction_cannot_revert_queue_to_legacy(active_direction, monkeypatch):
    vacancy = {"id": "730"}
    direction_runtime.prepare(vacancy)
    monkeypatch.setattr(config, "ACTIVE_DIRECTION", None)
    with pytest.raises(ValueError):
        matcher._resume_for_vacancy(vacancy)


def test_disk_pause_invalidates_cached_worker_before_dispatch(active_direction, tmp_path, monkeypatch):
    direction, _ = active_direction
    vacancy = {"id": "735", "source": "hh"}
    chosen = direction_runtime.prepare(vacancy)
    fresh = copy.deepcopy(direction)
    next(v for v in fresh["variants"] if v["hh_resume_hash"] == chosen["hh_resume_hash"])["status"] = "paused"
    directions.save_direction(tmp_path, fresh)
    assert config.ACTIVE_DIRECTION == direction  # Worker still has old config.
    monkeypatch.setattr(apply_orchestrator.company_blacklist, "is_blocked", lambda _: False)
    async def forbidden(*args, **kwargs):
        pytest.fail("persisted pause must stop cached worker")
    monkeypatch.setattr(apply_orchestrator, "_dispatch_apply", forbidden)
    result = asyncio.run(apply_orchestrator.dispatch_apply(vacancy, "cover"))
    assert result["reason"] == "direction_variant_unavailable"


def test_legacy_manual_queue_is_rejected_before_browser_or_claim(active_direction, monkeypatch):
    import agent
    monkeypatch.setattr(config, "HH_APPLICATION_MODE", "auto")
    monkeypatch.setattr(agent.company_blacklist, "is_blocked", lambda _: False)
    monkeypatch.setattr(agent.manual_apply_queue, "get_candidate", lambda _: {
        "status": "pending", "vacancy": {"id": "737", "source": "hh"},
        "evaluation": {"score": 90}, "allow_ai_apply": True,
    })
    recorded = []
    monkeypatch.setattr(agent.manual_apply_queue, "mark_candidate", lambda *args: recorded.append(args))
    monkeypatch.setattr(agent, "HHClient", lambda: pytest.fail("stale card must not open a browser"))
    monkeypatch.setattr(agent.manual_apply_queue, "claim_candidate", lambda *a, **k: pytest.fail("stale card must not claim"))
    result = asyncio.run(agent.do_manual_apply_token("legacy-card"))
    assert result["reason"] == "direction_context_changed"
    assert recorded[0][1] == "stale"


@pytest.mark.parametrize("alias_home", [False, True])
def test_chat_state_uses_captured_profile_home_after_direction_switch(active_direction, tmp_path, monkeypatch, alias_home):
    from runtime_context import RuntimePaths
    from hh_chat_responder import _state_repository
    home = tmp_path
    if alias_home:
        home = tmp_path / "profile-link"
        home.symlink_to(tmp_path, target_is_directory=True)
    paths = RuntimePaths(str(home), str(home / "state"), config.RESUME_FILE)
    monkeypatch.setattr(config, "ACTIVE_DIRECTION", None)
    monkeypatch.setattr(config, "JOB_HUNTER_HOME", "/another-profile")
    assert _state_repository(paths).path.parent.resolve() == tmp_path


def test_dispatch_records_request_and_outcome_with_bound_identity(active_direction, tmp_path, monkeypatch):
    _, texts = active_direction
    events = tmp_path / "events.jsonl"
    monkeypatch.setattr(config, "ANALYTICS_ENABLED", True)
    monkeypatch.setattr(config, "ANALYTICS_EVENTS_FILE", str(events))
    monkeypatch.setattr(config, "HH_APPLY_TRACE_ENABLED", False)
    monkeypatch.setattr(apply_orchestrator.company_blacklist, "is_blocked", lambda _: False)
    vacancy = {"id": "740", "source": "hh", "_hh_work_conditions": {"formats": ["remote"], "fly_in_fly_out": False}}
    chosen = direction_runtime.prepare(vacancy)
    observed = []

    async def dispatch(vacancy, cover, *args, **kwargs):
        observed.append(kwargs)
        assert hh_client._load_resume_text() == texts[chosen["hh_resume_hash"]].strip()
        return {"ok": False, "message": "synthetic no-submit"}

    monkeypatch.setattr(apply_orchestrator, "_dispatch_apply", dispatch)
    result = asyncio.run(apply_orchestrator.dispatch_apply(
        vacancy, "Тестовое письмо", preferred_resume_id="legacy-configured-id"))
    assert result["ok"] is False
    assert observed[0]["preferred_resume_id"] == chosen["hh_resume_hash"]
    assert direction_runtime.application_resume() is None
    records = [json.loads(line) for line in events.read_text().splitlines()]
    assert [row["event"] for row in records] == ["application_attempt", "application_result"]
    for row in records:
        assert row["direction"] == "electrical"
        assert row["hh_resume_hash"] == chosen["hh_resume_hash"]
        assert row["hh_resume_numeric_id"] == chosen["hh_resume_numeric_id"]
        assert row["variant_text_sha256"] == chosen["text_sha256"]
        assert row["selection_strategy"] == "ab"
        assert row["selection_reason"] == "ab:sha256_modulo"
        assert row["direction_variant_scope"] == "requested_local_variant"
    assert "Проверяю электрические щиты" not in events.read_text()


def test_unavailable_variant_never_reaches_dispatch(active_direction, monkeypatch):
    direction, _ = active_direction
    for variant in direction["variants"]:
        variant["status"] = "paused"
    monkeypatch.setattr(apply_orchestrator.company_blacklist, "is_blocked", lambda _: False)

    async def forbidden(*args, **kwargs):
        pytest.fail("must not send without a verified variant")

    monkeypatch.setattr(apply_orchestrator, "_dispatch_apply", forbidden)
    result = asyncio.run(apply_orchestrator.dispatch_apply({"id": "750"}, "cover"))
    assert result["reason"] == "direction_variant_unavailable"


def test_direction_uses_profile_fact_home(active_direction, tmp_path):
    import candidate_interview
    (tmp_path / "facts.json").write_text('{"confirmed":{"profession":"электрик"}}')
    assert facts.facts_file_path() == str(tmp_path / "facts.json")
    assert facts.load_facts()["confirmed"]["profession"] == "электрик"
    assert prompt_blocks._knowledge_dir() == str(tmp_path / "knowledge")
    assert candidate_interview.data_path() == str(tmp_path / "candidate_interview.json")


def test_direction_fallback_does_not_inherit_another_profession(active_direction, tmp_path):
    direction, _ = active_direction
    (tmp_path / "cover_letter_fallback.md").write_text("QA template from the profile")
    assert "QA template" not in matcher._fallback_cover_letter({"id": "755"})
    direction["cover_letter_fallback"] = "Здравствуйте! Направляю резюме электрика."
    assert matcher._fallback_cover_letter({"id": "755"}) == direction["cover_letter_fallback"]


def test_without_direction_keeps_legacy_payload_and_loader(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "ACTIVE_DIRECTION", None, raising=False)
    monkeypatch.setattr(matcher, "_load_resume", lambda: "legacy text")
    vacancy = {"id": "760"}
    assert matcher._resume_for_vacancy(vacancy) == "legacy text"
    assert "_direction_selection" not in vacancy
    assert "direction" not in resume_versions.payload(vacancy)


def test_legacy_resume_writers_cannot_overwrite_direction_text(active_direction, monkeypatch, capsys):
    import agent
    from commands import resume
    from pathlib import Path
    direction, _ = active_direction
    before = {v["text_file"]: Path(v["text_file"]).read_bytes() for v in direction["variants"]}
    monkeypatch.setattr(agent, "HHClient", lambda: pytest.fail("direction grab must not open HH"))
    monkeypatch.setattr(resume, "HHClient", lambda: pytest.fail("direction refresh must not open HH"))
    assert asyncio.run(resume.do_hh_resume_refresh()) is False
    asyncio.run(agent.do_grab_resume())
    asyncio.run(agent._save_resume_from_client(object()))
    assert "/direction_refresh" in capsys.readouterr().out
    assert all(Path(path).read_bytes() == raw for path, raw in before.items())
    assert directions.select_variant(direction, "700")["hh_resume_hash"]


def test_offline_direction_profile_matcher_queue_dispatch_smoke(active_direction, tmp_path, monkeypatch):
    """Cross real local boundaries; replace only LLM and HH service endpoints."""
    import profile
    import search_pipeline
    from pathlib import Path
    from tests.test_cover_grounding import approved, client_for

    direction, texts = active_direction
    monkeypatch.setenv("KB_RETRIEVAL_ENABLED", "0")
    monkeypatch.setattr(config, "HH_ENABLED", True)
    monkeypatch.setattr(config, "SEARCH_PAGES", 1)
    monkeypatch.setattr(config, "SEARCH_PROFILES", [{"area": 113, "schedule": "remote"}])
    profile._patch_config(profile.load_default_profile())
    events = tmp_path / "smoke-events.jsonl"
    monkeypatch.setattr(config, "ANALYTICS_ENABLED", True)
    monkeypatch.setattr(config, "ANALYTICS_EVENTS_FILE", str(events))
    monkeypatch.setattr(config, "HH_APPLY_TRACE_ENABLED", False)
    (tmp_path / "facts.json").write_text('{"confirmed": {"profession": "электрик"}}')
    vacancy_id = next(str(i) for i in range(100) if directions.select_variant(direction, str(i))["hh_resume_hash"] != "hash-0")
    vacancy = {"id": vacancy_id, "source": "hh", "title": "Электрик", "company": "Synthetic employer",
               "url": f"https://hh.ru/vacancy/{vacancy_id}",
               "_hh_work_conditions": {"formats": ["remote"], "fly_in_fly_out": False}}
    searches = []
    class CollectionClient:
        async def is_logged_in(self):
            return True
        async def search_vacancies(self, query, **kwargs):
            searches.append((query, kwargs))
            return [vacancy.copy()]
        def consume_antibot_signal(self):
            return None
    collected = asyncio.run(search_pipeline.collect_hh_vacancies(CollectionClient()))
    assert searches == [("Электрик", {"page": 0, "area": 113, "schedule": "remote"})]
    vacancy, = collected
    claim = "Проверяю электрические щиты."
    scoring = client_for(json.dumps({"score": 95, "should_apply": True, "reason": "Подходит", "red_flags": []}))
    monkeypatch.setattr(matcher, "_get_client", lambda: scoring)
    evaluation = asyncio.run(matcher.evaluate_vacancy(vacancy, "Проверка щитов"))
    cover_client = client_for(claim + " Обсудим детали?", json.dumps(approved(claim)))
    monkeypatch.setattr(matcher, "_get_client", lambda: cover_client)
    cover = asyncio.run(matcher.generate_cover_letter(vacancy, "Проверка щитов"))
    assert evaluation["should_apply"] and cover == claim + " Обсудим детали?"
    selected = vacancy["_direction_selection"]
    text = texts[selected["hh_resume_hash"]].strip()
    assert text in scoring.calls[0]["messages"][0]["content"]
    assert text in cover_client.calls[0]["messages"][0]["content"]
    assert "# Variant 0" not in scoring.calls[0]["messages"][0]["content"]

    queued = manual_apply_queue.create_candidate(vacancy, evaluation, profile_name="default")
    restored = manual_apply_queue.get_candidate(queued["token"])["vacancy"]
    direction_runtime.validate_queued(restored)
    observed = []
    class Client:
        async def apply_to_vacancy(self, url, letter, **kwargs):
            observed.append((url, letter, kwargs))
            assert hh_client._load_resume_text() == text
            assert kwargs["preferred_resume_id"] == selected["hh_resume_hash"]
            return {"ok": False, "message": "Synthetic pre-submit endpoint; no external action"}
    result = asyncio.run(apply_orchestrator.dispatch_apply(restored, cover, hh_client=Client()))
    assert result["ok"] is False and len(observed) == 1
    records = [json.loads(line) for line in events.read_text().splitlines()]
    attempts = [r for r in records if r["event"] in {"application_attempt", "application_result"}]
    assert len(attempts) == 2
    assert all(r["hh_resume_hash"] == selected["hh_resume_hash"] for r in attempts)
    assert attempts[0]["matcher_resume_sha256"] == attempts[0]["cover_letter_resume_sha256"]
    assert Path(config.MANUAL_APPLY_QUEUE_FILE).parent == tmp_path
