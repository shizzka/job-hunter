"""Previous-tree compatible safety regressions; synthetic clients/data only."""
import asyncio
import json
import logging
from types import SimpleNamespace

import pytest

import hh_chat_responder as chat
import hh_client
import prompt_blocks
from google_forms import answering
from hh import forms


class Responses:
    def __init__(self, payload, after=None):
        self.payload, self.after, self.calls = payload, after, []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.after:
            self.after()
        return SimpleNamespace(choices=[SimpleNamespace(
            finish_reason='stop', message=SimpleNamespace(content=json.dumps(self.payload)))])


@pytest.fixture
def blocks(monkeypatch):
    for name in ('build_profile_note_block', 'build_facts_block', 'build_salary_rule_block',
                 'build_contact_block', 'build_knowledge_base_block'):
        monkeypatch.setattr(prompt_blocks, name, lambda *a, **k: '')
    async def filtered(*a, **k): return ''
    monkeypatch.setattr(prompt_blocks, 'build_filtered_kb_block', filtered)
    monkeypatch.setattr(prompt_blocks, 'get_candidate_contacts', lambda: {})
    monkeypatch.setattr(hh_client, '_load_resume_text', lambda: 'Unrelated resume')


@pytest.mark.parametrize('question', [
    'Предоставите справку с места учёбы?', 'Готовы заполнить короткую форму?'])
def test_chat_templates_cannot_assert_candidate_facts_without_evidence(blocks, monkeypatch, question):
    client = Responses({'status': 'skip'})
    monkeypatch.setattr(chat, '_get_llm_client', lambda: client)
    assert asyncio.run(chat.generate_answer([{'text': question, 'is_ai': True}], {}, 'Unrelated resume')) is None


def test_chat_draft_is_not_its_own_factual_verification(blocks, monkeypatch):
    client = Responses({'status': 'answer', 'answer': 'У меня 12 лет коммерческого опыта Kubernetes.'})
    monkeypatch.setattr(chat, '_get_llm_client', lambda: client)
    assert asyncio.run(chat.generate_answer([{'text': 'Опыт Kubernetes?', 'is_ai': True}], {}, 'Junior designer')) is None


def test_google_form_high_confidence_is_not_evidence(blocks):
    client = Responses({'answers': [{'index': 0, 'answer': '12 лет Kubernetes',
                                    'options': [], 'skip': False, 'confidence': 'high'}]})
    answers = asyncio.run(answering.generate_form_answers(
        [{'index': 0, 'question': 'Опыт Kubernetes?', 'type': 'text'}], client_factory=lambda: client))
    assert answers and answers[0]['skip'] is True and answers[0]['confidence'] == 'low'


def test_form_contact_override_keeps_snapshot_across_generation(blocks, monkeypatch):
    contacts = {'email': 'original@example.test'}
    monkeypatch.setattr(prompt_blocks, 'get_candidate_contacts', lambda: dict(contacts))
    client = Responses({'answers': []}, lambda: contacts.update(email='other@example.test'))
    answers = asyncio.run(answering.generate_form_answers(
        [{'index': 0, 'question': 'Email', 'type': 'text'}], client_factory=lambda: client))
    assert answers[0]['answer'] == 'original@example.test'


def test_form_confirmed_fact_override_keeps_snapshot_across_generation(blocks, monkeypatch):
    import facts
    confirmed = {'phone_model': 'Original Phone'}
    monkeypatch.setattr(facts, 'load_facts', lambda: {'confirmed': dict(confirmed)})
    client = Responses({'answers': []}, lambda: confirmed.update(phone_model='Other Phone'))
    answers = asyncio.run(answering.generate_form_answers(
        [{'index': 0, 'question': 'Модель телефона', 'type': 'text'}], client_factory=lambda: client))
    assert answers[0]['answer'] == 'Original Phone'


def question_dependencies(client):
    async def filtered(*a, **k): return ''
    settings = SimpleNamespace(HH_AUTO_ANSWER_USE_LLM=True, LLM_API_KEY='synthetic',
        HH_AUTO_ANSWER_MAX_CHARS=500, HH_QUESTION_MODEL='', HH_CHOICE_MODEL='', LLM_MODEL='synthetic')
    return dict(settings=settings, logger=logging.getLogger('synthetic'),
        get_question_answer_client=lambda: client,
        build_salary_rule_block=lambda: '', build_facts_block=lambda: '',
        build_profile_note_block=lambda: '', build_filtered_kb_block=filtered,
        build_knowledge_base_block=lambda **k: '', parse_llm_json=json.loads,
        repair_llm_json=lambda *a, **k: None)


def test_hh_library_cannot_claim_experience_for_unrelated_candidate():
    client = Responses({'status': 'skip'})
    assert asyncio.run(forms.answer_question_with_llm(
        {'question_text': 'Ваш опыт SQL?', 'input_type': 'textarea'}, 'Designer',
        **question_dependencies(client))) is None


def test_hh_text_draft_is_not_its_own_factual_verification():
    client = Responses({'status': 'answer', 'answer': '12 лет Kubernetes'})
    assert asyncio.run(forms.answer_question_with_llm(
        {'question_text': 'Ваш опыт Kubernetes?', 'input_type': 'textarea'}, 'Designer',
        **question_dependencies(client))) is None


def test_hh_choice_draft_requires_evidence():
    client = Responses({'status': 'answer', 'selected': [{'index': 1}]})
    result = asyncio.run(forms.answer_choice_with_llm(
        {'question_text': 'Есть 12 лет опыта Kubernetes?', 'control': 'radio',
         'options': [{'index': 0, 'label': 'Нет'}, {'index': 1, 'label': 'Да'}]},
        'Designer', **question_dependencies(client)))
    assert result is None or result.get('is_skip') is True


def test_hh_choice_skip_does_not_force_a_guess():
    client = Responses({'status': 'skip'})
    result = asyncio.run(forms.answer_choice_with_llm(
        {'question_text': 'Согласны на переезд?', 'control': 'radio',
         'options': [{'index': 0, 'label': 'Нет'}, {'index': 1, 'label': 'Да'}]},
        'Designer', **question_dependencies(client)))
    assert result.get('is_skip') is True
    assert len(client.calls) == 1


class ScriptedResponses:
    def __init__(self, *responses, after=None):
        self.responses, self.after, self.calls = list(responses), after, []
        self.chat = SimpleNamespace(completions=self)

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        payload, finish = self.responses.pop(0)
        if self.after:
            self.after(len(self.calls))
        return SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish,
            message=SimpleNamespace(content=json.dumps(payload)))])


def evidence(answer, quote=None, source='resume', index=0):
    return {'verdict': 'supported', 'answers': [{'index': index, 'supported': True,
        'sentences': [{'index': 0, 'supported': True,
            'evidence': [{'source': source, 'quote': quote or answer}]}]}]}


def test_positive_hh_choice_preserves_actual_dom_index_with_placeholder_gap():
    proof = 'Я выбираю удалённую работу.'
    client = ScriptedResponses(({'status': 'answer', 'selected': [{'index': 2}]}, 'stop'),
                               (evidence('Удалённая работа', proof), 'stop'))
    result = asyncio.run(forms.answer_choice_with_llm(
        {'question_text': 'Выберите формат работы', 'control': 'select',
         'options': [{'index': 2, 'label': 'Удалённая работа'}, {'index': 5, 'label': 'Офис'}]},
        proof, **question_dependencies(client)))
    assert result['selected'] == [{'index': 2, 'custom_text': None}]
    assert result['is_skip'] is False
    checker = json.loads(client.calls[1]['messages'][1]['content'])
    assert checker['answers'][0]['answer'] == 'Удалённая работа'


@pytest.mark.parametrize('finish', ['length', 'content_filter', 'tool_calls'])
def test_truncated_choice_does_not_even_reach_verifier(finish):
    proof = 'Я выбираю удалённую работу.'
    client = ScriptedResponses(({'status': 'answer', 'selected': [{'index': 2}]}, finish),
                               (evidence('Удалённая работа', proof), 'stop'))
    result = asyncio.run(forms.answer_choice_with_llm(
        {'question_text': 'Выберите формат работы', 'control': 'select',
         'options': [{'index': 2, 'label': 'Удалённая работа'}]},
        proof, **question_dependencies(client)))
    assert result is None
    assert len(client.calls) == 1


@pytest.mark.parametrize('draft', [[], 'answer', 1, {'status': 'answer', 'answer': 1}])
def test_malformed_text_answer_is_controlled_skip(draft):
    client = Responses(draft)
    assert asyncio.run(forms.answer_question_with_llm(
        {'question_text': 'Какой у вас опыт Figma?', 'input_type': 'textarea'},
        'Designer', **question_dependencies(client))) is None


@pytest.mark.parametrize('finish', ['length', 'content_filter', 'tool_calls'])
def test_verifier_incomplete_response_never_approves(finish):
    from answer_grounding import verify_answers
    answer = 'У меня 2 года опыта SQL.'
    client = ScriptedResponses((evidence(answer), finish))
    assert asyncio.run(verify_answers([{'index': 0, 'question': 'Опыт SQL?', 'answer': answer}],
                                     {'resume': answer}, client, 'synthetic')) == set()


@pytest.mark.parametrize('failure', ['wrong_source', 'wrong_quote', 'wrong_index', 'bool_index',
                                     'duplicate', 'missing_sentence', 'invented_number', 'unsupported'])
def test_local_citation_guards_reject_optimistic_verifier(failure):
    from answer_grounding import validate_answer_check
    answer = 'У меня 2 года опыта SQL.'
    payload = evidence(answer)
    row = payload['answers'][0]
    sources = {'resume': answer}
    if failure == 'wrong_source': row['sentences'][0]['evidence'][0]['source'] = 'hr'
    if failure == 'wrong_quote': row['sentences'][0]['evidence'][0]['quote'] = 'I know Python'
    if failure == 'wrong_index': row['index'] = 42
    if failure == 'bool_index': row['index'] = False
    if failure == 'duplicate': payload['answers'].append(row)
    if failure == 'missing_sentence': row['sentences'] = []
    if failure == 'invented_number':
        sources = {'resume': 'У меня опыт SQL.'}
        row['sentences'][0]['evidence'][0]['quote'] = sources['resume']
    if failure == 'unsupported': payload['verdict'] = 'unsupported'
    assert validate_answer_check(payload, [{'index': 0, 'answer': answer}], sources) == set()


def test_verifier_captures_sources_and_models_before_await():
    from answer_grounding import verify_answers
    answer = 'У меня 2 года опыта SQL.'
    sources = {'resume': answer}
    client = ScriptedResponses((evidence(answer), 'stop'), after=lambda n: sources.update(resume='Other candidate'))
    approved = asyncio.run(verify_answers([{'index': 0, 'question': 'Опыт SQL?', 'answer': answer}],
                                        sources, client, 'captured-model'))
    assert approved == {0}
    assert client.calls[0]['model'] == 'captured-model'
    assert json.loads(client.calls[0]['messages'][1]['content'])['sources']['resume'] == answer


def test_contextvars_isolate_concurrent_candidate_operations():
    from answer_grounding import candidate_context, current_candidate, current_answer_client
    async def run():
        async def task(snapshot, client):
            with candidate_context(snapshot, client):
                await asyncio.sleep(0)
                assert current_candidate() == snapshot
                assert current_answer_client() == client
            assert current_candidate() is None
        await asyncio.gather(task('candidate-a', 'client-a'), task('candidate-b', 'client-b'))
    asyncio.run(run())


@pytest.mark.parametrize('qtype,draft,options', [
    ('text', {'answer': '12 лет Kubernetes', 'options': ['SQL']}, []),
    ('checkbox', {'answer': 'Kubernetes', 'options': ['SQL']}, ['SQL', 'Kubernetes']),
    ('radio', {'answer': 'SQL', 'options': ['SQL']}, ['SQL и Kubernetes']),
    ('select', {'answer': '', 'options': ['SQL', 'Kubernetes']}, ['SQL', 'Kubernetes']),
])
def test_google_form_verifies_only_canonical_actual_filled_values(blocks, qtype, draft, options):
    client = ScriptedResponses(({'answers': [{'index': 0, 'skip': False, **draft}]}, 'stop'))
    result = asyncio.run(answering.generate_form_answers(
        [{'index': 0, 'question': 'Ваш опыт?', 'type': qtype, 'options': options}],
        client_factory=lambda: client))
    assert result == []
    assert len(client.calls) == 1


def test_google_checkbox_verifies_every_exact_label_before_preparation(blocks, monkeypatch):
    claim = 'SQL\nKubernetes'
    monkeypatch.setattr(hh_client, '_load_resume_text', lambda: claim)
    proof = {'verdict': 'supported', 'answers': [{'index': 0, 'supported': True, 'sentences': [
        {'index': i, 'supported': True, 'evidence': [{'source': 'resume', 'quote': label}]}
        for i, label in enumerate(['SQL', 'Kubernetes'])]}]}
    client = ScriptedResponses(({'answers': [{'index': 0, 'skip': False, 'answer': 'SQL',
                                             'options': ['SQL', 'Kubernetes']}]}, 'stop'), (proof, 'stop'))
    answers = asyncio.run(answering.generate_form_answers(
        [{'index': 0, 'question': 'Ваш опыт?', 'type': 'checkbox', 'options': ['SQL', 'Kubernetes']}],
        client_factory=lambda: client))
    checker = json.loads(client.calls[1]['messages'][1]['content'])
    assert checker['answers'][0]['answer'] == claim
    assert answers[0]['options'] == ['SQL', 'Kubernetes'] and answers[0]['skip'] is False


def test_forbidden_claim_constraints_survive_conflicting_positive_resume(blocks, monkeypatch):
    import facts
    from answer_grounding import capture_candidate, verify_answers
    claim = 'У меня опыт Kubernetes.'
    monkeypatch.setattr(facts, 'load_facts', lambda: {'forbidden_claims': [claim]})
    snapshot = capture_candidate(claim)
    client = ScriptedResponses(({'verdict': 'unsupported', 'answers': [{'index': 0, 'supported': False}]}, 'stop'))
    approved = asyncio.run(verify_answers([{'index': 0, 'question': 'Опыт?', 'answer': claim}],
                                         snapshot.sources, client, 'synthetic', constraints=snapshot.fact_constraints))
    checker = json.loads(client.calls[0]['messages'][1]['content'])
    assert claim in checker['sources']['resume']
    assert claim in checker['constraints']
    assert 'приоритет' in client.calls[0]['messages'][0]['content']
    assert approved == set()


@pytest.mark.parametrize('entrypoint', ['process_one', 'process_all'])
def test_native_chat_captures_resume_and_client_before_browser_navigation(blocks, monkeypatch, tmp_path, entrypoint):
    from answer_grounding import current_candidate, current_answer_client
    from runtime_context import RuntimePaths
    original = Responses({'status': 'skip'})
    foreign = Responses({'status': 'skip'})
    current = {'resume': 'Original candidate', 'client': original}
    monkeypatch.setattr(hh_client, '_load_resume_text', lambda: current['resume'])
    monkeypatch.setattr(chat, '_get_llm_client', lambda: current['client'])
    observed = []
    class Page:
        async def goto(self, *args, **kwargs):
            current.update(resume='Other candidate', client=foreign)
            await asyncio.sleep(0)
        async def wait_for_timeout(self, *args): pass
    async def execute(*args, **kwargs):
        observed.append((current_candidate().resume, current_answer_client()))
        return {'ok': True}
    async def messages(*args):
        return {'messages': [{'id': 'm1', 'text': 'Опыт SQL?', 'is_ai': True}], 'vacancy': {}}
    async def chats(*args):
        observed.append((current_candidate().resume, current_answer_client()))
        return []
    monkeypatch.setattr(chat, '_execute_reply', execute)
    monkeypatch.setattr(chat, 'get_messages', messages)
    monkeypatch.setattr(chat, 'list_chats', chats)
    owner = SimpleNamespace(_page=Page())
    paths = RuntimePaths(str(tmp_path), str(tmp_path / 'state'), str(tmp_path / 'resume.md'))
    if entrypoint == 'process_one':
        asyncio.run(chat.process_one(owner, 'chat', dry_run=True, runtime_paths=paths))
    else:
        asyncio.run(chat.process_all(owner, dry_run=True, runtime_paths=paths))
    assert observed == [('Original candidate', original)]
    assert current_candidate() is None and current_answer_client() is None


def test_unavailable_original_answer_client_cannot_switch_after_await(blocks, monkeypatch):
    from answer_grounding import candidate_operation, current_answer_client, UnavailableAnswerClient
    current = {'client': None}
    foreign = Responses({'status': 'answer', 'answer': 'Foreign answer'})
    def factory():
        if current['client'] is None:
            raise RuntimeError('Original client unavailable')
        return current['client']
    @candidate_operation(client_factory=factory)
    async def operation():
        current['client'] = foreign
        await asyncio.sleep(0)
        assert isinstance(current_answer_client(), UnavailableAnswerClient)
        with pytest.raises(RuntimeError, match='Original candidate'):
            await current_answer_client().chat.completions.create(model='synthetic')
    asyncio.run(operation())
    assert foreign.calls == []


def test_bound_chat_never_loads_foreign_kb_or_model_after_browser_await(blocks, monkeypatch):
    from answer_grounding import capture_candidate, candidate_context
    import config
    claim = 'У меня опыт SQL.'
    monkeypatch.setattr(prompt_blocks, 'build_knowledge_base_block', lambda **kwargs: 'Original candidate KB')
    snapshot = capture_candidate(claim)
    client = ScriptedResponses(({'status': 'answer', 'answer': claim}, 'stop'), (evidence(claim), 'stop'))
    async def forbidden_filter(*args, **kwargs):
        pytest.fail('Bound chat must not read foreign config or KB')
    monkeypatch.setattr(prompt_blocks, 'build_filtered_kb_block', forbidden_filter)
    monkeypatch.setattr(config, 'LLM_MODEL', 'foreign-model')
    async def run():
        with candidate_context(snapshot, client):
            return await chat.generate_answer([{'id': 'm1', 'text': 'Какой у вас опыт SQL?', 'is_ai': True}], {}, 'Foreign resume')
    assert asyncio.run(run()) == claim
    assert client.calls[0]['model'] == snapshot.models['LLM_MODEL']
    assert 'Original candidate KB' in client.calls[0]['messages'][1]['content']


@pytest.mark.parametrize('label', ['Record my email and consent to marketing',
                                  'Указать электронную почту и передать данные партнёрам',
                                  'Record other@example.test as the email to be included with my response'])
def test_email_consent_hook_cannot_click_unreviewed_checkbox(label):
    from google_forms.filling import _fill_google_form_email_consent
    class Page:
        def locator(self, *args):
            pytest.fail('Unreviewed consent must not inspect/click arbitrary checkbox')
    assert asyncio.run(_fill_google_form_email_consent(Page())) is False


def test_native_answer_operation_rejects_mismatched_account_before_browser(blocks, monkeypatch, tmp_path):
    import config
    from answer_grounding import candidate_operation
    monkeypatch.setattr(config, 'HH_COOKIES_FILE', str(tmp_path / 'original-cookies.json'))
    called = []
    @candidate_operation(client_factory=lambda: Responses({}))
    async def operation(owner): called.append(True)
    owner = SimpleNamespace(_cookie_paths=SimpleNamespace(cookies_file=str(tmp_path / 'other-cookies.json')))
    with pytest.raises(ValueError, match='profile/account mismatch'):
        asyncio.run(operation(owner))
    assert called == []


@pytest.mark.parametrize('finish', ['length', 'content_filter', 'tool_calls', None])
def test_json_repair_cannot_accept_incomplete_parseable_response(finish):
    from llm_utils import repair_llm_json
    client = ScriptedResponses(({'status': 'answer', 'answer': 'Partial'}, finish))
    with pytest.raises(ValueError, match='Incomplete JSON repair'):
        asyncio.run(repair_llm_json(client, model='synthetic', raw_text='broken'))


@pytest.mark.parametrize('section', ['inferred', 'weak', 'do_not_claim', 'forbidden_claims', 'allowed_wording'])
def test_nonconfirmed_fact_cannot_be_cited_by_optimistic_verifier(blocks, monkeypatch, section):
    import facts
    import candidate_interview
    from answer_grounding import capture_candidate, validate_answer_check
    claim = 'У меня 12 лет Kubernetes.'
    monkeypatch.setattr(facts, 'load_facts', lambda: {section: {'experience': claim}})
    monkeypatch.setattr(candidate_interview, 'prompt_block', lambda: '')
    snapshot = capture_candidate('Junior designer')
    payload = evidence(claim)
    payload['answers'][0]['sentences'][0]['evidence'][0]['source'] = 'facts'
    assert claim not in snapshot.sources.get('facts', '')
    assert validate_answer_check(payload, [{'index': 0, 'answer': claim}], snapshot.sources) == set()


@pytest.mark.parametrize('kind', ['structured', 'legacy', 'interview'])
def test_confirmed_fact_evidence_remains_usable(blocks, monkeypatch, kind):
    import facts
    import candidate_interview
    from answer_grounding import capture_candidate, validate_answer_check
    claim = 'У меня 2 года SQL.'
    personal = {'confirmed': {'experience': claim}} if kind == 'structured' else {'experience': claim}
    monkeypatch.setattr(facts, 'load_facts', lambda: {} if kind == 'interview' else personal)
    monkeypatch.setattr(candidate_interview, 'prompt_block', lambda: claim if kind == 'interview' else '')
    snapshot = capture_candidate('Junior designer')
    payload = evidence(claim)
    payload['answers'][0]['sentences'][0]['evidence'][0]['source'] = 'facts'
    assert validate_answer_check(payload, [{'index': 0, 'answer': claim}], snapshot.sources) == (set() if kind == 'legacy' else {0})


def test_whole_native_google_preview_keeps_original_contacts_after_navigation(blocks, monkeypatch, tmp_path):
    import google_form_filler as gforms
    from tests.test_google_form_drafts import browser_stubs
    from runtime_context import RuntimePaths
    from unittest.mock import AsyncMock
    contacts = {'email': 'original@example.test'}
    monkeypatch.setattr(prompt_blocks, 'get_candidate_contacts', lambda: dict(contacts))
    questions = [{'index': 0, 'question': 'Email', 'type': 'text', 'required': True}]
    page = browser_stubs(monkeypatch, questions)
    goto = page.goto
    async def navigate(*args, **kwargs):
        await goto(*args, **kwargs)
        contacts.update(email='other-candidate@example.test')
    page.goto = navigate
    monkeypatch.setattr(gforms, 'generate_form_answers', AsyncMock(return_value=[]))
    filled = []
    async def fill(page, qs, answers, *, approval_owner):
        assert approval_owner
        filled.extend(answers)
        return {'filled': [{'index': 0}], 'skipped': []}
    monkeypatch.setattr(gforms, 'fill_form', fill)
    detail = asyncio.run(gforms.preview_form(page, 'https://docs.google.com/forms/d/e/synthetic/viewform',
        profile_name='qa', runtime_paths=RuntimePaths(str(tmp_path), str(tmp_path / 'state'), str(tmp_path / 'resume.md'))))
    assert filled[0]['answer'] == 'original@example.test'
    assert detail['answers'][0]['answer'] == 'original@example.test'
    from answer_grounding import current_candidate
    assert current_candidate() is None


def test_native_google_preview_does_not_autofill_unverified_cached_experience(blocks, monkeypatch, tmp_path):
    import google_form_filler as gforms
    from tests.test_google_form_drafts import browser_stubs
    from runtime_context import RuntimePaths
    from unittest.mock import AsyncMock
    questions = [{'index': 0, 'question': 'Опыт Kubernetes?', 'type': 'text', 'required': True}]
    page = browser_stubs(monkeypatch, questions)
    monkeypatch.setattr(gforms, 'generate_form_answers', AsyncMock(return_value=[]))
    monkeypatch.setattr(gforms, '_reuse_cached_answers', lambda *a: [
        {'index': 0, 'answer': '12 лет Kubernetes', 'confidence': 'high', 'skip': False}])
    filled = []
    async def fill(page, qs, answers, *, approval_owner):
        assert approval_owner
        filled.extend(answers)
        return {'filled': [], 'skipped': [{'index': 0}]}
    monkeypatch.setattr(gforms, 'fill_form', fill)
    detail = asyncio.run(gforms.preview_form(page, 'https://docs.google.com/forms/d/e/synthetic/viewform',
        profile_name='qa', runtime_paths=RuntimePaths(str(tmp_path), str(tmp_path / 'state'), str(tmp_path / 'resume.md'))))
    assert filled[0]['skip'] is True
    assert detail['answers'][0]['source'] == 'cached_needs_confirmation'
    assert detail['answers'][0]['confidence'] == 'low'


def test_custom_text_cannot_hide_selected_noncustom_experience_claim(blocks):
    client = ScriptedResponses(({'status': 'answer', 'selected': [
        {'index': 2, 'custom_text': 'У меня опыт SQL.'}]}, 'stop'),
        (evidence('У меня опыт SQL.'), 'stop'))
    result = asyncio.run(forms.answer_choice_with_llm(
        {'question_text': 'Какой у вас опыт?', 'control': 'radio', 'options': [
            {'index': 2, 'label': '12 лет Kubernetes', 'is_custom': False}]},
        'У меня опыт SQL.', **question_dependencies(client)))
    assert result is None or result.get('is_skip')
    assert len(client.calls) == 1


@pytest.mark.parametrize('custom_text', [None, '', {'claim': 'Invented'}])
def test_custom_option_requires_valid_text(blocks, custom_text):
    client = ScriptedResponses(({'status': 'answer', 'selected': [
        {'index': 2, 'custom_text': custom_text}]}, 'stop'))
    assert asyncio.run(forms.answer_choice_with_llm(
        {'question_text': 'Опыт?', 'control': 'radio', 'options': [
            {'index': 2, 'label': 'Другое', 'is_custom': True}]},
        'SQL', **question_dependencies(client))) is None


def test_custom_option_verification_includes_the_selected_label(blocks):
    text = 'Другое: У меня опыт SQL.'
    client = ScriptedResponses(({'status': 'answer', 'selected': [
        {'index': 2, 'custom_text': 'У меня опыт SQL.'}]}, 'stop'),
        (evidence(text, 'У меня опыт SQL.'), 'stop'))
    result = asyncio.run(forms.answer_choice_with_llm(
        {'question_text': 'Опыт?', 'control': 'radio', 'options': [
            {'index': 2, 'label': 'Другое', 'is_custom': True}]},
        'У меня опыт SQL.', **question_dependencies(client)))
    assert not result['is_skip']
    checker = json.loads(client.calls[1]['messages'][1]['content'])
    assert checker['answers'][0]['answer'] == text
