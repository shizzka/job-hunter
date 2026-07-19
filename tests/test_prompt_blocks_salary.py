import facts
import prompt_blocks


def test_salary_rule_mentions_workload_dependency(monkeypatch):
    monkeypatch.setattr(prompt_blocks.config, "HH_AUTO_ANSWER_SALARY_BASELINE", "80000")
    monkeypatch.setattr(prompt_blocks.config, "HH_AUTO_ANSWER_SALARY_RULE", "")

    block = prompt_blocks.build_salary_rule_block()

    assert "конечные ожидания зависят от загрузки" in block


def test_format_structured_facts_includes_claim_guardrails():
    block = facts.format_facts_for_prompt({
        "confirmed": {
            "qa_experience": "8 месяцев QA на web/API проекте",
            "tools": ["Postman", "TestIT", "YouTrack"],
        },
        "weak": ["участие в API-автотестах Python/pytest"],
        "allowed_wording": ["участвовал в разработке API-автотестов"],
        "do_not_claim": ["самостоятельный AQA", "коммерческий опыт Java/Selenium"],
    })

    assert "CONFIRMED" in block
    assert "WEAK / LIMITED" in block
    assert "DO NOT CLAIM" in block
    assert "8 месяцев QA" in block
    assert "самостоятельный AQA" in block
    assert "не превращай слабые факты" in block


def test_format_flat_facts_remains_supported():
    block = facts.format_facts_for_prompt({"location": "СПб", "willing_remote": True})

    assert "- location: СПб" in block
    assert "- willing_remote: да" in block
