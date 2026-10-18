"""Reconciliation refusals are observable without disclosing private evidence."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hh import verification
from tests.test_hh_post_submit_verification import APPLIED_HTML, VACANCY_URL


@pytest.mark.parametrize('failure,step,reads', [
    ('reentry', 'reentry', 0),
    ('no_proof', 'owned_post', 0),
    ('no_cookie', 'post_cookie_missing_or_ambiguous', 0),
    ('cookie', 'post_cookie_mismatch', 0),
    ('status', 'vacancy_response', 1),
    ('url', 'vacancy_response', 1),
    ('mime', 'vacancy_response', 1),
    ('html', 'applied_server_html_false', 2),
    ('page', 'ownership_changed', 1),
    ('context', 'ownership_changed', 1),
    ('attempt', 'ownership_changed', 1),
    ('sequence', 'post_sequence_changed', 1),
    ('anonymous', 'server_auth_or_challenge', 1),
    ('captcha', 'server_auth_or_challenge', 1),
    ('dispose', 'response_dispose', 1),
])
def test_refusal_names_exact_step_without_tokens(monkeypatch, failure, step, reads):
    token = 'PRIVATE_COOKIE_SENTINEL'
    request = SimpleNamespace(all_headers=AsyncMock(return_value={
        'cookie': '' if failure == 'no_cookie' else 'hhtoken=' + token}))
    proof = {'url': 'https://hh.ru/applicant/vacancy_response/popup', 'fields': {'vacancy_id': '2'}}
    context = SimpleNamespace(cookies=AsyncMock(return_value=[{
        'name': 'hhtoken', 'value': 'other' if failure == 'cookie' else token}]),
        _hh_action_watch=SimpleNamespace(application_post_sequence=1, unknown=False))
    page = SimpleNamespace(_hh_action_monitor=SimpleNamespace(unknown=False))
    attempt = SimpleNamespace(page=page, context=context, _hh_application_posts=[{'request': request}])
    session = SimpleNamespace(_page=page, _context=context, _external_attempt=attempt)
    if failure == 'reentry':
        attempt._hh_reconciliation_started = True
    monkeypatch.setattr(verification, '_owned_post', lambda *args: None if failure == 'no_proof' else proof)
    html = {'html': '<h1 data-qa="vacancy-title">QA</h1>',
            'anonymous': APPLIED_HTML + '<script>{"userType":"anonymous"}</script>',
            'captcha': APPLIED_HTML + '<div data-qa="captcha">Captcha</div>'}.get(failure, APPLIED_HTML)
    response = SimpleNamespace(status=500 if failure == 'status' else 200,
        url='https://hh.ru/vacancy/3' if failure == 'url' else VACANCY_URL,
        headers={'content-type': 'application/json' if failure == 'mime' else 'text/html'},
        text=AsyncMock(return_value=html),
        dispose=AsyncMock(side_effect=RuntimeError('private error') if failure == 'dispose' else None))

    async def get(*args, **kwargs):
        if failure in {'page', 'context', 'attempt'}:
            setattr(session, {'page': '_page', 'context': '_context', 'attempt': '_external_attempt'}[failure], object())
        if failure == 'sequence':
            context._hh_action_watch.application_post_sequence += 1
        return response

    context.request = SimpleNamespace(get=AsyncMock(side_effect=get))
    asyncio.run(verification.reconcile_post_submit(session, attempt, 'resume', 'letter'))
    diagnostic = attempt._hh_reconciliation_diagnostic
    assert diagnostic['step'] == step
    assert context.request.get.await_count == reads
    assert token not in str(diagnostic) and 'private error' not in str(diagnostic)
    assert getattr(attempt, '_hh_verified_receipt', None) is None
    if failure in {'page', 'context', 'attempt'}:
        assert failure in diagnostic['failed'] and diagnostic['after'] == 'vacancy_get'
    if failure == 'html':
        assert diagnostic['read_number'] == 2


def _redirect_case(monkeypatch, location):
    token = 'T'
    request = SimpleNamespace(all_headers=AsyncMock(return_value={'cookie': 'hhtoken=' + token}))
    proof = {'url': 'https://hh.ru/applicant/vacancy_response/popup', 'fields': {'vacancy_id': '2'}}
    context = SimpleNamespace(cookies=AsyncMock(return_value=[{'name': 'hhtoken', 'value': token}]),
                              _hh_action_watch=SimpleNamespace(application_post_sequence=1, unknown=False))
    page = SimpleNamespace(_hh_action_monitor=SimpleNamespace(unknown=False))
    attempt = SimpleNamespace(page=page, context=context, _hh_application_posts=[{'request': request}])
    session = SimpleNamespace(_page=page, _context=context, _external_attempt=attempt)
    monkeypatch.setattr(verification, '_owned_post', lambda *args: proof)
    regional = 'https://spb.hh.ru/vacancy/2'
    responses = {
        'https://hh.ru/vacancy/2': SimpleNamespace(status=302, url='https://hh.ru/vacancy/2',
                                                   headers={'location': location}, text=AsyncMock(return_value=''),
                                                   dispose=AsyncMock()),
        regional: SimpleNamespace(status=200, url=regional, headers={'content-type': 'text/html'},
                                  text=AsyncMock(return_value=APPLIED_HTML), dispose=AsyncMock()),
    }
    urls = []

    async def get(url, **kwargs):
        urls.append(url)
        return responses.get(url, responses['https://hh.ru/vacancy/2'])

    context.request = SimpleNamespace(get=AsyncMock(side_effect=get))
    asyncio.run(verification.reconcile_post_submit(session, attempt, 'resume', 'letter'))
    return attempt, urls


def test_regional_vacancy_redirect_is_followed_once(monkeypatch):
    attempt, urls = _redirect_case(monkeypatch, 'https://spb.hh.ru/vacancy/2')
    assert urls == ['https://hh.ru/vacancy/2', 'https://spb.hh.ru/vacancy/2']
    assert attempt._hh_reconciliation_diagnostic['step'] == 'verified'


@pytest.mark.parametrize('location', ['https://spb.hh.ru/applicant/negotiations', 'https://evil.example/vacancy/2',
                                      'http://spb.hh.ru/vacancy/2', 'https://spb.hh.ru/vacancy/2?from=x',
                                      'https://hh.ru.evil.example/vacancy/2'])
def test_other_redirects_still_refuse(monkeypatch, location):
    attempt, urls = _redirect_case(monkeypatch, location)
    assert urls == ['https://hh.ru/vacancy/2']
    assert attempt._hh_reconciliation_diagnostic['step'] == 'vacancy_response'
