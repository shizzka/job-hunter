"""B41: real grounding/fallback metadata must stop offline dispatch."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

import agent
import config
import matcher
import manual_apply_queue
from tests.test_agent_manual_apply import _stub_setup
from tests.test_cover_grounding import approved, client_for, isolated_candidate, SENTENCES, unsupported_first
from tests.test_observability import configure_synthetic_search


@pytest.fixture
def direction(monkeypatch):
    monkeypatch.setattr(config, "ACTIVE_DIRECTION", {"name": "qa", "cover_letter_fallback": ""}, raising=False)
    # Test the cover contract without a persisted resume/direction catalog.
    monkeypatch.setattr(agent.direction_runtime, "prepare", lambda *a, **k: None)
    monkeypatch.setattr(agent.direction_runtime, "validate_queued", lambda *a: None)
    monkeypatch.setattr(agent.apply_orchestrator, "fetch_vacancy_details", AsyncMock(return_value="Описание"))


@pytest.mark.parametrize("saved", ["", "Сохранённое владельцем письмо для QA."])
@pytest.mark.parametrize("limit", [1000, 45])
def test_manual_grounding_failure_obeys_real_fallback_provenance(
        tmp_path, monkeypatch, isolated_candidate, direction, saved, limit):
    token, cards, dispatched = _stub_setup(tmp_path, monkeypatch, allow_stub=False)
    config.ACTIVE_DIRECTION["cover_letter_fallback"] = saved
    monkeypatch.setattr(agent.apply_orchestrator, "get_cover_letter_limit", lambda source: limit)
    client = client_for(" ".join(SENTENCES), json.dumps(approved("Absent quote")),
                        json.dumps(approved("Absent quote")),
                        " ".join(SENTENCES), json.dumps(approved("Absent quote")))
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    monkeypatch.setattr(agent, "generate_cover_letter", matcher.generate_cover_letter)
    result = asyncio.run(agent.do_manual_apply_token(token))
    if saved:
        assert dispatched and not cards
        assert dispatched[0][1] == matcher.salary_policy.finish_cover(dispatched[0][0], saved)[:limit]
    else:
        assert result["stub_review"] and not dispatched
        assert manual_apply_queue.get_candidate(token)["status"] == "pending"
        assert "Сохранённого письма для этого направления нет" in cards[0][0]
        assert [b["text"] for b in cards[0][1]["inline_keyboard"][0]] == [
            "🔄 Перегенерировать", "📨 Отправить шаблон"]


def test_manual_grounded_letter_reaches_dispatch_unchanged(
        tmp_path, monkeypatch, isolated_candidate, direction):
    token, cards, dispatched = _stub_setup(tmp_path, monkeypatch, allow_stub=False)
    client = client_for(" ".join(SENTENCES), json.dumps(approved()))
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    monkeypatch.setattr(agent, "generate_cover_letter", matcher.generate_cover_letter)
    asyncio.run(agent.do_manual_apply_token(token))
    assert dispatched and not cards
    assert dispatched[0][1] == matcher.salary_policy.finish_cover(dispatched[0][0], " ".join(SENTENCES))


def test_nonliteral_quote_regenerates_once_and_verifies_the_new_letter(isolated_candidate, monkeypatch):
    draft = " ".join(SENTENCES)
    client = client_for(draft, json.dumps(approved("Absent quote")), json.dumps(approved("Absent quote")),
                        draft, json.dumps(approved()))
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    vacancy = {"title": "QA", "salary": "100000"}
    cover = asyncio.run(matcher.generate_cover_letter(vacancy))
    assert cover == draft and not matcher.cover_letter_requires_review(vacancy, cover)
    assert [c["llm_role"] for c in client.calls] == [
        "cover_letter", "cover_grounding", "cover_grounding", "cover_letter_rewrite", "cover_grounding"]


def test_nonliteral_retry_is_available_after_a_semantic_rewrite(isolated_candidate, monkeypatch):
    draft = " ".join(SENTENCES)
    client = client_for(draft, json.dumps(unsupported_first()), draft, json.dumps(approved("Absent quote")),
                        draft, json.dumps(approved()))
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    vacancy = {"title": "QA", "salary": "100000"}
    assert asyncio.run(matcher.generate_cover_letter(vacancy)) == draft
    assert len(client.calls) == 6
    assert not matcher.cover_letter_requires_review(vacancy, draft)


def test_manual_unknown_letter_requires_review(tmp_path, monkeypatch):
    token, cards, dispatched = _stub_setup(tmp_path, monkeypatch, allow_stub=False)
    monkeypatch.setattr(agent, "generate_cover_letter", AsyncMock(return_value="Текст без проверки."))
    result = asyncio.run(agent.do_manual_apply_token(token))
    assert result["stub_review"] and cards and not dispatched


@pytest.mark.parametrize("saved", ["", "Сохранённое владельцем письмо."])
def test_auto_fallback_sends_card_instead_of_unapproved_apply(tmp_path, monkeypatch, saved):
    configure_synthetic_search(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "ACTIVE_DIRECTION", None, raising=False)
    monkeypatch.setattr(config, "COVER_ASK_SALARY_RANGE", False)
    if saved:
        (tmp_path / "cover_letter_fallback.md").write_text(saved)
    monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))
    vacancy = {"id": "42", "source": "hh", "title": "QA", "company": "Пример"}
    monkeypatch.setattr(agent.search_pipeline, "collect_all", AsyncMock(return_value=[vacancy]))
    monkeypatch.setattr(agent.search_pipeline.filters, "check_vacancy", lambda v: None)
    monkeypatch.setattr(agent, "evaluate_vacancy", AsyncMock(return_value={
        "score": 95, "reason": "Подходит", "should_apply": True, "red_flags": []}))
    async def generate(v, details):
        return matcher._remember_cover_letter_meta(matcher._fallback_cover_letter(v), cover_style="",
                                                  fallback=True, grounding_status="nonliteral_quote")
    monkeypatch.setattr(agent, "generate_cover_letter", generate)
    dispatch = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(agent.apply_orchestrator, "dispatch_apply", dispatch)
    result = asyncio.run(agent.do_search())
    if saved:
        dispatch.assert_awaited_once()
        assert result["applied"] == 1
    else:
        dispatch.assert_not_awaited()
        assert result["applied"] == 0
        assert "Автоотклик не отправлен" in agent.notify_needs_manual.await_args.kwargs["note"]
        assert agent.notify_needs_manual.await_args.kwargs["reply_markup"]
        # The offered card remains actionable for its owner; there was no submit.
        reset = agent.apply_orchestrator.card_apply.set(True)
        try:
            assert agent.apply_orchestrator.handled_application_result(vacancy) is None
        finally:
            agent.apply_orchestrator.card_apply.reset(reset)


def test_unknown_letter_and_other_direction_fallback_require_choice(monkeypatch):
    monkeypatch.setattr(config, "ACTIVE_DIRECTION", {"name": "qa", "cover_letter_fallback": "QA письмо"}, raising=False)
    assert matcher.cover_letter_requires_review({}, "Произвольный текст")
    cover = matcher._fallback_cover_letter({})
    assert not matcher.cover_letter_requires_review({}, matcher.salary_policy.finish_cover({}, cover))
    monkeypatch.setattr(config, "ACTIVE_DIRECTION", {"name": "electrician", "cover_letter_fallback": ""}, raising=False)
    assert matcher.cover_letter_requires_review({}, cover)
    from tests.cover_letters import grounded_letter
    checked = grounded_letter("Проверенное письмо")
    assert not matcher.cover_letter_requires_review({}, checked)
    monkeypatch.setattr(config, "ACTIVE_DIRECTION", {"name": "qa", "cover_letter_fallback": ""}, raising=False)
    assert matcher.cover_letter_requires_review({}, checked)


@pytest.mark.parametrize("fallback", ["", None, "   "])
def test_direction_without_fallback_cannot_use_profile_letter(tmp_path, monkeypatch, fallback):
    profile_letter = "Письмо из другого направления профиля."
    (tmp_path / "cover_letter_fallback.md").write_text(profile_letter)
    monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))
    monkeypatch.setattr(config, "ACTIVE_DIRECTION", {"name": "qa", "cover_letter_fallback": fallback}, raising=False)
    assert matcher._fallback_cover_letter({}) != profile_letter
    assert matcher.cover_letter_requires_review({}, matcher.salary_policy.finish_cover({}, profile_letter))
