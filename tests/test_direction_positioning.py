"""One direction intent for scoring, covers and forms; no external services."""
import asyncio
import json

import pytest

import config
import directions
import matcher
import prompt_blocks
from answer_grounding import capture_candidate, retrieve_candidate
from kb_retrieval import get_knowledge_index
from tests.test_directions import draft
from tests.test_cover_grounding import approved, client_for

POSITION = 'Монтирую кабельные линии и собираю щиты.'
OLD = 'Переход в QA: Junior Manual QA'
BANS = ['Не заявлять коммерческий стаж QA.', 'Без ночных смен.', 'Не утверждать действующую группу ЭБ.']


@pytest.fixture
def direction_context(tmp_path, monkeypatch):
    knowledge = tmp_path / 'knowledge'
    knowledge.mkdir()
    (knowledge / 'profile_note.md').write_text(OLD + '\n\n- [DO_NOT_CLAIM] ' + BANS[0])
    (knowledge / 'candidate_facts.md').write_text(
        '# Карьерная цель\n- [GOAL] ' + OLD + '\n\n# Общие сведения\n'
        '- [FACT] Есть опыт электромонтажа.\n- [LIMIT] ' + BANS[1])
    (tmp_path / 'facts.json').write_text(json.dumps({
        'confirmed': {'target_roles': [OLD], 'education': 'Техническое образование'},
        'inferred': {'positioning': OLD}, 'allowed_wording': [OLD],
        'do_not_claim': [BANS[0]]}, ensure_ascii=False))
    (tmp_path / 'candidate_interview.json').write_text(json.dumps({'facts': [
        {'topic': 'целевая роль', 'text': OLD}, {'topic': 'общий', 'text': 'Монтаж электрощитов.'},
        {'topic': 'границы', 'text': BANS[1]}]}, ensure_ascii=False))
    value = draft('electrician', count=1)
    value.update(positioning=POSITION, goal=['Электромонтажник'], allowed_wording=['Электрик'],
                 do_not_claim_extra=[BANS[2]])
    saved = directions.save_direction(tmp_path, value, texts={'hash-0': 'Резюме: монтаж электрощитов.'})
    directions.set_active_direction(tmp_path, saved['name'])
    monkeypatch.setattr(config, 'ACTIVE_DIRECTION', saved, raising=False)
    monkeypatch.setattr(config, 'JOB_HUNTER_HOME', str(tmp_path))
    monkeypatch.setattr(config, 'RESUME_FILE', saved['variants'][0]['text_file'])
    monkeypatch.setattr(config, 'VACANCY_FILTER_POLICY', 'generic')
    monkeypatch.setattr(config, 'HH_AUTO_ANSWER_PROFILE_NOTE', OLD)
    monkeypatch.setattr(matcher.resume_versions, 'record_input', lambda *a: None)
    monkeypatch.setattr(prompt_blocks, 'get_candidate_contacts', lambda: {})
    return tmp_path


def assert_context(text):
    assert POSITION in text
    assert OLD not in text
    assert all(ban in text for ban in BANS)
    assert 'Электромонтажник' in text


@pytest.mark.parametrize('retrieval', ['0', '1'])
def test_same_intent_and_boundaries_for_every_prompt(direction_context, monkeypatch, retrieval):
    monkeypatch.setenv('KB_RETRIEVAL_ENABLED', retrieval)
    assert_context(matcher._build_matcher_truth_block({'title': 'Электромонтажник'}))
    snapshot = retrieve_candidate(capture_candidate('Резюме: монтаж электрощитов.'), 'Кем хотите работать?')
    assert_context(snapshot.profile_note + snapshot.facts + snapshot.knowledge)
    assert OLD not in snapshot.evidence_facts
    assert snapshot.confirmed['target_roles'] == ['Электромонтажник']
    client = client_for(POSITION + ' Обсудим детали?', json.dumps(approved(POSITION, 'profile_note')))
    monkeypatch.setattr(matcher, '_get_client', lambda: client)
    letter = asyncio.run(matcher.generate_cover_letter({'id': 'synthetic', 'title': 'Электромонтажник', 'salary': '100000'}))
    assert POSITION in letter
    assert len(client.calls) == 2
    assert_context(client.calls[0]['messages'][0]['content'])
    sources = json.loads(client.calls[1]['messages'][1]['content'])['sources']
    assert POSITION in sources['profile_note']
    assert OLD not in json.dumps(sources, ensure_ascii=False)


def test_knowledge_snapshot_is_direction_specific_and_freezes_intent(direction_context, monkeypatch):
    first = get_knowledge_index(direction_context)
    assert_context(first.retrieve('электрик', limit_chars=12000))
    monkeypatch.setattr(config, 'ACTIVE_DIRECTION', {**config.ACTIVE_DIRECTION, 'positioning': 'Обслуживаю оборудование.'})
    second = get_knowledge_index(direction_context)
    assert 'Обслуживаю оборудование.' in second.retrieve('', limit_chars=12000)
    assert POSITION in first.retrieve('', limit_chars=12000)
    monkeypatch.setattr(config, 'ACTIVE_DIRECTION', None)
    assert OLD in get_knowledge_index(direction_context).retrieve('', limit_chars=12000)


def test_empty_intent_keeps_original_context(direction_context, monkeypatch):
    monkeypatch.setattr(config, 'ACTIVE_DIRECTION', {**config.ACTIVE_DIRECTION,
        'positioning': '', 'goal': [], 'allowed_wording': [], 'do_not_claim_extra': []})
    assert OLD in prompt_blocks.build_profile_note_block()
    assert OLD in prompt_blocks.build_facts_block()


def test_positioning_validation(direction_context):
    value = directions.active_direction(direction_context)
    value['positioning'] = 'x' * 1501
    with pytest.raises(directions.DirectionError, match='1500'):
        directions.save_direction(direction_context, value)


def test_suggestion_uses_selected_resume_and_common_facts_only(direction_context, monkeypatch):
    import search_query_suggester as suggester
    payload = {'title': 'Электрика', 'filter_policy': 'generic', 'search_queries': ['Электрик'],
               'relevant_keywords': ['монтаж'], 'exclude_keywords': [], 'positioning': POSITION,
               'goal': ['Электромонтажник'], 'allowed_wording': ['Электрик']}
    client = client_for(json.dumps(payload, ensure_ascii=False))
    monkeypatch.setattr(suggester, 'get_llm_client', lambda: client)
    result = asyncio.run(suggester.suggest_direction(
        resumes=[{'title': 'Электрик', 'raw': 'Монтаж электрощитов.'}], profile_dir=str(direction_context)))
    assert result['positioning'] == POSITION and result['goal'] == ['Электромонтажник']
    prompt = client.calls[0]['messages'][1]['content']
    assert 'Монтаж электрощитов.' in prompt and 'Техническое образование' in prompt
    assert OLD not in prompt
    assert 'Не выдумывай' in client.calls[0]['messages'][0]['content']


def test_questionnaire_prompt_and_grounding_use_direction_positioning(direction_context):
    from hh import forms
    from tests.test_answer_grounding_regressions import ScriptedResponses, evidence, question_dependencies
    client = ScriptedResponses(({'status': 'answer', 'answer': POSITION}, 'stop'),
                               (evidence(POSITION, source='profile_note'), 'stop'))
    dependencies = question_dependencies(client)
    dependencies.update(build_profile_note_block=prompt_blocks.build_profile_note_block,
                        build_facts_block=prompt_blocks.build_facts_block,
                        build_knowledge_base_block=prompt_blocks.build_knowledge_base_block,
                        build_filtered_kb_block=prompt_blocks.build_filtered_kb_block)
    answer = asyncio.run(forms.answer_question_with_llm(
        {'question_text': 'Кем вы работаете?', 'input_type': 'textarea'}, 'Резюме: монтаж электрощитов.',
        **dependencies))
    assert answer == POSITION
    author = '\n'.join(message['content'] for message in client.calls[0]['messages'])
    assert_context(author)
    sources = json.loads(client.calls[1]['messages'][1]['content'])['sources']
    assert POSITION in sources['profile_note']
    assert OLD not in json.dumps(sources, ensure_ascii=False)


def test_legacy_intent_aliases_removed_without_erasing_structured_bans():
    from direction_knowledge import overlay_facts, markdown_parts, interview_items
    value = {'positioning': POSITION}
    data = {'confirmed': {'profile_note': OLD}, 'do_not_claim': {'target_roles': 'Не заявлять стаж QA'}}
    result = overlay_facts(data, value)
    assert OLD not in json.dumps(result, ensure_ascii=False)
    assert result['do_not_claim'] == data['do_not_claim']
    assert list(markdown_parts('knowledge/limits.md', '# Карьерная цель\nНельзя заявлять опыт QA', value))
    assert interview_items([{'topic': 'цель', 'text': OLD}], value) == []


def test_null_fact_sections_keep_common_constraints():
    from direction_knowledge import overlay_facts
    result = overlay_facts({'confirmed': None, 'inferred': None, 'do_not_claim': ['Общий запрет']},
                           {'goal': ['Электромонтажник'], 'positioning': POSITION})
    assert result['confirmed']['target_roles'] == ['Электромонтажник']
    assert result['inferred']['positioning'] == POSITION
    assert result['do_not_claim'] == ['Общий запрет']
