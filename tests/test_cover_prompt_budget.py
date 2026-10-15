"""Offline regressions for selected knowledge, restrictions and shared evidence."""
import asyncio
import json
import logging
from unittest.mock import AsyncMock, Mock

import pytest

import candidate_interview
import facts
import matcher
import prompt_blocks
from tests.test_cover_grounding import approved, client_for


def test_selected_sections_survive_oversized_common_knowledge(monkeypatch, caplog):
    sections = [{"num": n, "title": f"QA {n}", "content": f"SELECTED_{n}_" * 60} for n in range(1, 7)]
    monkeypatch.setattr(prompt_blocks, "_load_kb_filterable", lambda: ("common " * 3000, sections))
    monkeypatch.setattr(prompt_blocks, "select_kb_sections", AsyncMock(return_value=list(range(1, 7))))
    with caplog.at_level(logging.INFO):
        block = asyncio.run(prompt_blocks.build_filtered_kb_block("QA", None, max_sections=6, limit_chars=12000))
    assert len(block) <= 12000
    for section in sections:
        assert section["content"] in block
    assert block.index(sections[-1]["content"]) < block.index("common ")
    assert "sections_full=[1, 2, 3, 4, 5, 6]" in caplog.text
    assert "sections_dropped=0" in caplog.text


def test_selected_sections_alone_over_budget_are_kept_and_logged(monkeypatch, caplog):
    section = {"num": 1, "title": "QA", "content": "SELECTED " * 2000}
    monkeypatch.setattr(prompt_blocks, "_load_kb_filterable", lambda: ("COMMON", [section]))
    monkeypatch.setattr(prompt_blocks, "select_kb_sections", AsyncMock(return_value=[1]))
    with caplog.at_level(logging.WARNING):
        block = asyncio.run(prompt_blocks.build_filtered_kb_block("QA", None, limit_chars=100))
    assert section["content"] in block
    assert "COMMON" not in block
    assert "sections_full=[1]" in caplog.text
    assert "over_budget=true" in caplog.text


@pytest.mark.parametrize("limit", [0, 1, 2, 100, 12000])
def test_fallback_kb_budget_includes_header_and_truncation_marker(tmp_path, monkeypatch, limit):
    (tmp_path / "answering_rules.md").write_text("x" * 18000)
    monkeypatch.setattr(prompt_blocks, "_knowledge_dir", lambda *args: str(tmp_path))
    assert len(prompt_blocks.build_knowledge_base_block(limit_chars=limit)) <= limit
    assert len(prompt_blocks._format_kb_block("x" * 18000, [], limit_chars=limit)) <= limit


def test_duplicate_section_numbers_preserve_sql_and_assign_unused_number(monkeypatch):
    sections = prompt_blocks._parse_kb_sections(
        "## 13. SQL\nSQL_ONLY\n## 13. Personal facts\nPERSONAL_ONLY\n## 14. API\nAPI_ONLY\n")
    numbers = [section["num"] for section in sections]
    assert numbers == [13, 15, 14]
    monkeypatch.setattr(prompt_blocks, "_load_kb_filterable", lambda: ("", sections))
    monkeypatch.setattr(prompt_blocks, "select_kb_sections", AsyncMock(return_value=[13, 13]))
    block = asyncio.run(prompt_blocks.build_filtered_kb_block("SQL", None))
    assert block.count("SQL_ONLY") == 1
    assert "PERSONAL_ONLY" not in block


def test_cover_author_and_checker_share_confirmed_snapshot_including_late_interview(
        monkeypatch):
    claim = "Проверяю REST API в Postman."
    personal = {"confirmed": {"project": "x" * 3500, "experience": "Стаж 8 месяцев, junior."},
                "inferred": {"other": "INFERRED_ONLY"}, "weak": ["Не самостоятельный AQA."],
                "do_not_claim": ["FORBIDDEN_ONLY"]}
    load_facts = Mock(return_value=personal)
    interview = [
        {"topic": "опыт", "text": "y" * 3500}, {"topic": "опыт", "text": claim},
        {"topic": "границы", "text": "Не заявлять middle-уровень."}]
    load_interview = Mock(return_value=interview)
    monkeypatch.setattr(facts, "load_facts", load_facts)
    monkeypatch.setattr(candidate_interview, "facts", load_interview)
    monkeypatch.setattr(prompt_blocks, "build_profile_note_block", lambda: "")
    monkeypatch.setattr(prompt_blocks, "build_knowledge_base_block", lambda **kwargs: "")
    sections = [{"num": n, "title": f"QA {n}", "content": f"SELECTED_{n}"} for n in range(1, 7)]
    monkeypatch.setattr(prompt_blocks, "_load_kb_filterable", lambda: ("COMMON " * 3000, sections))
    monkeypatch.setattr(prompt_blocks, "select_kb_sections", AsyncMock(return_value=list(range(1, 7))))
    monkeypatch.setattr(matcher, "_load_resume", lambda: "Junior QA")
    monkeypatch.setattr(matcher, "_detect_candidate_claim_overstatements", lambda text: [])
    letter = claim + " Обсудим детали?"
    client = client_for(letter, json.dumps(approved(claim, "facts")))
    original_create = client.chat.completions.create

    async def create(**kwargs):
        # Mutate backing data after capture: verification must use the old snapshot.
        personal["confirmed"]["experience"] = "FOREIGN_PROFILE_ONLY"
        interview[1]["text"] = "FOREIGN_INTERVIEW_ONLY"
        return await original_create(**kwargs)

    client.chat.completions.create = create
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    result = asyncio.run(matcher.generate_cover_letter({"title": "QA", "id": "unit-only"}))
    assert result == letter
    load_facts.assert_called_once_with()
    load_interview.assert_called_once_with(None)
    assert matcher.analyze_cover_letter(result)["grounding_status"] == "verified"
    author = client.calls[0]["messages"][0]["content"]
    checker = json.loads(client.calls[1]["messages"][1]["content"])
    assert checker["sources"]["facts"] in author
    assert checker["sources"]["knowledge"] in author
    for section in sections:
        assert section["content"] in author
    for text in (claim, "Стаж 8 месяцев, junior."):
        assert text in author and text in checker["sources"]["facts"]
    assert "Не заявлять middle-уровень." in author
    assert "Не заявлять middle-уровень." in checker["constraints"]
    assert "FORBIDDEN_ONLY" in author and "FORBIDDEN_ONLY" in checker["constraints"]
    for text in ("INFERRED_ONLY", "FOREIGN_PROFILE_ONLY", "FOREIGN_INTERVIEW_ONLY", "FORBIDDEN_ONLY"):
        assert text not in checker["sources"]["facts"]
    assert "INFERRED_ONLY" not in author
    assert "Учебные материалы, общие правила и шаблоны" in client.calls[1]["messages"][0]["content"]
    assert "в любых источниках НЕ" in client.calls[1]["messages"][0]["content"]


def test_cover_interview_keeps_whole_facts_and_unbounded_late_restrictions(monkeypatch):
    restrictions = "Не заявлять middle и 3 года QA. " * 150
    monkeypatch.setattr(candidate_interview, "facts", lambda profile_dir=None: [
        {"text": "Слишком длинный факт. " * 200, "topic": "опыт"},
        {"text": "Проверяю API в Postman.", "topic": "инструменты"},
        {"text": restrictions, "topic": "границы"}])
    evidence, constraints = candidate_interview.cover_prompt_blocks(limit_chars=300)
    assert len(evidence) <= 300
    assert "Слишком длинный факт" not in evidence
    assert "Проверяю API в Postman." in evidence
    assert restrictions in constraints
    assert restrictions not in evidence


@pytest.mark.parametrize("limit", [0, 1, 2, 3, 3000])
def test_interview_budget_includes_truncation_marker(monkeypatch, limit):
    monkeypatch.setattr(candidate_interview, "facts", lambda profile_dir=None: [{"text": "x" * 4000}])
    assert len(candidate_interview.prompt_block(limit_chars=limit)) <= limit
