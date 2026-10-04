"""Native offline regressions for closing review findings; no external sends."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import agent
import hh_client
import matcher
from hh import apply as hh_apply
import seen
from tests.test_matcher_deferred_safety import search_harness, matcher_inputs
from tests.test_hh_resume_target_safety import ApplyPage


@pytest.mark.parametrize('dry_run', [True, False])
def test_blacklist_count_is_not_a_semantic_rejection_or_permanent_seen(search_harness, monkeypatch, dry_run):
    home, collected, vacancies = search_harness
    monkeypatch.setattr(agent.company_blacklist, 'is_blocked', lambda name: True)
    monkeypatch.setattr(agent, 'notify_search_started', AsyncMock())
    evaluation = AsyncMock(side_effect=AssertionError('Blocked company must not reach Matcher'))
    dispatch = AsyncMock(side_effect=AssertionError('Blocked company must not reach apply'))
    monkeypatch.setattr(agent, 'evaluate_vacancy', evaluation)
    monkeypatch.setattr(agent.apply_orchestrator, 'dispatch_apply', dispatch)
    result = asyncio.run(agent.do_search(dry_run=dry_run))
    assert result['found'] == result['skipped'] == 1
    assert result['applied'] == result['deferred'] == 0
    assert result['source_stats']['hh']['blacklisted'] == 1
    assert result['source_stats']['hh']['rejected'] == 0
    assert not seen.is_seen('hh:1')
    evaluation.assert_not_awaited()
    dispatch.assert_not_awaited()
    assert agent.notify_summary.await_args.args[:3] == (1, 0, 1)


@pytest.mark.parametrize('method', ['selector', 'dom'])
def test_ui_await_cannot_change_target_after_last_identity_check(method):
    page = ApplyPage()
    client = hh_client.HHClient()
    client._page = page
    async def inspect(stage, **kwargs):
        if stage.startswith('verified_'):
            await asyncio.sleep(0)
            page.selected = 'wrong'
        return False
    client._ensure_expected_ui = inspect
    async def approved():
        return await hh_apply.selected_resume_matches(page, 'target', 'QA')
    if method == 'selector':
        result = asyncio.run(client._click_with_fallbacks(page.submit, 'submit', before_click=approved))
    else:
        result = asyncio.run(client._submit_response_form_via_dom(before_submit=approved))
    assert result is False
    assert not page.sent
    assert page.submit.attempts == 0


@pytest.mark.parametrize('change', ['resume', 'letter', 'required_question', 'unchanged'])
def test_inconclusive_native_apply_never_repeats_submit(change, tmp_path, monkeypatch):
    monkeypatch.setattr(agent.config, "HH_COOKIES_FILE", str(tmp_path / "cookies.json"))
    page = ApplyPage()
    page.dom_submits = 0
    original_evaluate = page.evaluate
    async def evaluate(script, expected=None):
        if 'form.requestSubmit' in script:
            page.dom_submits += 1
        return await original_evaluate(script, expected)
    page.evaluate = evaluate
    state = {'changed': False, 'required': False}
    async def wait(milliseconds):
        if milliseconds == 4000 and page.sent and not state['changed']:
            state['changed'] = True
            if change == 'resume': page.selected = 'wrong'
            if change == 'letter': page.letter.value = 'Unapproved changed letter'
            if change == 'required_question': state['required'] = True
    page.wait_for_timeout = wait
    client = hh_client.HHClient()
    client._page = page
    client._save_debug_snapshot = AsyncMock()
    client._detect_anti_bot_kind = AsyncMock(return_value=None)
    client._handle_anti_bot_with_solver = AsyncMock(return_value=None)
    client._page_closed_or_archived = AsyncMock(return_value=False)
    client._has_existing_response_ui = AsyncMock(return_value=False)
    client._dismiss_magritte_dropdowns = AsyncMock()
    client._response_requires_questions = AsyncMock(return_value=False)
    async def questions():
        return {'fields': [{'required': True, 'answered': False}] if state['required'] else []}
    client._inspect_employer_questions = questions
    async def success():
        return page.dom_submits > 0
    async def controls():
        return (page.url, object(), False, None, page.letter, page.submit)
    client._apply_success_detected = success
    client._detect_response_controls = controls
    result = asyncio.run(hh_apply.apply_to_vacancy(client, 'https://hh.ru/vacancy/1', 'Approved letter',
        preferred_resume_id='target', preferred_resume_title='QA',
        absolute_hh_url=lambda value: value, anti_bot_message=lambda *args: '', logger=hh_client.log))
    assert page.submit.attempts == 1  # First attempt; never a real network send.
    assert page.dom_submits == 0
    assert not result['ok'] and result['uncertain']


def evaluation_client(payload, finish_reason='stop'):
    response = SimpleNamespace(choices=[SimpleNamespace(finish_reason=finish_reason,
        message=SimpleNamespace(content=json.dumps(payload)))])
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(return_value=response))))


@pytest.mark.parametrize('payload', [
    {}, {'score': None}, {'score': True}, {'score': False}, {'score': 'not a score'},
    {'score': float('nan')}, {'score': float('inf')}, {'score': []},
    {'score': 'NaN/100'}, {'score': 'Infinity/100'}, {'score': '1e309'},
    {'score': 90, 'should_apply': 'false'}, {'score': 90, 'reason': []},
    {'score': 90, 'red_flags': 'bad'}, {'score': 90, 'red_flags': [None]},
])
def test_malformed_evaluation_schema_is_deferred(matcher_inputs, monkeypatch, payload):
    client = evaluation_client(payload)
    monkeypatch.setattr(matcher, '_get_client', lambda: client)
    result = asyncio.run(matcher.evaluate_vacancy(matcher_inputs))
    assert result['score'] is None
    assert result['error_kind'] == 'llm_error'
    assert result['evaluation_status'] == 'deferred_unscored'
    assert result['should_apply'] is False
    assert client.chat.completions.create.await_count == 1


@pytest.mark.parametrize('finish_reason', ['length', 'content_filter', None])
def test_incomplete_primary_evaluation_is_deferred(matcher_inputs, monkeypatch, finish_reason):
    client = evaluation_client({'score': 90, 'should_apply': True}, finish_reason)
    monkeypatch.setattr(matcher, '_get_client', lambda: client)
    result = asyncio.run(matcher.evaluate_vacancy(matcher_inputs))
    assert result['score'] is None and result['evaluation_status'] == 'deferred_unscored'
    assert client.chat.completions.create.await_count == 1


@pytest.mark.parametrize('score,expected', [(85.5, 85), ('85/100', 85), ('score: 72,5', 72)])
def test_valid_fractional_and_text_scores_remain_compatible(matcher_inputs, monkeypatch, score, expected):
    client = evaluation_client({'score': score, 'should_apply': True})
    monkeypatch.setattr(matcher, '_get_client', lambda: client)
    result = asyncio.run(matcher.evaluate_vacancy(matcher_inputs))
    assert result['score'] == expected and 'error_kind' not in result


def test_incomplete_evaluation_retry_is_not_accepted(monkeypatch):
    client = evaluation_client({'score': 90}, 'length')
    monkeypatch.setattr(matcher, '_repair_llm_json', AsyncMock(side_effect=ValueError('Invalid repair')))
    with pytest.raises(ValueError, match='Incomplete LLM evaluation'):
        asyncio.run(matcher._parse_evaluation_json_with_repair(client, 'not JSON', 'Synthetic prompt', 'synthetic'))
    assert client.chat.completions.create.await_count == 1


def test_malformed_real_matcher_does_not_permanently_reject_vacancy(search_harness, matcher_inputs, monkeypatch):
    home, _, _ = search_harness
    client = evaluation_client({})
    monkeypatch.setattr(matcher, '_get_client', lambda: client)
    monkeypatch.setattr(agent, 'evaluate_vacancy', matcher.evaluate_vacancy)
    result = asyncio.run(agent.do_search(dry_run=True))
    assert result['deferred'] == 1 and result['skipped'] == result['applied'] == 0
    assert result['source_stats']['hh']['rejected'] == 0
    assert not seen.is_seen('hh:1')
    assert len(json.loads((home / 'matcher_deferred.json').read_text())['items']) == 1
