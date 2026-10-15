"""Offline B3-2 gates: outcome uncertainty must not imply session corruption."""
import asyncio

import pytest

from tests.test_e2e_hh_apply_happy_path import run_hh_gate
from tests.test_hh_post_submit_verification import APPLIED_HTML, POPUP_URL, VACANCY_URL

UNCONFIRMED_HTML = '<main><h1 data-qa="vacancy-title">QA</h1><div role="alert">Спасибо!</div></main>'


def test_server_render_race_has_one_get_retry_and_one_submit(tmp_path, monkeypatch):
    gate = asyncio.run(run_hh_gate(tmp_path, monkeypatch, noise=False, stale_receipt=True,
                                  continue_after_apply=True,
                                  confirmation_html=[UNCONFIRMED_HTML, APPLIED_HTML]))
    assert gate.application_posts == [('POST', POPUP_URL)], gate.evidence
    failure = next(event for event in gate.trace._events if event['stage'] == 'APPLY_EXCEPTION')
    assert failure['previous_stage'] == 'PRE_SUBMIT_VERIFY'
    assert failure['application_posts'] == [{'owned': True, 'status': 200}]
    events = [event for event in gate.trace._events if event['stage'] == 'POST_SUBMIT_RECONCILIATION']
    assert [(event['step'], event.get('read_number')) for event in events] == [
        ('applied_server_html_false', 1), ('verified', 2)]
    assert len([call for call in gate.fresh_gets if call['url'] == VACANCY_URL]) == 2
    assert gate.entries['2']['action'] == 'applied'
    assert gate.dispatch_results[0]['submission_status'] == 'success_confirmed'
    assert gate.entries['3']['action'] == 'skipped_low_score'
    assert not gate.result['hh_recovery']['stopped'] and gate.evidence['hard_stops'] == 0


def test_real_uncertainty_notifies_continues_and_never_retries_across_runs(tmp_path, monkeypatch):
    gate = asyncio.run(run_hh_gate(tmp_path, monkeypatch, noise=False, stale_receipt=True,
                                  continue_after_apply=True, repeat_run=True,
                                  confirmation_html=UNCONFIRMED_HTML))
    assert gate.application_posts == [('POST', POPUP_URL)], gate.evidence
    assert gate.evidence['dispatched'] == ['2'], gate.evidence
    assert len([call for call in gate.fresh_gets if call['url'] == VACANCY_URL]) == 2
    outcome = gate.dispatch_results[0]
    assert outcome['uncertain'] and not outcome['ok'] and not outcome['hh_hard_stop']
    attempt, = gate.native_attempts
    assert attempt.uncertain and attempt.acting
    assert attempt.repository.get(VACANCY_URL)['status'] == 'uncertain'
    assert attempt.repository.claim(VACANCY_URL, '', '') is None
    assert gate.entries['2']['action'] == 'apply_uncertain'
    assert gate.entries['3']['action'] == 'skipped_low_score'
    assert gate.evidence['matched'] == ['2', '3', '3'], gate.evidence
    assert not gate.result['hh_recovery']['stopped'] and gate.evidence['hard_stops'] == 0
    assert gate.result['applied'] == 0 and gate.result['source_stats']['hh']['uncertain'] == 1
    assert gate.second_result['applied'] == 0
    assert len(gate.notices) == 1
    assert gate.notices[0].kwargs['uncertain'] is True
    assert 'продолжается' in gate.notices[0].kwargs['note']
    assert 'Автоматического повтора нет' in gate.notices[0].kwargs['note']


@pytest.mark.parametrize('failure', ['anti_bot', 'unexpected_ui', 'auth_ui', 'auth_server', 'unknown', 'binding'])
def test_compromised_session_still_stops_source_immediately(tmp_path, monkeypatch, failure):
    gate = asyncio.run(run_hh_gate(tmp_path, monkeypatch, noise=False, stale_receipt=True,
                                  continue_after_apply=True, session_failure=failure,
                                  confirmation_html=UNCONFIRMED_HTML if failure == 'auth_server' else APPLIED_HTML))
    assert gate.application_posts == [('POST', POPUP_URL)], gate.evidence
    assert gate.dispatch_results[0]['uncertain']
    assert gate.entries['2']['action'] == 'apply_uncertain'
    assert '3' not in gate.evidence['matched'], gate.evidence
    assert gate.result['hh_recovery']['stopped'] and gate.evidence['hard_stops'] >= 1
    assert gate.notices and 'HH остановлен' in gate.notices[0].kwargs['note']


def test_next_owned_application_is_not_poisoned_by_prior_uncertainty(tmp_path, monkeypatch):
    gate = asyncio.run(run_hh_gate(tmp_path, monkeypatch, noise=False, stale_receipt=True,
                                  apply_next=True, confirmation_html=UNCONFIRMED_HTML))
    assert gate.evidence['dispatched'] == ['2', '3'], gate.evidence
    assert gate.application_posts == [('POST', POPUP_URL), ('POST', POPUP_URL)], gate.evidence
    assert gate.entries['2']['action'] == 'apply_uncertain'
    assert gate.entries['3']['action'] == 'applied', gate.evidence
    first, second = gate.native_attempts
    assert first.repository.get(first.url)['status'] == 'uncertain'
    assert second.repository.get(second.url)['status'] == 'completed'
    assert first.uncertain and not second.uncertain
    assert gate.result['applied'] == 1 and gate.evidence['hard_stops'] == 0
    assert not gate.result['hh_recovery']['stopped']
