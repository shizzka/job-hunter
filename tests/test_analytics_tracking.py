import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import analytics
import apply_orchestrator
import config
import manual_apply_queue


@pytest.fixture
def events(tmp_path, monkeypatch):
    path = tmp_path / 'events.jsonl'
    monkeypatch.setattr(config, 'ANALYTICS_ENABLED', True)
    monkeypatch.setattr(config, 'ANALYTICS_EVENTS_FILE', str(path))
    monkeypatch.setattr(config, 'JOB_HUNTER_HOME', str(tmp_path))
    return lambda: [json.loads(line) for line in path.read_text().splitlines()]


def test_parallel_calls_do_not_mix_context(events):
    async def emit():
        await asyncio.sleep(0)
        analytics.record_llm_call('test', 'model', SimpleNamespace(usage=SimpleNamespace(prompt_tokens=12, completion_tokens=3, total_tokens=15)))
    async def run():
        await asyncio.gather(*(analytics.tracked_call('matcher', 'run', {'id': str(i), 'source': 'hh'}, emit) for i in range(2)))
    asyncio.run(run())
    rows = events()
    assert {row['vacancy_id'] for row in rows} == {'0', '1'}
    assert all(row['input_tokens'] == 12 and row['cost_usd'] is None for row in rows)
    assert all(row['recorded_at_utc'].endswith('+00:00') and row['profile_id'] for row in rows)
    assert analytics._event_context.get() == {}


def test_failed_call_preserves_unknown_usage_and_resets_context(events):
    async def fail():
        analytics.record_llm_call('test', 'model', error_kind='TimeoutError')
        raise TimeoutError()
    with pytest.raises(TimeoutError):
        asyncio.run(analytics.tracked_call('matcher', 'run', {'id': '1'}, fail))
    assert events()[0]['input_tokens'] is None
    assert events()[0]['cost_usd'] is None
    assert analytics._event_context.get() == {}


@pytest.mark.parametrize('result, outcome', [({'ok': True}, 'sent'), ({'ok': True, 'already_applied': True}, 'already_applied'), ({'ok': False}, 'failed')])
def test_application_attempt_and_result_share_identity(events, monkeypatch, result, outcome):
    monkeypatch.setattr(apply_orchestrator, '_dispatch_apply', AsyncMock(return_value=result))
    asyncio.run(apply_orchestrator.dispatch_apply({'id': '1', '_analytics_run_id': 'run', '_analytics_apply_mode': 'manual'}, 'letter'))
    first, last = [row for row in events() if row['event'] in {'application_attempt', 'application_result'}]
    assert first['application_id'] == last['application_id']
    assert first['event'] == 'application_attempt'
    assert last['outcome'] == outcome
    assert last['apply_mode'] == 'manual'


def test_blacklist_action_remains_after_feedback_for_other_sources():
    markup = manual_apply_queue.build_manual_apply_markup({'company': 'Acme', 'source': 'habr'}, 'qa', 'token', include_feedback=False, allow_ai_apply=False)
    assert any(button.get('callback_data', '').startswith('manual_block_company:') for row in markup['inline_keyboard'] for button in row)
