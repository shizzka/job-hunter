"""Requirements after long company introductions must reach every LLM stage."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import matcher
import prompt_blocks
import relevance_verifier
import apply_orchestrator

DETAILS = 'О компании и продукте.\n' * 500 + '\nТребования: продвинутый Linux; анализ stack trace.\nБудет плюсом: Docker.'


class Client:
    def __init__(self, answer):
        self.create = AsyncMock(return_value=SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=answer))]))
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self.create))


def test_matcher_receives_complete_description(monkeypatch):
    client = Client('{"score":15,"should_apply":false,"reason":"Нет Linux","red_flags":[]}')
    monkeypatch.setattr(matcher, '_get_client', lambda: client)
    monkeypatch.setattr(matcher, '_load_resume', lambda: 'Junior Manual QA')
    monkeypatch.setattr(matcher, '_build_matcher_truth_block', lambda: '')
    asyncio.run(matcher.evaluate_vacancy({'title':'QA Engineer'}, DETAILS))
    assert DETAILS in client.create.call_args.kwargs['messages'][0]['content']


def test_letter_and_knowledge_selector_receive_complete_description(monkeypatch):
    client = Client('Проверяю API. Веду тест-кейсы. Обсудим задачи.')
    monkeypatch.setattr(matcher, '_get_client', lambda: client)
    monkeypatch.setattr(matcher, '_load_resume', lambda: 'Junior Manual QA')
    selector = AsyncMock(return_value='')
    monkeypatch.setattr(prompt_blocks, 'build_filtered_kb_block', selector)
    asyncio.run(matcher.generate_cover_letter({'title':'QA Engineer'}, DETAILS))
    assert DETAILS in selector.call_args.args[0]
    assert DETAILS in client.create.call_args.kwargs['messages'][0]['content']


def test_kb_selection_does_not_truncate_internally():
    client = Client('{"selected":[1]}')
    result = asyncio.run(prompt_blocks.select_kb_sections(DETAILS, [{'num':1,'title':'Linux'}], client))
    assert result == [1]
    assert DETAILS in client.create.call_args.kwargs['messages'][1]['content']


def test_verifier_receives_complete_description(monkeypatch):
    client = Client('{"verdict":"review","reason":"Linux не подтверждён"}')
    monkeypatch.setattr(relevance_verifier, 'get_llm_client', lambda: client)
    monkeypatch.setattr(matcher, '_load_resume', lambda: 'Manual QA')
    monkeypatch.setattr(relevance_verifier.config, 'HH_VERIFIER_SHADOW_ENABLED', True)
    asyncio.run(relevance_verifier.ShadowVerifier().check('test',{'id':'1'},DETAILS,{'score':80}))
    payload = json.loads(client.create.call_args.kwargs['messages'][1]['content'])
    assert payload['description'] == DETAILS


def test_hh_dispatch_preserves_complete_questionnaire_context(monkeypatch):
    monkeypatch.setattr(apply_orchestrator.company_blacklist, 'is_blocked', lambda *a: False)
    client = SimpleNamespace(apply_to_vacancy=AsyncMock(return_value={'ok':True}))
    asyncio.run(apply_orchestrator._dispatch_apply({'source':'hh','url':'https://hh.ru/vacancy/1','details':DETAILS}, 'letter', hh_client=client))
    assert DETAILS in client.apply_to_vacancy.call_args.kwargs['vacancy_context']


def test_employer_answer_prompts_preserve_requirements(monkeypatch):
    import config
    import logging
    from hh import forms
    monkeypatch.setattr(config, 'HH_AUTO_ANSWER_USE_LLM', True)
    monkeypatch.setattr(config, 'LLM_API_KEY', 'test-only')
    for function, field, response in [
        (forms.answer_question_with_llm, {'question_text':'Как оцениваете задачи этой вакансии?'}, '{"status":"answer","answer":"Нужно обсудить Linux."}'),
        (forms.answer_choice_with_llm, {'question_text':'Какой формат задач подходит?', 'control':'radio', 'options':[{'index':0,'text':'Обсудить требования'}]}, '{"status":"answer","selected":[{"index":0,"custom_text":null}]}'),
    ]:
        client = Client(response)
        asyncio.run(function(field, 'Manual QA', vacancy_context=DETAILS,
            settings=config, logger=logging.getLogger('test'),
            get_question_answer_client=lambda: client,
            build_salary_rule_block=lambda: '', build_facts_block=lambda: '',
            build_profile_note_block=lambda: '', build_filtered_kb_block=AsyncMock(return_value=''),
            build_knowledge_base_block=lambda **kw: '', parse_llm_json=json.loads,
            repair_llm_json=AsyncMock()))
        assert DETAILS in client.create.call_args.kwargs['messages'][1]['content']
