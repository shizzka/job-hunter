"""Offline search, matching, manual approval and owner-card regressions."""
import asyncio
from unittest.mock import AsyncMock

import pytest

import config
import matcher
import work_conditions as wc
from tests.test_work_conditions import policy, vacancy


@pytest.mark.parametrize("fields", [
    {"@workSchedule": "flyInFlyOut"}, {"schedule": {"id": "flyInFlyOut"}},
    {"employmentForm": "FLY_IN_FLY_OUT"},
    {"flyInFlyOutDurations": [{"flyInFlyOutDurationsElement": ["DAYS_30"]}]},
    {"compensation": {"mode": "FLY_IN_FLY_OUT"}},
    {"salary_range": {"period": {"id": "FLY_IN_FLY_OUT"}}},
])
def test_structured_shift_work_rejected_before_matching(monkeypatch, fields):
    monkeypatch.setattr(config, "ACTIVE_DIRECTION", policy(), raising=False)
    v = vacancy(**fields, workFormats=[{"workFormatsElement": ["ON_SITE"]}])
    llm = AsyncMock()
    monkeypatch.setattr(matcher, "_get_client", llm)
    result = asyncio.run(matcher.evaluate_vacancy(v))
    assert result["work_conditions_status"] == "reject" and result["score"] is None
    assert result["should_apply"] is False
    assert not matcher.is_manual_review_candidate({**result, "score": 55})
    assert "Вахта" in result["reason"]
    llm.assert_not_called()



@pytest.mark.parametrize("mode", ["auto", "confirm", "preview"])
@pytest.mark.parametrize("case", ["fly", "on_site", "unknown", "allowed"])
def test_search_conditions_gate_all_modes_before_evaluation(tmp_path, monkeypatch, mode, case):
    import agent
    from tests.test_observability import configure_synthetic_search
    from hh import negotiation_snapshot
    configure_synthetic_search(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "HH_APPLICATION_MODE", mode)
    monkeypatch.setattr(config, "ACTIVE_DIRECTION", policy(allowed_formats=["remote", "hybrid"]), raising=False)
    monkeypatch.setattr(negotiation_snapshot, "refresh_for_chats", lambda *a: [])
    fields = {"workFormats": ["ON_SITE" if case == "on_site" else "REMOTE"]}
    if case == "fly":
        fields["@workSchedule"] = "flyInFlyOut"
    elif case == "unknown":
        fields = {}
    v = {**vacancy(**fields), "company": "Пример", "salary": "не указана",
         "url": "https://hh.ru/vacancy/42"}
    monkeypatch.setattr(agent.search_pipeline, "collect_all", AsyncMock(return_value=[v]))
    monkeypatch.setattr(agent.search_pipeline.filters, "check_vacancy", lambda v: None)
    evaluate = AsyncMock(return_value={"score": 90, "should_apply": True, "reason": "Подходит", "red_flags": []})
    monkeypatch.setattr(agent, "evaluate_vacancy", evaluate)
    dispatch = AsyncMock(return_value={"ok": False, "already_applied": True})
    monkeypatch.setattr(agent.apply_orchestrator, "dispatch_apply", dispatch)
    result = asyncio.run(agent.do_search())
    if case != "allowed":
        evaluate.assert_not_awaited()
        agent.generate_cover_letter.assert_not_awaited()
        dispatch.assert_not_awaited()
        if case == "unknown":
            card = agent.notify_needs_manual.await_args
            assert card.args[1] is None and card.args[2] == "формат не указан"
            assert card.kwargs["reply_markup"] is None
            assert result["source_stats"]["hh"]["manual"] == 1
        else:
            agent.notify_needs_manual.assert_not_awaited()
            agent.apply_orchestrator.fetch_vacancy_details.assert_not_awaited()
            assert result["source_stats"]["hh"]["rejected"] == 1
    else:
        evaluate.assert_awaited_once()
        assert dispatch.await_count == (1 if mode == "auto" else 0)



@pytest.mark.parametrize("case", ["fly", "on_site", "unknown"])
def test_manual_queue_cannot_override_work_conditions(tmp_path, monkeypatch, case):
    import agent
    import direction_runtime
    import manual_apply_queue
    monkeypatch.setattr(config, "ACTIVE_DIRECTION", policy(allowed_formats=["remote", "hybrid"]), raising=False)
    monkeypatch.setattr(config, "HH_APPLICATION_MODE", "confirm")
    monkeypatch.setattr(direction_runtime, "validate_queued", lambda v: None)
    fields = {"workFormats": ["ON_SITE" if case == "on_site" else "REMOTE"]}
    if case == "fly":
        fields["@workSchedule"] = "flyInFlyOut"
    elif case == "unknown":
        fields = {}
    item = manual_apply_queue.create_candidate(vacancy(**fields), {"score": 95}, "Описание")
    # Old approved cards remain blocked even with an optimistic saved score.
    client = AsyncMock()
    monkeypatch.setattr(agent, "HHClient", client)
    result = asyncio.run(agent.do_manual_apply_token(item["token"]))
    assert result["reason"] == "work_conditions" and not result["ok"]
    assert manual_apply_queue.get_candidate(item["token"])["status"] == "conditions_blocked"
    client.assert_not_called()


def test_manual_approval_refreshes_cached_conditions_before_cover(monkeypatch):
    import agent
    import direction_runtime
    import manual_apply_queue
    from types import SimpleNamespace
    monkeypatch.setattr(config, "ACTIVE_DIRECTION", policy(), raising=False)
    monkeypatch.setattr(config, "HH_APPLICATION_MODE", "confirm")
    monkeypatch.setattr(direction_runtime, "validate_queued", lambda v: None)
    item = manual_apply_queue.create_candidate(vacancy(workFormats=["REMOTE"]), {"score": 95}, "Старое описание")
    client = SimpleNamespace(start=AsyncMock(), is_logged_in=AsyncMock(return_value=True), stop=AsyncMock())
    monkeypatch.setattr(agent, "HHClient", lambda: client)
    monkeypatch.setattr(agent.hh_guard, "can_auto_apply", lambda: (True, ""))
    async def fresh(v, **kwargs):
        v["_hh_work_conditions"] = wc.from_hh_fields({"@workSchedule": "flyInFlyOut"})
        return "Новое описание"
    monkeypatch.setattr(agent.apply_orchestrator, "fetch_vacancy_details", fresh)
    cover = AsyncMock()
    monkeypatch.setattr(agent, "generate_cover_letter", cover)
    result = asyncio.run(agent.do_manual_apply_token(item["token"]))
    assert result["reason"] == "work_conditions" and result["work_conditions_status"] == "reject"
    cover.assert_not_awaited()
    client.stop.assert_awaited_once()



def test_unevaluated_card_has_no_fake_score_or_suitability(monkeypatch):
    import notifier
    sent = AsyncMock(return_value=True)
    monkeypatch.setattr(notifier, "send_message", sent)
    asyncio.run(notifier.notify_needs_manual({"title": "Электромонтажник"}, None,
        "Страница вакансии не открылась.", note="Нужна проверка вручную."))
    text = sent.await_args.args[0]
    assert "Оценка не проводилась: Страница вакансии не открылась." in text
    assert "Вакансия на разбор" in text
    assert "0/100" not in text and "Совпадение" not in text and "Подходящая" not in text



def test_guard_stop_before_evaluation_does_not_invent_a_zero_score(tmp_path, monkeypatch):
    import agent
    from hh.ui import HHUnexpectedUI
    from tests.test_observability import configure_synthetic_search
    configure_synthetic_search(monkeypatch, tmp_path)
    v = {"id": "42", "source": "hh", "title": "Электромонтажник", "company": "Пример"}
    monkeypatch.setattr(agent.search_pipeline, "collect_all", AsyncMock(return_value=[v]))
    monkeypatch.setattr(agent.search_pipeline.filters, "check_vacancy", lambda v: None)
    monkeypatch.setattr(agent.apply_orchestrator, "fetch_vacancy_details",
                        AsyncMock(side_effect=HHUnexpectedUI("vacancy_details", "a" * 64)))
    evaluation = AsyncMock()
    monkeypatch.setattr(agent, "evaluate_vacancy", evaluation)
    result = asyncio.run(agent.do_search())
    assert result["hh_recovery"]["stopped"] is True
    evaluation.assert_not_awaited()
    card = agent.notify_needs_manual.await_args
    assert card.args[1] is None and "до оценки" in card.args[2]
    assert "fingerprint" not in card.kwargs["note"] and "UI" not in card.kwargs["note"]
