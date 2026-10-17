"""Offline salary policy regressions: no HH, Telegram or model connections."""
import asyncio
from types import SimpleNamespace

import pytest

import config
import matcher
import salary_policy as policy


@pytest.fixture(autouse=True)
def salary_question_on(monkeypatch):
    monkeypatch.setattr(config, "COVER_ASK_SALARY_RANGE", True)


@pytest.mark.parametrize("value, upper", [
    ("от 70 000 до 90 000 ₽ за месяц", 90000),
    ("до 90\u202f000 руб.", 90000), ("70–90 тыс. ₽", 90000),
    ("100000", 100000), ("от 70 000 ₽", None),
    ("100000+", None), ("до 90 000 USD", None),
    ("до 900 ₽ в час", None), ("не указана", None),
    ({"from": 80000, "to": 90000, "currency": "RUR"}, 90000),
    ({"from": 80000, "currency": "RUR"}, None),
])
def test_upper_bound(value, upper):
    assert policy.salary_upper_rub({"salary": value}) == upper


def test_baseline_direction_precedes_legacy(monkeypatch):
    monkeypatch.setattr(config, "HH_AUTO_ANSWER_SALARY_BASELINE", "100000")
    monkeypatch.setattr(config, "ACTIVE_DIRECTION", {"salary_baseline": 120000}, raising=False)
    assert "120 000" in policy.confirmation_reason({"salary": "до 110 000 ₽"})
    monkeypatch.setattr(config, "ACTIVE_DIRECTION", None)
    assert not policy.confirmation_reason({"salary": "до 110 000 ₽"})
    assert policy.confirmation_reason({"salary": "до 90 000 ₽"})
    assert not policy.confirmation_reason({"salary": "от 90 000 ₽"})


@pytest.mark.parametrize("missing", [None, "", "не указана", "по договорённости", {}])
def test_salary_free_cover_always_ends_polite_question(missing):
    vacancy = {"salary": missing}
    cover = policy.finish_cover(vacancy, "Здравствуйте! Готов обсудить задачи.")
    assert cover.endswith(policy.SALARY_QUESTION)
    assert policy.finish_cover(vacancy, cover) == cover
    assert policy.finish_cover({"salary": "100000 ₽"}, "Текст.") == "Текст."


@pytest.mark.parametrize("flag", ["Не указана зарплата", "Salary range missing", "Отсутствует вилка оплаты"])
def test_salary_flags_are_soft_for_all_role_policies(flag):
    result = matcher._soften_salary_red_flags({"score": 80, "red_flags": [flag, "Мошенничество"]})
    assert result["red_flags"] == ["Мошенничество"]
    assert flag in result["soft_flags"]


class FakePage:
    def __init__(self, text):
        self.text = text
        self.reads = []

    async def query_selector(self, selector):
        self.reads.append(selector)
        assert selector == "body"
        return self

    async def inner_text(self):
        return self.text


def test_rendered_hh_warning_requires_exact_card_acknowledgement():
    warning = "Скорее всего, будет отказ."
    page = FakePage("Вакансия\n" + warning + " Зарплата ниже ожиданий")
    session = SimpleNamespace(_page=page)
    assert asyncio.run(policy.unapproved_hh_salary_warning(session)) == warning
    assert policy.warning_result(session._hh_salary_warning)["requires_salary_confirmation"]
    session._approved_salary_warning = warning
    assert not asyncio.run(policy.unapproved_hh_salary_warning(session))
    page.text = "Скорее всего, будет отказ из-за зарплатных ожиданий."
    assert asyncio.run(policy.unapproved_hh_salary_warning(session))
    assert page.reads == ["body", "body", "body"]


def test_no_warning_does_not_stop():
    page = FakePage("Зарплата не указана. Вакансия открыта.")
    assert not asyncio.run(policy.read_hh_salary_warning(page))


def test_hh_warning_real_dom_stops_before_any_application_post(tmp_path, monkeypatch):
    import tests.test_e2e_hh_apply_happy_path as gate_module
    warning = "Скорее всего, будет отказ: зарплатные ожидания выше предложения."
    monkeypatch.setattr(gate_module, "FIXTURE", gate_module.FIXTURE + f'<div role="alert">{warning}</div>')
    gate = asyncio.run(gate_module.run_hh_gate(tmp_path, monkeypatch, noise=False))
    assert gate.application_posts == [], gate.evidence
    assert gate.result["applied"] == 0, gate.evidence
    assert gate.entries["2"]["action"] == "manual_hh_salary_confirmation", gate.evidence


@pytest.mark.parametrize("mode", ["auto", "confirm"])
def test_below_baseline_search_creates_confirmation_card(tmp_path, monkeypatch, mode):
    from unittest.mock import AsyncMock
    import agent
    from tests.test_observability import configure_synthetic_search
    for key, value in {
        "JOB_HUNTER_HOME": str(tmp_path), "RUN_HISTORY_FILE": str(tmp_path / "runs.jsonl"),
        "RUNTIME_STATUS_FILE": str(tmp_path / "runtime.json"),
        "SEEN_VACANCIES_FILE": str(tmp_path / "seen.json"),
        "HH_STATE_DIR": str(tmp_path / "hh"), "HH_GUARD_STATE_FILE": str(tmp_path / "guard.json"),
    }.items():
        monkeypatch.setattr(config, key, value)
    configure_synthetic_search(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "HH_APPLICATION_MODE", mode)
    monkeypatch.setattr(config, "HH_AUTO_ANSWER_SALARY_BASELINE", "100000")
    vacancy = {"id": "4", "source": "hh", "title": "QA", "company": "Synthetic", "salary": "до 90 000 ₽"}
    monkeypatch.setattr(agent.search_pipeline, "collect_all", AsyncMock(return_value=[vacancy]))
    dispatch = AsyncMock(side_effect=AssertionError("Salary requires approval first"))
    monkeypatch.setattr(agent.apply_orchestrator, "dispatch_apply", dispatch)
    captured = []
    def queue(*args, **kwargs):
        captured.append((args, kwargs))
        return {"token": "synthetic"}
    monkeypatch.setattr(agent.manual_apply_queue, "create_candidate", queue)
    result = asyncio.run(agent.do_search())
    dispatch.assert_not_awaited()
    assert result["applied"] == 0 and captured[0][1]["allow_ai_apply"] is True
    assert "90 000" in agent.notify_needs_manual.await_args.kwargs["note"]
    assert "100 000" in agent.notify_needs_manual.await_args.kwargs["note"]


def test_cover_replaces_generic_closing_instead_of_adding_second_ending():
    cover = policy.finish_cover({}, "Здравствуйте! Факт. Обсудим детали?")
    assert "Обсудим детали?" not in cover
    assert cover.endswith(policy.SALARY_QUESTION)


def test_warning_survives_queue_compaction(tmp_path, monkeypatch):
    import manual_apply_queue as queue
    monkeypatch.setattr(queue, "_queue_path", lambda profile_name=None: tmp_path / "queue.json")
    warning = "Скорее всего, будет отказ."
    item = queue.create_candidate({"id": "salary", "source": "hh", "_hh_salary_warning": warning},
                                  {"score": 80}, profile_name="synthetic")
    assert queue.get_candidate(item["token"])["vacancy"]["_hh_salary_warning"] == warning


def test_owner_confirmed_apply_records_warning_without_blocking():
    warning = "Скорее всего, будет отказ."
    session = SimpleNamespace(_page=FakePage("Отклик\n" + warning + " Зарплата ниже"),
                              _owner_confirmed_application=True)
    assert asyncio.run(policy.unapproved_hh_salary_warning(session)) == ""
    assert session._hh_salary_warning == warning
