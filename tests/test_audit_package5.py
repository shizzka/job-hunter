"""Extractor output cannot certify unsupported consent or identity claims."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import facts
import config
from answer_grounding import capture_candidate, validate_answer_check
from tests.test_answer_grounding_regressions import blocks


@pytest.fixture
def extracted(tmp_path, monkeypatch):
    resume = tmp_path / 'resume.md'
    resume.write_text('Junior QA. Postman and SQL. No relocation statement.')
    monkeypatch.setattr(config, 'RESUME_FILE', str(resume))
    response = SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content=json.dumps(
        {'willing_relocate': True, 'summary': 'Готов к переезду.', 'salary': 500000})))])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=response))))
    monkeypatch.setattr(facts, '_get_llm_client', lambda: client)
    return resume


def test_unsupported_extraction_never_becomes_confirmed_grounding(extracted, blocks):
    value = asyncio.run(facts.extract_facts_from_resume(extracted.read_text()))
    facts.save_facts(value)
    snapshot = capture_candidate(extracted.read_text())
    claim = 'Готов к переезду.'
    forged = {'verdict': 'supported', 'answers': [{'index': 0, 'supported': True, 'sentences': [
        {'index': 0, 'supported': True, 'evidence': [{'source': 'facts', 'quote': 'willing_relocate: да'}]}]}]}
    assert validate_answer_check(forged, [{'index': 0, 'answer': claim}], snapshot.sources) == set()
    assert not snapshot.confirmed.get('willing_relocate')
    assert value['unconfirmed']['willing_relocate'] is True
    assert value['_provenance']['kind'] == 'model_extraction'
    assert len(value['_provenance']['resume_sha256']) == 64


def test_extractor_cannot_promote_its_own_confirmed_section(extracted, monkeypatch, blocks):
    response = SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop', message=SimpleNamespace(content=
                json.dumps({'confirmed': {'willing_relocate': True}})))])
    monkeypatch.setattr(facts, '_get_llm_client', lambda: SimpleNamespace(chat=SimpleNamespace(
                completions=SimpleNamespace(create=AsyncMock(return_value=response)))))
    value = asyncio.run(facts.extract_facts_from_resume(extracted.read_text()))
    facts.save_facts(value)
    assert not capture_candidate(extracted.read_text()).confirmed.get('willing_relocate')


def test_flat_legacy_extraction_has_no_confirmed_provenance(extracted, blocks):
    facts.save_facts({'willing_relocate': True, 'summary': 'Готов к переезду.'})
    assert 'willing_relocate' not in capture_candidate(extracted.read_text()).sources.get('facts', '')


def test_extraction_preserves_existing_user_confirmed_facts_and_bans(extracted):
    facts.save_facts({'confirmed': {'location': 'User-confirmed city'}, 'do_not_claim': ['Relocation']})
    value = asyncio.run(facts.extract_facts_from_resume(extracted.read_text()))
    facts.save_facts(value)
    saved = facts.load_facts()
    assert saved['confirmed'] == {'location': 'User-confirmed city'}
    assert saved['do_not_claim'] == ['Relocation']
    assert saved['unconfirmed']['willing_relocate'] is True


def test_actual_cover_grounding_cannot_cite_extraction(extracted, blocks, monkeypatch):
    import matcher
    import prompt_blocks
    value = asyncio.run(facts.extract_facts_from_resume(extracted.read_text()))
    facts.save_facts(value)
    claim = 'Готов к переезду.'
    verifier = {'verdict': 'supported', 'sentences': [{'index': 0, 'supported': True,
                'evidence': [{'source': 'facts', 'quote': 'willing_relocate: да'}]}]}
    responses = [claim, json.dumps(verifier)]
    async def create(**kwargs):
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason='stop',
            message=SimpleNamespace(content=responses.pop(0)))])
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))
    monkeypatch.setattr(matcher, '_get_client', lambda: client)
    monkeypatch.setattr(matcher, '_load_resume', extracted.read_text)
    monkeypatch.setattr(prompt_blocks, 'build_facts_block', lambda: facts.format_facts_for_prompt(facts.load_facts()))
    cover = asyncio.run(matcher.generate_cover_letter({'id': 'synthetic', 'title': 'QA'}))
    assert cover != claim
    assert matcher.analyze_cover_letter(cover)['overclaim_guard']
