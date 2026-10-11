"""Offline HH questionnaire routing regressions; never submit a response."""
import asyncio
import json
from types import SimpleNamespace

import pytest

import config
import hh_client
import prompt_blocks
from answer_grounding import current_answer_client, current_candidate
from llm_client import LLMProvidersExhaustedError


RESUME = 'Я выбираю удалённую работу.'
LABEL = 'Удалённая работа'


class Router:
    """Mock only the shared router transport, retaining generation and grounding."""
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason='stop', message=SimpleNamespace(content=json.dumps(response)))])


def evidence(supported=True):
    return {'verdict': 'supported' if supported else 'unsupported', 'answers': [
        {'index': 0, 'supported': supported, 'sentences': [
            {'index': 0, 'supported': supported,
             'evidence': [{'source': 'resume', 'quote': RESUME}]}]}]}


def choice(control='radio'):
    return {'field_id': 'synthetic-choice', 'question_text': 'Выберите режим взаимодействия',
            'control': control, 'required': True, 'options': [
                {'index': 2, 'label': LABEL}, {'index': 5, 'label': 'Офис'}]}


@pytest.fixture
def synthetic(monkeypatch, tmp_path):
    import candidate_interview
    import facts
    monkeypatch.setattr(facts, 'load_facts', lambda: {})
    monkeypatch.setattr(candidate_interview, 'prompt_block', lambda: '')
    monkeypatch.setattr(prompt_blocks, 'get_candidate_contacts', lambda: {})
    for name in ('build_profile_note_block', 'build_facts_block',
                 'build_salary_rule_block', 'build_knowledge_base_block'):
        monkeypatch.setattr(prompt_blocks, name, lambda *a, **k: '')
        monkeypatch.setattr(hh_client, '_' + name, lambda *a, **k: '')
    async def filtered(*a, **k):
        return ''
    monkeypatch.setattr(hh_client, '_build_filtered_kb_block', filtered)
    settings = {'HH_AUTO_ANSWER_SIMPLE_QUESTIONS': True,
                'HH_AUTO_ANSWER_USE_LLM': True, 'LLM_API_KEY': '',
                'HH_AUTO_ANSWER_MAX_QUESTIONS': 5, 'HH_AUTO_ANSWER_MAX_CHARS': 500,
                'HH_AUTO_ANSWER_SALARY_TEXT': '', 'HH_AUTO_ANSWER_SALARY_NUMBER': '',
                'HH_QUESTION_MODEL': '', 'HH_CHOICE_MODEL': '', 'LLM_MODEL': 'synthetic-router',
                'HH_COOKIES_FILE': str(tmp_path / 'synthetic-cookies.json')}
    for name, value in settings.items():
        monkeypatch.setattr(config, name, value)
    monkeypatch.setattr(hh_client, '_load_resume_text', lambda: RESUME)
    monkeypatch.setattr(hh_client, '_question_answer_client', None)


def install_router(monkeypatch, router):
    calls = []
    def factory():
        calls.append(True)
        if isinstance(router, Exception):
            raise router
        return router
    monkeypatch.setattr(hh_client, 'get_llm_client', factory)
    return calls


def session_for(field, router=None):
    session = hh_client.HHClient.__new__(hh_client.HHClient)
    session._cookie_paths = SimpleNamespace(cookies_file=config.HH_COOKIES_FILE)
    session.filled = []
    session.inspected = []
    session.boundaries = []
    async def inspect():
        session.inspected.append(current_answer_client())
        if router is not None:
            assert current_answer_client() is router, 'Shared router must be captured before browser awaits'
        await asyncio.sleep(0)
        return {'fields': [field], 'unsupported_fields': 0, 'page_text': 'Synthetic questionnaire'}
    async def fill(answers):
        session.filled.extend(answers)
        return {'filled': len(answers), 'errors': []}
    async def wait(*a):
        pass
    async def forbidden_submit(**kwargs):
        pytest.fail('Questionnaire regression must stop before submit')
    async def before_submit():
        session.boundaries.append(bool(session.filled))
        # Authorize preflight; stop once answers have been filled and checked.
        return not session.filled
    session._inspect_employer_questions = inspect
    session._fill_employer_question_answers = fill
    session._submit_employer_questions = forbidden_submit
    session._page = SimpleNamespace(wait_for_timeout=wait)
    return session, before_submit


@pytest.mark.parametrize('control', ['radio', 'select'])
def test_no_legacy_key_shared_router_choice_reaches_fill_boundary(synthetic, monkeypatch, control):
    router = Router({'status': 'answer', 'selected': [{'index': 2}]}, evidence())
    factory_calls = install_router(monkeypatch, router)
    session, boundary = session_for(choice(control), router)
    result = asyncio.run(session._try_auto_answer_questions(before_submit=boundary))
    assert factory_calls == [True]
    assert len(router.calls) == 2, 'Choice generation and independent grounding must both run'
    assert json.loads(router.calls[1]['messages'][1]['content'])['answers'][0]['answer'] == LABEL
    assert session.filled == [{'field_id': 'synthetic-choice', 'control': control,
                               'selected_indices': [2], 'custom_text': None}]
    assert session.boundaries == [False, True]
    assert result['handled'] and not result['ok']
    assert result['question_answers'][0]['answer'] == LABEL
    assert current_candidate() is None and current_answer_client() is None


def test_direct_choice_wrapper_uses_shared_router_without_legacy_key(synthetic, monkeypatch):
    router = Router({'status': 'answer', 'selected': [{'index': 2}]}, evidence())
    factory_calls = install_router(monkeypatch, router)
    session, _ = session_for(choice())
    result = asyncio.run(session._answer_choice_with_llm(choice(), RESUME))
    assert result is not None and result['is_skip'] is False
    assert result['selected'] == [{'index': 2, 'custom_text': None}]
    assert factory_calls == [True] and len(router.calls) == 2


@pytest.mark.parametrize('failure', ['unavailable', 'exhausted', 'ambiguous', 'invalid_index', 'unsupported'])
def test_no_key_router_failure_requires_manual_without_fill_or_submit(synthetic, monkeypatch, failure):
    draft = {'status': 'answer', 'selected': [{'index': 2}]}
    if failure == 'unavailable':
        router = RuntimeError('Synthetic router unavailable')
        expected_calls = 0
    elif failure == 'exhausted':
        router = Router(LLMProvidersExhaustedError(['synthetic'], 'synthetic-router', RuntimeError('quota')))
        expected_calls = 1
    else:
        if failure == 'ambiguous':
            draft['selected'].append({'index': 5})
        if failure == 'invalid_index':
            draft['selected'] = [{'index': 99}]
        router = Router(draft, evidence(False))
        expected_calls = 2 if failure == 'unsupported' else 1
    factory_calls = install_router(monkeypatch, router)
    session, boundary = session_for(choice())
    result = asyncio.run(session._try_auto_answer_questions(before_submit=boundary))
    assert result['handled'] and result['ok'] is False
    assert session.filled == [] and session.boundaries == []
    assert factory_calls == [True], 'Exercise shared router availability, not the legacy key guard'
    if not isinstance(router, Exception):
        assert len(router.calls) == expected_calls
    assert current_candidate() is None and current_answer_client() is None


@pytest.mark.parametrize('disabled', ['optout', 'empty_resume'])
def test_questionnaire_llm_optout_and_empty_resume_still_skip(synthetic, monkeypatch, disabled):
    if disabled == 'optout':
        monkeypatch.setattr(config, 'HH_AUTO_ANSWER_USE_LLM', False)
    else:
        monkeypatch.setattr(hh_client, '_load_resume_text', lambda: '')
    router = Router()
    factory_calls = install_router(monkeypatch, router)
    session, boundary = session_for(choice())
    result = asyncio.run(session._try_auto_answer_questions(before_submit=boundary))
    assert result['handled'] and result['ok'] is False
    assert session.filled == [] and router.calls == []
    if disabled == 'optout':
        assert factory_calls == []


def test_no_legacy_key_text_question_also_reaches_fill_boundary(synthetic, monkeypatch):
    router = Router({'status': 'answer', 'answer': RESUME}, evidence())
    factory_calls = install_router(monkeypatch, router)
    field = {'field_id': 'synthetic-text', 'control': 'textarea', 'input_type': 'textarea',
             'question_text': 'Какой режим взаимодействия вы выбираете?', 'required': True}
    session, boundary = session_for(field, router)
    result = asyncio.run(session._try_auto_answer_questions(before_submit=boundary))
    assert factory_calls == [True] and len(router.calls) == 2
    assert session.filled == [{'field_id': 'synthetic-text', 'answer': RESUME}]
    assert session.boundaries == [False, True]
    assert result['handled'] and result['ok'] is False
