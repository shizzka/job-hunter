import prompt_blocks


def test_salary_rule_mentions_workload_dependency(monkeypatch):
    monkeypatch.setattr(prompt_blocks.config, "HH_AUTO_ANSWER_SALARY_BASELINE", "80000")
    monkeypatch.setattr(prompt_blocks.config, "HH_AUTO_ANSWER_SALARY_RULE", "")

    block = prompt_blocks.build_salary_rule_block()

    assert "конечные ожидания зависят от загрузки" in block
