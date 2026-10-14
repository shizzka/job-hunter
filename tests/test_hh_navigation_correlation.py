"""Navigation source stops retain a single cause; no browser or network actions."""
import asyncio
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import agent
import config
from run_summary import build_run_summary
from state_store.private_journal import read_json_records
from tests.test_observability import configure_synthetic_search


@pytest.mark.parametrize('navigation,uncertain', [(True, False), (False, False), (True, True)])
def test_navigation_stop_correlates_guard_and_only_hh_downstream(tmp_path, monkeypatch, navigation, uncertain):
    def deny_network(*args, **kwargs):
        raise AssertionError('Navigation correlation regression must remain offline')
    monkeypatch.setattr(socket.socket, 'connect', deny_network)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny_network)
    for key, value in {
        'JOB_HUNTER_HOME': str(tmp_path), 'SEEN_VACANCIES_FILE': str(tmp_path / 'seen.json'),
        'RUN_HISTORY_FILE': str(tmp_path / 'runs.jsonl'), 'RUNTIME_STATUS_FILE': str(tmp_path / 'runtime.json'),
        'ANALYTICS_EVENTS_FILE': str(tmp_path / 'events.jsonl'),
        'ANALYTICS_STATE_FILE': str(tmp_path / 'analytics.json'), 'HH_STATE_DIR': str(tmp_path / 'hh'),
        'HH_GUARD_STATE_FILE': str(tmp_path / 'guard.json'),
    }.items():
        monkeypatch.setattr(config, key, value)
    monkeypatch.setattr(config, 'ANALYTICS_ENABLED', True)
    configure_synthetic_search(monkeypatch, tmp_path)
    monkeypatch.setattr(config, 'HABR_ENABLED', True)
    hh = SimpleNamespace(start=AsyncMock(), stop=AsyncMock(), hard_stop_browser=AsyncMock(return_value=True),
                         _page=None, is_logged_in=AsyncMock(return_value=True),
                         get_negotiation_statuses=AsyncMock(return_value=[]))
    monkeypatch.setattr(agent, 'HHClient', lambda: hh)
    habr = SimpleNamespace(stop=AsyncMock(), stop_browser=AsyncMock(), _page=None,
                           is_logged_in=AsyncMock(return_value=True))
    monkeypatch.setattr(agent, 'HabrCareerClient', lambda: habr)
    vacancies = [{'id': str(vid), 'source': source, 'title': f'QA {vid}', 'company': 'Synthetic',
                  'url': f'https://hh.ru/vacancy/{vid}'} for vid, source in [(4, 'hh'), (5, 'hh'), (6, 'habr')]]
    monkeypatch.setattr(agent.search_pipeline, 'collect_all', AsyncMock(return_value=vacancies))
    dispatches = []
    async def dispatch(vacancy, *args, **kwargs):
        dispatches.append(vacancy['id'])
        if vacancy['source'] != 'hh':
            return {'ok': True}
        result = {'ok': False, 'hh_hard_stop': True, 'uncertain': uncertain,
                  'hh_recovery_reason': 'vacancy_navigation_failed' if navigation else 'auth_origin_unproven'}
        if navigation:
            result['hh_navigation_failure'] = {'stage': 'VACANCY_NAVIGATION',
                'reason_code': 'vacancy_navigation_failed', 'status': 403,
                'response_origin': 'https://spb.hh.ru'}
        return result
    monkeypatch.setattr(agent.apply_orchestrator, 'dispatch_apply', dispatch)
    result = asyncio.run(agent.do_search())
    events = read_json_records(config.ANALYTICS_EVENTS_FILE)
    run = read_json_records(config.RUN_HISTORY_FILE)[-1]
    summary = build_run_summary(run, events)
    decisions = [e for e in events if e.get('event') == 'decision']
    assert dispatches == ['4', '6']
    assert result['hh_recovery']['stopped']
    if navigation and not uncertain:
        root = summary['primary_failure']
        assert root['reason_code'] == 'vacancy_navigation_failed'
        assert root['stage'] == 'VACANCY_NAVIGATION' and root['source'] == 'hh'
        assert root['failure_id'] and root['retryable'] is False
        assert root['affected_count'] == 2
        assert root['downstream_events'] == {'guard_stop': 1, 'not_processed': 1}
        hh_decisions = [e for e in decisions if e['source'] == 'hh']
        assert all(e.get('failure_id') == root['failure_id'] for e in hh_decisions)
        assert all(e.get('failure_id') != root['failure_id'] for e in decisions if e['source'] != 'hh')
    elif uncertain:
        assert summary['primary_failure']['reason_code'] == 'apply_uncertain'
        assert not any(e.get('reason_code') == 'vacancy_navigation_failed' for e in events)
    else:
        assert not any(e.get('reason_code') == 'vacancy_navigation_failed' for e in events)


def test_legacy_navigation_guard_uses_explicit_failed_trace_stage():
    run_id = 'legacy-navigation'
    events = [
        {'event': 'stage_failed', 'run_id': run_id, 'failure_id': 'collection-error',
         'stage': 'collection', 'source': 'hh', 'continued': True, 'reason_code': 'UNKNOWN_ROOT_CAUSE'},
        {'event': 'debug_artifact', 'run_id': run_id, 'vacancy_id': '137127878', 'source': 'hh',
         'stage': 'VACANCY_NAVIGATION', 'outcome': 'error', 'artifact_type': 'apply_trace'},
        {'event': 'hh_ui_isolation', 'run_id': run_id, 'vacancy_id': '137127878', 'source': 'hh',
         'recovered': False, 'reason': 'auth_origin_unproven'},
        {'event': 'decision', 'run_id': run_id, 'vacancy_id': '137127878', 'source': 'hh',
         'decision': 'guard_stop', 'note': 'hh:ui_hard_stop'},
    ]
    events += [{'event': 'decision', 'run_id': run_id, 'vacancy_id': str(i), 'source': 'hh',
                'decision': 'not_processed', 'outcome': 'not_processed', 'note': 'hh_recovery_breaker'}
               for i in range(104)]
    run = {'run_id': run_id, 'ok': True, 'finished_at': '2026-10-11T12:45:50+00:00',
           'funnel': {'not_processed': 104, 'evaluated': 1}}
    summary = build_run_summary(run, events)
    root = summary['primary_failure']
    assert root['reason_code'] == 'vacancy_navigation_failed'
    assert root['stage'] == 'VACANCY_NAVIGATION' and root['source'] == 'hh'
    assert root['downstream_events'] == {'guard_stop': 1, 'not_processed': 104}
    assert root['affected_count'] == 105
    assert summary == build_run_summary(run, events)
    assert all('failure_id' not in e for e in events[1:])
    # A URL/name in arbitrary text and a generic guard cannot invent a navigation failure.
    unrelated = [e for e in events if e.get('event') != 'debug_artifact']
    assert build_run_summary(run, unrelated)['primary_failure']['reason_code'] != 'vacancy_navigation_failed'
    newer_artifact = {**events[1], 'stage': 'SUBMIT'}
    later_failure = events[:2] + [newer_artifact] + events[2:]
    assert build_run_summary(run, later_failure)['primary_failure']['reason_code'] != 'vacancy_navigation_failed'
    previous_decision = {**events[3], 'decision': 'apply_failed'}
    separate_attempts = events[:2] + [previous_decision] + events[2:]
    assert build_run_summary(run, separate_attempts)['primary_failure']['reason_code'] != 'vacancy_navigation_failed'
