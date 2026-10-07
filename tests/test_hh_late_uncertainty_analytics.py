"""Late HH uncertainty replaces this run's prior decision, without double counts."""
import pytest

import analytics
import config


@pytest.fixture
def journal(tmp_path, monkeypatch):
    path = tmp_path / 'events.jsonl'
    monkeypatch.setattr(config, 'JOB_HUNTER_HOME', str(tmp_path))
    monkeypatch.setattr(config, 'ANALYTICS_ENABLED', True)
    monkeypatch.setattr(config, 'ANALYTICS_EVENTS_FILE', str(path))
    monkeypatch.setattr(config, 'ANALYTICS_STATE_FILE', str(tmp_path / 'analytics.json'))
    monkeypatch.setattr(config, 'RUN_HISTORY_FILE', str(tmp_path / 'history.jsonl'))
    return path


@pytest.mark.parametrize('prior', [
    'guard_stop', 'apply_failed', 'questions_required', 'already_applied', 'applied_auto',
])
def test_late_uncertainty_replaces_prior_once_and_cannot_be_downgraded(journal, prior):
    vacancy = {'id': '4', 'source': 'hh'}
    other = {'id': 'other', 'source': 'habr'}
    with analytics.observe_search('same-run', 'auto') as observation:
        analytics.record_decision(run_id='same-run', vacancy=vacancy, decision=prior)
        analytics.record_decision(run_id='same-run', vacancy=vacancy, decision='apply_uncertain')
        analytics.record_decision(run_id='same-run', vacancy=vacancy, decision='apply_uncertain')
        analytics.record_decision(run_id='same-run', vacancy=vacancy, decision='applied_auto')
        analytics.count_application(vacancy)
        analytics.record_decision(run_id='same-run', vacancy=other, decision='applied_auto')
        counters = observation.counters['hh']
        assert counters['manual'] == counters['uncertain'] == 1
        assert all(counters[name] == 0 for name in ('applied', 'failed', 'guard_stop', 'skipped'))
        assert dict(observation.reasons) == {'apply_uncertain': 1, 'applied': 1}
        assert observation.counters['habr']['applied'] == 1
    summary = analytics.summarize(all_time=True, events_file=str(journal))
    assert summary['reason_breakdown'] == {'apply_uncertain': 1, 'applied': 1}
    assert summary['by_source']['hh']['manual'] == 1
    assert summary['by_source']['hh']['auto_applied'] == 0
    assert summary['by_source']['habr']['auto_applied'] == 1


def test_uncertainty_summary_does_not_replace_a_different_runs_history(journal):
    vacancy = {'id': '4', 'source': 'hh'}
    with analytics.observe_search('older-run', 'auto'):
        analytics.record_decision(run_id='older-run', vacancy=vacancy, decision='apply_failed')
    with analytics.observe_search('newer-run', 'auto'):
        analytics.record_decision(run_id='newer-run', vacancy=vacancy, decision='apply_uncertain')
    summary = analytics.summarize(all_time=True, events_file=str(journal))
    assert summary['reason_breakdown'] == {'apply_failed': 1, 'apply_uncertain': 1}


def test_summary_keeps_uncertainty_even_if_later_journal_call_has_no_live_observation(journal):
    vacancy = {'id': '4', 'source': 'hh'}
    with analytics.observe_search('same-run', 'auto'):
        analytics.record_decision(run_id='same-run', vacancy=vacancy, decision='apply_uncertain')
    analytics.record_decision(run_id='same-run', vacancy=vacancy, decision='applied_auto')
    summary = analytics.summarize(all_time=True, events_file=str(journal))
    assert summary['reason_breakdown'] == {'apply_uncertain': 1}
    assert summary['by_source']['hh']['auto_applied'] == 0
