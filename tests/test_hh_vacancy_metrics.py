"""Passive metrics must not change details or application outcomes."""
import asyncio
import json
from pathlib import Path

import pytest

import config
from hh import vacancy_metrics as metrics
from tests.test_hh_negotiation_snapshot import html


@pytest.mark.parametrize('present', [True, False])
def test_metrics_numeric_allowlist_and_nulls(present):
    state = {'vacancyView': {'vacancyFull': {'extraVacancyFields': {
        'totalResponsesCount': 7, 'vacancyResponsesCount': 7, 'letter': 'PRIVATE'}}},
        'vacancyOnlineUsersCounters': {'42': {'includingCurrent': 3, 'excludingCurrent': 2,
                                             'name': 'PRIVATE'}}} if present else {}
    metrics.record(html(state), '42', 'apply_before')
    metrics.record(html(state), '42', 'details')
    path = Path(config.JOB_HUNTER_HOME) / 'vacancy_metrics.jsonl'
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert path.stat().st_mode & 0o777 == 0o600
    assert len(rows) == 2
    for row, stage in zip(rows, ['apply_before', 'details']):
        assert set(row) == {'ts', 'vacancy_id', 'stage', 'total_responses', 'vacancy_responses',
                            'online_including_current', 'online_excluding_current'}
        assert row['stage'] == stage and row['vacancy_id'] == 42
        assert [row[key] for key in ('total_responses', 'vacancy_responses',
                                    'online_including_current', 'online_excluding_current')] == (
            [7, 7, 3, 2] if present else [None] * 4)
    assert 'PRIVATE' not in path.read_text()


def fail(*args, **kwargs):
    raise OSError('synthetic journal failure')


def test_metrics_write_failure_never_escapes(monkeypatch):
    monkeypatch.setattr(metrics, 'append_records', fail)
    assert metrics.record(html({}), '42', 'details') is None


def test_apply_records_metrics_from_existing_markup(tmp_path, monkeypatch):
    from tests import test_e2e_hh_apply_happy_path as harness
    state = {'vacancyView': {'vacancyFull': {'extraVacancyFields': {
        'totalResponsesCount': 7, 'vacancyResponsesCount': 7}}},
        'vacancyOnlineUsersCounters': {'2': {'includingCurrent': 3, 'excludingCurrent': 2}}}
    monkeypatch.setattr(harness, 'VACANCY_HTML', harness.VACANCY_HTML.replace(
        '</body>', html(state) + '</body>'))
    gate = asyncio.run(harness.run_hh_gate(tmp_path, monkeypatch))
    assert gate.result['applied'] == 1 and len(gate.application_posts) == 1
    rows = [json.loads(line) for line in (Path(config.JOB_HUNTER_HOME) / 'vacancy_metrics.jsonl').read_text().splitlines()]
    assert len(rows) == 1
    assert rows[0]['stage'] == 'apply_before' and rows[0]['vacancy_id'] == 2
    assert rows[0]['total_responses'] == rows[0]['vacancy_responses'] == 7
    assert rows[0]['online_including_current'] == 3 and rows[0]['online_excluding_current'] == 2
    assert len([call for call in gate.fresh_gets if call['url'] == harness.VACANCY_URL]) == 1


@pytest.mark.parametrize('failure', ['writer', 'record'])
def test_apply_outcome_unchanged_on_metrics_exception(tmp_path, monkeypatch, failure):
    from tests.test_e2e_hh_apply_happy_path import run_hh_gate
    monkeypatch.setattr(metrics, 'append_records' if failure == 'writer' else 'record', fail)
    gate = asyncio.run(run_hh_gate(tmp_path, monkeypatch))
    assert gate.result['applied'] == 1 and len(gate.application_posts) == 1
    assert gate.entries['2']['action'] == 'applied'
    assert gate.dispatch_results[0]['uncertain'] is False
    assert gate.result['hh_recovery']['stopped'] is False


@pytest.mark.parametrize('failure', ['', 'writer', 'record'])
def test_details_outcome_and_get_count_unchanged(tmp_path, monkeypatch, failure):
    from tests.test_hh_vacancy_details import browser_scenario, HTML, URL
    if failure:
        monkeypatch.setattr(metrics, 'append_records' if failure == 'writer' else 'record', fail)
    state = {'vacancyView': {'vacancyFull': {'extraVacancyFields': {
        'totalResponsesCount': 7, 'vacancyResponsesCount': 7}}}}
    async def check(client, requests, frames, fetched):
        assert await client.get_vacancy_details(URL) == 'Test browser lifecycle and Python services.'
        assert len(fetched) == 1 and not frames
        assert all(method == 'GET' for method, _ in requests)
        assert not client._page._hh_action_monitor.unknown
        if not failure:
            row = json.loads((Path(config.JOB_HUNTER_HOME) / 'vacancy_metrics.jsonl').read_text())
            assert row['stage'] == 'details' and row['vacancy_id'] == 123
            assert row['total_responses'] == row['vacancy_responses'] == 7
    asyncio.run(browser_scenario(tmp_path, monkeypatch, check, HTML.replace('</body>', html(state) + '</body>')))
