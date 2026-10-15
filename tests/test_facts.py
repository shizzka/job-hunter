"""Cover facts must retain restrictions independently of the factual budget."""
import pytest

import facts


@pytest.mark.parametrize("limit", [0, 1, 50, 3000])
def test_restrictions_survive_large_facts_and_both_ban_fields(limit):
    personal = {
        "confirmed": {"project": "Длинный проект. " * 1000},
        "weak": ["Только участие в автотестах; не самостоятельный AQA."],
        "allowed_wording": ["QA-стаж: 8 месяцев, junior."],
        "do_not_claim": ["Не заявлять middle-уровень."],
        "forbidden_claims": ["Не заявлять 3 года QA."],
    }
    block = facts.format_facts_for_prompt(personal, limit_chars=limit)
    for key in ("weak", "allowed_wording", "do_not_claim", "forbidden_claims"):
        assert personal[key][0] in block
    constraints = facts.format_fact_constraints(personal)
    assert block.endswith(constraints)
    assert len(block.removesuffix(constraints)) <= limit


@pytest.mark.parametrize("limit", [0, 1, 2, 3, 100, 3000])
def test_fact_budget_includes_truncation_marker(limit):
    block = facts.format_facts_for_prompt({"confirmed": {"project": "x" * 5000}}, limit_chars=limit)
    assert len(block) <= limit


def test_cover_can_keep_complete_confirmed_facts_without_advisory_claims():
    personal = {"confirmed": {"project": "x" * 5000, "level": "Стаж 8 месяцев, junior."},
                "inferred": {"role": "Неподтверждённая роль"}}
    block = facts.confirmed_facts_for_prompt(personal, limit_chars=None)
    assert personal["confirmed"]["project"] in block
    assert personal["confirmed"]["level"] in block
    assert personal["inferred"]["role"] not in block
