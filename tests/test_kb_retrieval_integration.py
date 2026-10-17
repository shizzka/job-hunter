"""Shared retrieval evidence across real generation entry points, offline only."""
import asyncio
import json
from dataclasses import asdict
from unittest.mock import AsyncMock

import pytest

import config
import matcher
import prompt_blocks
import hh_chat_responder as chat
from answer_grounding import capture_candidate, candidate_context, retrieve_candidate
from google_forms import answering
from hh import forms
from tests.test_answer_grounding_regressions import ScriptedResponses, question_dependencies
from tests.test_cover_grounding import approved, client_for

CLAIM = 'Проверяю Android на личном устройстве.'


def answer_evidence():
    return {'verdict': 'supported', 'answers': [{'index': 0, 'supported': True,
        'sentences': [{'index': 0, 'supported': True,
                       'evidence': [{'source': 'knowledge', 'quote': CLAIM}]}]}]}


@pytest.fixture
def corpus(tmp_path, monkeypatch):
    monkeypatch.setenv('KB_RETRIEVAL_ENABLED', '1')
    home = tmp_path / 'original'
    knowledge = home / 'knowledge'
    knowledge.mkdir(parents=True)
    (home / 'resume.md').write_text('Начинающий специалист', encoding='utf-8')
    (knowledge / 'profile_note.md').write_text('Кандидат: ручное тестирование.', encoding='utf-8')
    (knowledge / 'candidate_facts.md').write_text(
        '- [FACT] Общие данные. ' * 800 + '- [SKILL] ' + CLAIM
        + ' - [LIMIT] Не заявлять коммерческий стаж.'
        + ' - [DO_NOT_CLAIM] Не утверждать знание Kotlin.', encoding='utf-8')
    monkeypatch.setattr(config, 'RESUME_FILE', str(home / 'resume.md'))
    monkeypatch.setattr(config, 'HH_COOKIES_FILE', str(home / 'cookies.json'))
    monkeypatch.setattr(prompt_blocks, 'get_candidate_contacts', lambda: {})
    monkeypatch.setattr(prompt_blocks, 'build_salary_rule_block', lambda: '')
    monkeypatch.setattr(prompt_blocks, 'select_kb_sections', AsyncMock(side_effect=AssertionError('LLM selection forbidden')))
    return home


def assert_shared(client, author_index=0, checker_index=1):
    author = '\n'.join(m['content'] for m in client.calls[author_index]['messages'])
    checker = json.loads(client.calls[checker_index]['messages'][1]['content'])
    selected = checker['sources']['knowledge']
    assert CLAIM in selected and selected in author
    assert '[LIMIT]' in selected and '[DO_NOT_CLAIM]' in selected
    return selected


def test_cover_knowledge_author_and_checker_identical(corpus, monkeypatch):
    monkeypatch.setattr(matcher, '_load_resume', lambda: 'Начинающий специалист')
    client = client_for(CLAIM + ' Обсудим детали?', json.dumps(approved(CLAIM, 'knowledge')))
    monkeypatch.setattr(matcher, '_get_client', lambda: client)
    assert asyncio.run(matcher.generate_cover_letter({'title': 'Android QA', 'id': 'synthetic', 'salary': '100000 ₽'})) == CLAIM + ' Обсудим детали?'
    assert len(assert_shared(client)) <= 12000
    prompt_blocks.select_kb_sections.assert_not_awaited()


def test_chat_knowledge_author_and_checker_identical(corpus, monkeypatch):
    client = ScriptedResponses(({'status': 'answer', 'answer': CLAIM}, 'stop'), (answer_evidence(), 'stop'))
    monkeypatch.setattr(chat, '_get_llm_client', lambda: client)
    result = asyncio.run(chat.generate_answer([{'text': 'Есть Android?', 'is_ai': True}], {}, 'Начинающий специалист'))
    assert result == CLAIM
    assert_shared(client)
    prompt_blocks.select_kb_sections.assert_not_awaited()


@pytest.mark.parametrize('kind', ['text', 'choice', 'google'])
def test_questionnaire_uses_question_for_retrieval_and_shared_evidence(corpus, monkeypatch, kind):
    if kind == 'google':
        payload = {'answers': [{'index': 0, 'answer': CLAIM, 'options': [], 'skip': False, 'confidence': 'high'}]}
    elif kind == 'choice':
        payload = {'status': 'answer', 'selected': [{'index': 0}]}
    else:
        payload = {'status': 'answer', 'answer': CLAIM}
    client = ScriptedResponses((payload, 'stop'), (answer_evidence(), 'stop'))
    if kind == 'google':
        import hh_client
        monkeypatch.setattr(hh_client, '_load_resume_text', lambda: 'Начинающий специалист')
        result = asyncio.run(answering.generate_form_answers(
            [{'index': 0, 'question': 'Есть Android?', 'type': 'text'}], client_factory=lambda: client))
        assert result[0]['skip'] is False
    else:
        dependency = question_dependencies(client)
        field = {'question_text': 'Есть Android?', 'input_type': 'text'}
        function = forms.answer_question_with_llm
        if kind == 'choice':
            function = forms.answer_choice_with_llm
            field.update(control='radio', options=[{'index': 0, 'label': CLAIM}])
        result = asyncio.run(function(field, 'Начинающий специалист', **dependency))
        assert result
    assert_shared(client)


def test_bound_index_survives_profile_switch_and_source_change(corpus, tmp_path, monkeypatch):
    snapshot = capture_candidate('Начинающий специалист')
    json.dumps(asdict(snapshot), ensure_ascii=False)  # Chat revision compatibility.
    foreign = tmp_path / 'foreign'
    (foreign / 'knowledge').mkdir(parents=True)
    (foreign / 'knowledge' / 'facts.md').write_text('- [FACT] FOREIGN Android secrets.')
    monkeypatch.setattr(config, 'RESUME_FILE', str(foreign / 'resume.md'))
    (corpus / 'knowledge' / 'candidate_facts.md').write_text('Changed after snapshot')
    selected = retrieve_candidate(snapshot, 'Есть Android?')
    assert CLAIM in selected.knowledge
    assert 'FOREIGN' not in str(selected.sources)
    client = ScriptedResponses(({'status': 'answer', 'answer': CLAIM}, 'stop'), (answer_evidence(), 'stop'))
    async def run():
        with candidate_context(snapshot, client):
            return await chat.generate_answer([{'text': 'Есть Android?', 'is_ai': True}], {}, 'Foreign resume')
    assert asyncio.run(run()) == CLAIM
    assert_shared(client)


def test_flag_off_preserves_llm_selection(corpus, monkeypatch):
    monkeypatch.setenv('KB_RETRIEVAL_ENABLED', '0')
    monkeypatch.setattr(prompt_blocks, '_load_kb_filterable', lambda: ('', [{'num': 1, 'title': 'Legacy', 'content': 'LEGACY'}]))
    selector = AsyncMock(return_value=[1])
    monkeypatch.setattr(prompt_blocks, 'select_kb_sections', selector)
    block = asyncio.run(prompt_blocks.build_filtered_kb_block('Android', object()))
    assert 'LEGACY' in block
    selector.assert_awaited_once()
    snapshot = capture_candidate('Original resume')
    assert snapshot.knowledge_index == ''
    assert 'knowledge' not in snapshot.sources
    assert retrieve_candidate(snapshot, 'Android') is snapshot


def test_metadata_never_enters_legacy_prompts(corpus, monkeypatch):
    (corpus / 'knowledge' / 'kb_eval_questions.txt').write_text('PRIVATE_EVAL_LABEL')
    (corpus / 'knowledge' / 'synonyms.txt').write_text('PRIVATE_SYNONYM_LABEL')
    result = prompt_blocks.build_knowledge_base_block(limit_chars=50000)
    assert 'PRIVATE_' not in result
    monkeypatch.setenv('KB_RETRIEVAL_ENABLED', '0')
    result = asyncio.run(prompt_blocks.build_filtered_kb_block('', None, limit_chars=50000))
    assert 'PRIVATE_' not in result


@pytest.mark.parametrize('kind', ['cover', 'chat'])
def test_mandatory_overflow_never_uses_truncated_fallback(corpus, monkeypatch, kind):
    from kb_retrieval import KnowledgeBudgetError
    (corpus / 'knowledge' / 'candidate_facts.md').write_text('[DO_NOT_CLAIM] ' + 'Не заявлять коммерческий стаж. ' * 1000)
    client = client_for('Should not be generated')
    monkeypatch.setattr(matcher, '_get_client', lambda: client)
    monkeypatch.setattr(chat, '_get_llm_client', lambda: client)
    monkeypatch.setattr(matcher, '_load_resume', lambda: 'Начинающий специалист')
    with pytest.raises(KnowledgeBudgetError):
        if kind == 'cover':
            asyncio.run(matcher.generate_cover_letter({'title': 'Android QA', 'id': 'synthetic'}))
        else:
            asyncio.run(chat.generate_answer([{'text': 'Есть Android?', 'is_ai': True}], {}, 'Начинающий специалист'))
    assert not client.calls


def test_retrieved_quote_cannot_certify_invented_numbers(corpus, monkeypatch):
    forged_claim = 'Проверяю Android 12 лет.'
    client = ScriptedResponses(({'status': 'answer', 'answer': forged_claim}, 'stop'), (answer_evidence(), 'stop'))
    monkeypatch.setattr(chat, '_get_llm_client', lambda: client)
    assert asyncio.run(chat.generate_answer([{'text': 'Есть Android?', 'is_ai': True}], {}, 'Начинающий специалист')) is None
    assert_shared(client)
