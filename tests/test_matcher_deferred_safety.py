"""Quota/transport failures cannot reject or permanently lose a vacancy."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import agent
import analytics
import config
import llm_client
import matcher
import notifier
import seen


class RateLimitError(Exception):
    status_code = 429


@pytest.fixture
def matcher_inputs(monkeypatch):
    monkeypatch.setattr(matcher, '_load_resume', lambda: 'Synthetic candidate')
    monkeypatch.setattr(matcher, '_build_matcher_truth_block', lambda: '')
    monkeypatch.setattr(config, 'VACANCY_FILTER_POLICY', 'generic')
    monkeypatch.setattr(llm_client, '_record_usage', lambda *args, **kwargs: None)
    return {'id': 'hh:1', 'source': 'hh', 'title': 'Synthetic vacancy', 'company': 'Synthetic employer'}


@pytest.mark.parametrize('failure', [RateLimitError('quota'), TimeoutError('private payload'), RuntimeError('private payload')])
def test_evaluation_failure_returns_unscored_not_zero(matcher_inputs, monkeypatch, failure):
    client = SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=AsyncMock(side_effect=failure))))
    monkeypatch.setattr(matcher, '_get_client', lambda: client)
    result = asyncio.run(matcher.evaluate_vacancy(matcher_inputs))
    assert result['score'] is None
    assert result['evaluation_status'] == 'deferred_unscored'
    assert not result['should_apply']
    assert 'response_probability_score' not in result
    assert 'private payload' not in json.dumps(result)


@pytest.mark.parametrize('outcomes,expected_score', [
    ([429, 429], None), ([429, 80], 80), ([503, 80], 80),
])
def test_actual_provider_fallback_then_exhaustion_or_success(matcher_inputs, monkeypatch, outcomes, expected_score):
    calls = []
    response = SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=json.dumps({
        'score': 80, 'reason': 'Synthetic semantic match', 'should_apply': True, 'red_flags': []})))])

    class ProviderError(Exception):
        def __init__(self, status):
            self.status_code = status

    def provider(index):
        async def create(**kwargs):
            calls.append(index)
            if outcomes[index] in (429, 503):
                raise ProviderError(outcomes[index])
            return response
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    client = llm_client.FallbackLLMClient([
        llm_client.ProviderSpec('one', 'https://provider.test/v1', 'synthetic-one'),
        llm_client.ProviderSpec('two', 'https://provider.test/v1', 'synthetic-two'),
    ])
    monkeypatch.setattr(client, '_client_for', provider)
    monkeypatch.setattr(matcher, '_get_client', lambda: client)
    result = asyncio.run(matcher.evaluate_vacancy(matcher_inputs))
    assert calls == [0, 1]
    assert result['score'] == expected_score
    if expected_score is None:
        assert result['evaluation_status'] == 'deferred_unscored'
        assert result['error_kind'] == 'llm_limits_exhausted'
        assert result['llm_providers'] == ['one', 'two']
    else:
        assert 'error_kind' not in result


@pytest.fixture
def search_harness(tmp_path, monkeypatch):
    for key, filename in (
        ('JOB_HUNTER_HOME', ''), ('SEEN_VACANCIES_FILE', 'seen.json'),
        ('RUNTIME_STATUS_FILE', 'runtime.json'), ('RUN_HISTORY_FILE', 'history.jsonl'),
        ('HH_RESUME_PIPELINE_FILE', 'pipeline.json'), ('HH_GUARD_STATE_FILE', 'guard.json'),
    ):
        monkeypatch.setattr(config, key, str(tmp_path / filename))
    monkeypatch.setattr(config, 'VACANCY_FILTER_POLICY', 'generic')
    monkeypatch.setattr(config, 'HH_ENABLED', True)
    monkeypatch.setattr(config, 'SUPERJOB_ENABLED', False)
    monkeypatch.setattr(config, 'HABR_ENABLED', False)
    monkeypatch.setattr(config, 'GEEKJOB_ENABLED', False)
    monkeypatch.setattr(config, 'HH_RESUME_PIPELINE_ENABLED', False)
    monkeypatch.setattr(agent, 'HHClient', lambda: SimpleNamespace(
        start=AsyncMock(), stop=AsyncMock(), is_logged_in=AsyncMock(return_value=False)))
    monkeypatch.setattr(agent.notifier, 'notify_stale_cookies', AsyncMock())
    monkeypatch.setattr(agent.notifier, 'notify_llm_issue', AsyncMock())
    monkeypatch.setattr(agent, 'office_log', AsyncMock())
    monkeypatch.setattr(agent, 'notify_summary', AsyncMock())
    monkeypatch.setattr(agent.hh_guard, 'can_auto_apply', lambda: (True, ''))
    monkeypatch.setattr(agent.company_blacklist, 'is_blocked', lambda name: False)
    monkeypatch.setattr(agent, 'ShadowVerifier', lambda: SimpleNamespace(check=AsyncMock()))
    monkeypatch.setattr(agent.apply_orchestrator, 'fetch_vacancy_details', AsyncMock(return_value='Synthetic details'))
    vacancies = [{'id': 'hh:1', 'source': 'hh', 'title': 'Synthetic vacancy', 'company': 'Synthetic employer',
                  'url': 'https://hh.ru/vacancy/1'}]
    collected = [vacancies]

    async def collect(*args, **kwargs):
        return [dict(v) for v in collected[0]]

    monkeypatch.setattr(agent.search_pipeline, 'collect_all', collect)
    return tmp_path, collected, vacancies


@pytest.mark.parametrize('kind', ['llm_limits_exhausted', 'llm_error'])
def test_full_search_defers_without_seen_rejection_and_recovers_from_queue(search_harness, monkeypatch, kind):
    home, collected, vacancies = search_harness
    evaluate = AsyncMock(return_value={'score': None, 'error_kind': kind, 'should_apply': False,
                                     'reason': 'Transient provider failure', 'red_flags': []})
    monkeypatch.setattr(agent, 'evaluate_vacancy', evaluate)
    first = asyncio.run(agent.do_search(dry_run=True))
    assert first['deferred'] == 1
    assert first['applied'] == first['skipped'] == 0
    assert first['source_stats']['hh']['rejected'] == 0
    assert not seen.is_seen('hh:1')
    queue_path = home / 'matcher_deferred.json'
    state = json.loads(queue_path.read_text())
    assert len(state['items']) == 1
    assert queue_path.stat().st_mode & 0o777 == 0o600
    summary = analytics.summarize()
    assert summary['low_score'] == summary['red_flagged'] == summary['auto_applied'] == 0
    assert summary['deferred_unscored'] == 1
    # Discovery no longer returns it; persisted queue must still retry later.
    collected[0] = []
    evaluate.reset_mock()
    second = asyncio.run(agent.do_search(dry_run=True))
    evaluate.assert_not_awaited()
    assert not seen.is_seen('hh:1')
    entry = next(iter(state['items'].values()))
    entry['next_attempt_at'] = 0
    queue_path.write_text(json.dumps(state))
    evaluate.return_value = {'score': 80, 'reason': 'Recovered semantic match', 'red_flags': [], 'should_apply': True}
    recovered = asyncio.run(agent.do_search(dry_run=True))
    evaluate.assert_awaited_once()
    assert recovered['applied'] == 1  # dry-run match, not a real submit
    assert json.loads(queue_path.read_text())['items'] == {}
    assert seen.is_seen('hh:1')


def test_multiple_unscored_vacancies_send_one_alert_per_run(search_harness, monkeypatch):
    _, collected, vacancies = search_harness
    collected[0] = [dict(vacancies[0], id=f'hh:{i}', title=f'Synthetic vacancy {i}') for i in range(4)]
    monkeypatch.setattr(agent, 'evaluate_vacancy', AsyncMock(return_value={
        'score': 0, 'error_kind': 'llm_limits_exhausted', 'should_apply': False, 'red_flags': []}))
    result = asyncio.run(agent.do_search(dry_run=True))
    assert result['deferred'] == 4
    assert result['skipped'] == 0
    agent.notifier.notify_llm_issue.assert_awaited_once()


def test_notification_does_not_describe_transient_error_as_bad_vacancy(monkeypatch):
    send = AsyncMock()
    monkeypatch.setattr(notifier, 'send_message', send)
    asyncio.run(notifier.notify_llm_issue({'title': 'Synthetic'}, {'error_kind': 'llm_limits_exhausted'}))
    text = send.await_args.args[0]
    assert 'Оценка отложена' in text
    assert 'score=0' not in text and 'score 0' not in text
    assert 'повторной оценки' in text


@pytest.mark.parametrize('failure', ['shadow_error', 'cancelled', 'hh_guard'])
def test_recovered_score_alone_cannot_discard_unhandled_vacancy(search_harness, monkeypatch, failure):
    from state_store.matcher_deferred import MatcherDeferredQueue
    home, collected, vacancies = search_harness
    queue = MatcherDeferredQueue(home, clock=lambda: 1)
    queue.defer(vacancies[0], 'Synthetic details', 'llm_error')
    collected[0] = []
    monkeypatch.setattr(agent, 'evaluate_vacancy', AsyncMock(return_value={
        'score': 80, 'reason': 'Recovered semantic match', 'red_flags': [], 'should_apply': True}))
    if failure == 'hh_guard':
        monkeypatch.setattr(agent.hh_guard, 'can_auto_apply', lambda: (False, 'Synthetic pause'))
    else:
        error = asyncio.CancelledError() if failure == 'cancelled' else RuntimeError('Synthetic downstream failure')
        monkeypatch.setattr(agent, 'ShadowVerifier', lambda: SimpleNamespace(check=AsyncMock(side_effect=error)))
    if failure == 'cancelled':
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(agent.do_search())
    else:
        asyncio.run(agent.do_search())
    assert not seen.is_seen('hh:1')
    assert len(queue.store.load()['items']) == 1


def test_already_processed_deferred_record_is_acknowledged_without_reevaluation(search_harness, monkeypatch):
    from state_store.matcher_deferred import MatcherDeferredQueue
    home, collected, vacancies = search_harness
    queue = MatcherDeferredQueue(home, clock=lambda: 1)
    queue.defer(vacancies[0], 'Synthetic details', 'llm_error')
    seen.mark_seen('hh:1', vacancies[0], 'skipped_low_score')
    collected[0] = []
    evaluate = AsyncMock()
    monkeypatch.setattr(agent, 'evaluate_vacancy', evaluate)
    asyncio.run(agent.do_search(dry_run=True))
    evaluate.assert_not_awaited()
    assert queue.store.load()['items'] == {}
