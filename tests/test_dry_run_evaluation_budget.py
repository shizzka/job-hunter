"""B3: a dry run reuses remembered evaluations and stops spending the LLM quota at a cap;
a real search always evaluates afresh."""
import asyncio
from unittest.mock import AsyncMock

import agent
import evaluation_cache
from tests.test_matcher_deferred_safety import search_harness  # noqa: F401 (fixture)


def test_dry_run_cap_cache_and_real_search_stays_fresh(search_harness, monkeypatch):
    home, collected, vacancies = search_harness
    collected[0] = [{'id': f'hh:{i}', 'source': 'hh', 'title': f'Synthetic {i}', 'company': 'Synthetic employer',
                     'url': f'https://hh.ru/vacancy/{i}'} for i in (1, 2, 3)]
    monkeypatch.setenv('DRY_RUN_MAX_LLM_EVALUATIONS', '2')
    evaluate = AsyncMock(return_value={'score': 30, 'reason': 'low', 'red_flags': [], 'should_apply': False})
    monkeypatch.setattr(agent, 'evaluate_vacancy', evaluate)

    first = asyncio.run(agent.do_search(dry_run=True))
    assert evaluate.await_count == 2
    assert first['source_stats']['hh']['dry_run_not_evaluated'] == 1
    assert 'потолок' in first['note']

    evaluate.reset_mock()
    second = asyncio.run(agent.do_search(dry_run=True))
    assert evaluate.await_count == 1  # two from memory, the third is new
    assert second['source_stats']['hh']['dry_run_cached'] == 2

    evaluate.reset_mock()
    asyncio.run(agent.do_search(dry_run=False))
    assert evaluate.await_count == 3  # a real search never trusts the cache


def test_cache_skips_deferred_and_expires(tmp_path, monkeypatch):
    monkeypatch.setattr(evaluation_cache.config, 'JOB_HUNTER_HOME', str(tmp_path))
    vacancy = {'id': '9', 'source': 'hh'}
    evaluation_cache.store(vacancy, {'score': None, 'error_kind': 'llm_error'})
    assert evaluation_cache.lookup(vacancy) is None
    evaluation_cache.store(vacancy, {'score': 70, 'reason': 'ok'})
    assert evaluation_cache.lookup(vacancy)['score'] == 70
    monkeypatch.setattr(evaluation_cache.time, 'time', lambda: 10 ** 12)
    assert evaluation_cache.lookup(vacancy) is None
