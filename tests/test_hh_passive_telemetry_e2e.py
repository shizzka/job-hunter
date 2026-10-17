"""Offline Chromium: live telemetry signatures preserve the real apply/search flow."""
import asyncio

import pytest

from tests.test_e2e_hh_apply_happy_path import run_hh_gate
from tests.test_hh_passive_telemetry import live_script


@pytest.mark.parametrize('case', ['confirmed', 'uncertain', 'extra_query', 'sentry_host', 'sentry_path', 'sentry_large', 'second_popup', 'disabled'])
def test_live_telemetry_through_apply_and_search(tmp_path, monkeypatch, case):
    effect = live_script()
    if case == 'extra_query':
        effect += "await fetch('/anatskytics?event=applied&unproven=1',{method:'POST',headers:{'content-type':'application/x-www-form-urlencoded'},body:''}).catch(()=>{});"
    elif case in {'sentry_host', 'sentry_path', 'sentry_large'}:
        url = {'sentry_host': 'https://unproven.invalid/api/607/envelope/',
               'sentry_path': 'https://sentry.hh.ru/api/607/not-envelope/',
               'sentry_large': 'https://sentry.hh.ru/api/607/envelope/'}[case]
        body = "'x'.repeat(65537)" if case == 'sentry_large' else "'{}\\n{}'"
        effect += ("await fetch('" + url + "',{method:'POST',mode:'no-cors',headers:{'content-type':'text/plain'},body:"
                   + body + "}).catch(()=>{});")
    elif case == 'second_popup':
        effect += "await fetch('/applicant/vacancy_response/popup',{method:'POST',body:'vacancy_id=2'}).catch(()=>{});"
    elif case == 'disabled':
        monkeypatch.setenv('HH_PASSIVE_TELEMETRY_ENABLED', '0')
    options = {}
    if case == 'uncertain':
        options['confirmation_html'] = '<main><h1 data-qa="vacancy-title">Synthetic vacancy</h1></main>'
    gate = asyncio.run(run_hh_gate(tmp_path, monkeypatch, noise=False,
        stale_receipt=True, continue_after_apply=True, post_submit_effect=effect, **options))
    attempt, = gate.native_attempts
    continues = case in {'confirmed', 'uncertain'}
    assert attempt.context._hh_action_watch.unknown is not continues, gate.evidence
    assert attempt.page._hh_action_monitor.unknown is not continues, gate.evidence
    assert gate.result['hh_recovery']['stopped'] is not continues, gate.evidence
    if continues:
        assert gate.evidence['hard_stops'] == 0, gate.evidence
    else:
        assert gate.evidence['hard_stops'] >= 1, gate.evidence
    assert gate.evidence['matched'] == (['2', '3'] if continues else ['2']), gate.evidence
    if continues:
        assert len(attempt._hh_application_posts) == 1
        assert attempt._hh_application_posts[0]['owned'] and attempt._hh_application_posts[0]['status'] == 200
    result = gate.dispatch_results[0]
    assert result['uncertain'] is (case != 'confirmed'), gate.evidence
    assert bool(result.get('hh_hard_stop')) is not continues
    assert attempt.repository.get(attempt.url)['status'] == ('completed' if case == 'confirmed' else 'uncertain')
    assert not attempt.repository.claim(attempt.url, '', '')
