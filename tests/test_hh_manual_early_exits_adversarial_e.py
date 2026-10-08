"""Claimed manual early outcomes stay provisional through owned shutdown."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import agent
import apply_orchestrator
import config
import manual_apply_queue
import seen
from hh.browser import CookieBinding, stop_browser
from hh.recovery import monitor_page
from state_store.hh_cookies import HHCookieRepository
from state_store.native_apply import NativeApplyRepository
from state_store.private_journal import read_json_records
from tests.test_hh_late_outcome_adversarial_e import Emitter

URL = 'https://hh.ru/vacancy/4'
EARLY = ('login_false', 'login_runtime', 'deferred', 'no_cover', 'archived', 'revoked')
CLEAN = {
    'login_false': ('failed', None, []),
    'login_runtime': ('failed', None, []),
    'deferred': ('deferred', None, []),
    'no_cover': ('failed_no_cover', None, ['apply_failed']),
    'archived': ('archived', 'manual_ai_archived', ['skipped_low_score']),
    'revoked': ('dismissed', None, []),
}


def run_early(monkeypatch, home, outcome, *, late=False, termination='success'):
    for key, name in {
        'JOB_HUNTER_HOME': '', 'SEEN_VACANCIES_FILE': 'seen.json',
        'ANALYTICS_EVENTS_FILE': 'events.jsonl', 'ANALYTICS_STATE_FILE': 'analytics.json',
        'HH_STATE_DIR': 'hh', 'HH_GUARD_STATE_FILE': 'guard.json',
        'HH_COOKIES_FILE': 'cookies.json', 'HH_RESUME_PIPELINE_FILE': 'pipeline.json',
    }.items():
        monkeypatch.setattr(config, key, str(home / name))
    monkeypatch.setattr(config, 'ANALYTICS_ENABLED', True)
    monkeypatch.setattr(config, 'HH_APPLICATION_MODE', 'auto')
    monkeypatch.setattr(config, 'HH_PRIMARY_RESUME_ID', 'synthetic-exact')
    monkeypatch.setattr(manual_apply_queue, '_queue_path', lambda profile_name=None: home / 'queue.json')
    monkeypatch.setattr(agent.company_blacklist, 'is_blocked', lambda *args: False)
    monkeypatch.setattr(agent.hh_guard, 'can_auto_apply',
                        lambda: (False, 'Synthetic guard refusal') if outcome == 'deferred' else (True, ''))
    vacancy = {'id': '4', 'source': 'hh', 'title': 'Synthetic', 'company': 'Synthetic', 'url': URL}
    candidate = manual_apply_queue.create_candidate(
        vacancy, {'score': 55, 'reason': 'Synthetic'},
        details='Вакансия закрыта' if outcome == 'archived' else 'Ordinary synthetic vacancy',
        profile_name='synthetic')
    context, page, socket = Emitter(), Emitter(), Emitter()
    page.context = context
    client = SimpleNamespace(_page=page, _context=context, start=AsyncMock(),
                             is_logged_in=AsyncMock(return_value=outcome != 'login_false'))
    if outcome == 'login_runtime':
        client.is_logged_in = AsyncMock(side_effect=RuntimeError('Synthetic login evaluation failure'))
    elif outcome == 'predispatch_cancel':
        client.is_logged_in = AsyncMock(side_effect=asyncio.CancelledError())
    monitor_page(client, page)
    repository = NativeApplyRepository(home / 'cookies.json', 'hh')
    cookie_repository = HHCookieRepository(home / 'cookies.json')
    _, revision = cookie_repository.snapshot()
    browser = SimpleNamespace(contexts=[context], _hh_owner_session=client)
    context.browser = browser
    driver = SimpleNamespace(stop=AsyncMock())
    client._browser, client._pw = browser, driver
    client._cookie_binding = CookieBinding(cookie_repository, revision, False, context=context)
    notes, captures, terminations = [], [], []
    dead = False

    async def cover(*args):
        if outcome == 'revoked':
            # Exercise the actual human feedback API while this owner applies.
            manual_apply_queue.record_feedback(candidate['token'], 'bad')
        return '' if outcome == 'no_cover' else 'Synthetic authorized cover'

    async def cookies():
        item = manual_apply_queue.get_candidate(candidate['token'])
        captures.append({
            'queue': item['status'], 'owner': item['owner'],
            'seen': seen._load().get('4', {}).get('action'), 'notices': len(notes),
            'decisions': [event['decision'] for event in read_json_records(config.ANALYTICS_EVENTS_FILE)
                          if event.get('event') == 'decision'],
        })
        if late:
            page.emit('websocket', socket)
            socket.emit('framesent', 'Synthetic unowned mutation during early-outcome shutdown')
        await asyncio.sleep(0)
        return []

    async def terminate(owned_browser):
        nonlocal dead
        assert owned_browser is browser
        terminations.append(owned_browser)
        if termination == 'cancel':
            raise asyncio.CancelledError()
        if termination == 'failure':
            return False
        dead = True
        return True

    async def stop():
        # The complete production captured-owner/cookie/driver sequence runs;
        # external browser transport and process death are the only doubles.
        return await stop_browser(client, terminate=terminate)

    async def note(v, *args, **kwargs):
        assert v['id'] == '4'
        notes.append({'note': str(kwargs.get('note', '')), 'safe': dead,
                      'released': client._page is None and client._context is None})

    context.cookies = AsyncMock(side_effect=cookies)
    client.stop = AsyncMock(side_effect=stop)
    dispatch = AsyncMock(side_effect=AssertionError('Early outcome must never dispatch'))
    monkeypatch.setattr(agent, 'HHClient', lambda: client)
    monkeypatch.setattr(agent, 'generate_cover_letter', AsyncMock(side_effect=cover))
    monkeypatch.setattr(agent, 'notify_needs_manual', AsyncMock(side_effect=note))
    monkeypatch.setattr(agent, 'notify_application', AsyncMock())
    monkeypatch.setattr(apply_orchestrator, 'dispatch_apply', dispatch)
    returned, error = None, None
    try:
        returned = asyncio.run(agent.do_manual_apply_token(candidate['token']))
    except (RuntimeError, asyncio.CancelledError) as exc:
        error = exc
    item = manual_apply_queue.get_candidate(candidate['token'])
    decisions = [event['decision'] for event in read_json_records(config.ANALYTICS_EVENTS_FILE)
                 if event.get('event') == 'decision']
    observed = {
        'result': returned, 'error': error, 'queue': item, 'captures': captures, 'notes': notes,
        'seen': seen._load().get('4', {}).get('action'), 'decisions': decisions,
        'unknown': context._hh_action_watch.unknown, 'dead': dead, 'client': client, 'driver': driver,
    }
    dispatch.assert_not_awaited()
    agent.notify_application.assert_not_awaited()
    assert repository.get(URL) == {} and not repository.path.exists(), observed
    context.cookies.assert_awaited_once()
    client.stop.assert_awaited_once()
    assert len(terminations) == 1 and observed['unknown'] is late, observed
    assert item['owner'] == captures[0]['owner'], observed
    return observed


def assert_provisional(observed):
    capture = observed['captures'][0]
    assert capture['queue'] == 'applying', observed
    assert capture['seen'] is None and capture['decisions'] == [] and capture['notices'] == 0, observed


def assert_uncertain(observed):
    assert observed['seen'] == 'apply_uncertain' and observed['queue']['status'] == 'uncertain', observed
    assert observed['decisions'] == ['apply_uncertain'], observed
    assert manual_apply_queue.claim_candidate(observed['queue']['token']) is None, observed


def assert_clean(observed, outcome):
    queue, action, decisions = CLEAN[outcome]
    assert observed['error'] is None and observed['result']['ok'] is False, observed
    assert not observed['result'].get('uncertain'), observed
    assert observed['queue']['status'] == queue and observed['seen'] == action, observed
    assert observed['decisions'] == decisions, observed
    if outcome == 'archived':
        assert observed['result'].get('closed_or_archived') is True
    elif outcome == 'no_cover':
        assert observed['result'].get('no_cover') is True
    elif outcome == 'deferred':
        assert observed['result'].get('deferred') is True


@pytest.mark.parametrize('outcome', EARLY)
@pytest.mark.parametrize('late', [False, True])
def test_claimed_early_outcome_waits_for_death_and_late_evidence(monkeypatch, tmp_path, outcome, late):
    observed = run_early(monkeypatch, tmp_path, outcome, late=late)
    assert_provisional(observed)
    assert observed['error'] is None and observed['dead'], observed
    assert observed['client']._page is None and observed['client']._context is None, observed
    observed['driver'].stop.assert_awaited_once()
    assert all(note['safe'] and note['released'] for note in observed['notes']), observed
    if not late:
        assert_clean(observed, outcome)
        assert len(observed['notes']) == (0 if outcome in {'archived', 'revoked'} else 1), observed
        return
    assert_uncertain(observed)
    assert observed['result']['uncertain'] is True and observed['result']['ok'] is False, observed
    assert not any(observed['result'].get(flag) for flag in ('no_cover', 'closed_or_archived', 'deferred')), observed
    assert len(observed['notes']) == 1, observed
    assert 'мог' in observed['notes'][0]['note'] and 'повтор' in observed['notes'][0]['note'], observed


@pytest.mark.parametrize('outcome', EARLY)
@pytest.mark.parametrize('termination', ['failure', 'cancel'])
def test_claimed_early_unproven_death_propagates_with_durable_uncertainty(monkeypatch, tmp_path, outcome, termination):
    observed = run_early(monkeypatch, tmp_path, outcome, termination=termination)
    assert_provisional(observed)
    assert_uncertain(observed)
    expected = asyncio.CancelledError if termination == 'cancel' else RuntimeError
    assert isinstance(observed['error'], expected) and observed['result'] is None, observed
    assert observed['notes'] == [] and not observed['dead'], observed
    assert observed['client']._page is not None and observed['client']._context is not None, observed
    observed['driver'].stop.assert_not_awaited()


@pytest.mark.parametrize('late', [False, True])
def test_predispatch_cancellation_seals_then_preserves_sticky_late_evidence(monkeypatch, tmp_path, late):
    observed = run_early(monkeypatch, tmp_path, 'predispatch_cancel', late=late)
    assert_provisional(observed)
    assert isinstance(observed['error'], asyncio.CancelledError) and observed['result'] is None, observed
    assert observed['dead'] and observed['notes'] == [], observed
    observed['driver'].stop.assert_awaited_once()
    if late:
        assert_uncertain(observed)
    else:
        assert observed['seen'] is None and observed['queue']['status'] == 'pending', observed
        assert observed['decisions'] == [], observed


@pytest.mark.parametrize('outcome', EARLY)
def test_clean_early_business_classifications_are_preserved(monkeypatch, tmp_path, outcome):
    # Separate compatibility controls show the original terminal business
    # outcomes, independently of the additional shutdown ordering invariant.
    observed = run_early(monkeypatch, tmp_path, outcome)
    assert_clean(observed, outcome)
    assert observed['dead'] and not observed['unknown'], observed
