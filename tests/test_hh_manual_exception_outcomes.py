"""A UI exception must not finalize a manual token before owned shutdown."""
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
from state_store.native_apply import NativeApplyRepository, run_native_attempt
from state_store.private_journal import read_json_records
from tests.test_hh_late_outcome_adversarial_e import DISPATCH, Emitter

URL = 'https://hh.ru/vacancy/4'


def run_exception(monkeypatch, tmp_path, *, phase='apply', late=False, recovered=True, termination='success', operation_raises=True):
    for key, name in {'JOB_HUNTER_HOME': '', 'SEEN_VACANCIES_FILE': 'seen.json',
        'ANALYTICS_EVENTS_FILE': 'events.jsonl', 'ANALYTICS_STATE_FILE': 'analytics.json',
        'HH_STATE_DIR': 'hh', 'HH_GUARD_STATE_FILE': 'guard.json',
        'HH_COOKIES_FILE': 'cookies.json', 'HH_RESUME_PIPELINE_FILE': 'pipeline.json'}.items():
        monkeypatch.setattr(config, key, str(tmp_path / name))
    monkeypatch.setattr(config, 'ANALYTICS_ENABLED', True)
    monkeypatch.setattr(config, 'HH_APPLICATION_MODE', 'auto')
    monkeypatch.setattr(config, 'HH_PRIMARY_RESUME_ID', 'synthetic-exact')
    monkeypatch.setattr(manual_apply_queue, '_queue_path', lambda profile_name=None: tmp_path / 'queue.json')
    monkeypatch.setattr(agent.company_blacklist, 'is_blocked', lambda *args: False)
    monkeypatch.setattr(agent.hh_guard, 'can_auto_apply', lambda: (True, ''))
    monkeypatch.setattr(agent, 'generate_cover_letter', AsyncMock(return_value='Synthetic approved cover'))
    vacancy = {'id': '4', 'source': 'hh', 'title': 'Synthetic', 'company': 'Synthetic', 'url': URL}
    candidate = manual_apply_queue.create_candidate(vacancy, {'score': 55, 'reason': 'Synthetic'},
                                                    details='Synthetic vacancy', profile_name='synthetic')
    context, page, socket = Emitter(), Emitter(), Emitter()
    page.context = context
    client = SimpleNamespace(_page=page, _context=context, start=AsyncMock())
    monitor_page(client, page)
    repository = NativeApplyRepository(tmp_path / 'cookies.json', 'hh')
    cookie_repository = HHCookieRepository(tmp_path / 'cookies.json')
    _, revision = cookie_repository.snapshot()
    browser = SimpleNamespace(contexts=[context], _hh_owner_session=client)
    context.browser = browser
    client._browser, client._pw = browser, SimpleNamespace(stop=AsyncMock())
    client._cookie_binding = CookieBinding(cookie_repository, revision, False, context=context)
    queue_at_capture, notices, lower_calls = [], [], []

    async def ui_exception():
        if not operation_raises:
            return {'ok': False, 'message': 'Synthetic known no-action refusal'}
        exc = apply_orchestrator.HHUnexpectedUI('manual_synthetic', 'e' * 64)
        exc.hh_recovered, exc.hh_uncertain = recovered, False
        raise exc

    client.is_logged_in = AsyncMock(side_effect=ui_exception if phase == 'login' else None, return_value=True)

    async def lower(*args, **kwargs):
        lower_calls.append('dispatch')
        return await run_native_attempt(client, repository, URL, ui_exception)

    async def cookies():
        queue_at_capture.append(manual_apply_queue.get_candidate(candidate['token'])['status'])
        if late:
            page.emit('websocket', socket)
            socket.emit('framesent', 'Synthetic possible action during owned shutdown')
        await asyncio.sleep(0)
        return []

    async def terminate(owned_browser):
        assert owned_browser is browser
        if termination == 'cancel':
            raise asyncio.CancelledError()
        return termination == 'success'

    async def stop():
        return await stop_browser(client, terminate=terminate)

    async def notice(v, *args, **kwargs):
        assert v['id'] == '4' and client._page is None and client._context is None
        notices.append(kwargs['note'])

    context.cookies = AsyncMock(side_effect=cookies)
    client.stop = AsyncMock(side_effect=stop)
    monkeypatch.setattr(agent, 'HHClient', lambda: client)
    monkeypatch.setattr(agent, 'notify_needs_manual', AsyncMock(side_effect=notice))
    monkeypatch.setattr(agent, 'notify_application', AsyncMock())
    monkeypatch.setattr(apply_orchestrator, 'dispatch_apply', DISPATCH)
    monkeypatch.setattr(apply_orchestrator, '_dispatch_apply', lower)
    monkeypatch.setattr(apply_orchestrator, 'create_hh_apply_trace', lambda *args, **kwargs: None)
    returned, error = None, None
    try:
        returned = asyncio.run(agent.do_manual_apply_token(candidate['token']))
    except (RuntimeError, asyncio.CancelledError) as exc:
        error = exc
    state = manual_apply_queue.get_candidate(candidate['token'])
    decisions = [event['decision'] for event in read_json_records(config.ANALYTICS_EVENTS_FILE)
                 if event.get('event') == 'decision']
    expected_uncertain = late or (phase == 'apply' and not recovered) or termination != 'success'
    assert lower_calls == (['dispatch'] if phase == 'apply' else [])
    assert context.cookies.await_count == client.stop.await_count == 1
    assert queue_at_capture == (['uncertain'] if phase == 'apply' and not recovered else ['applying'])
    assert seen._load()['4']['action'] == ('apply_uncertain' if expected_uncertain else 'manual_hh_guard_stop')
    assert state['status'] == ('uncertain' if expected_uncertain else 'dismissed')
    assert decisions == (['apply_uncertain'] if expected_uncertain else ['guard_stop'])
    if phase == 'apply':
        assert repository.get(URL)['status'] == ('uncertain' if late else 'failed')
    if termination == 'success':
        assert error is None and returned['ok'] is False
        assert returned['uncertain'] is expected_uncertain
        assert returned['reason'] == 'hh_unexpected_ui'
        assert len(notices) == 1
        if expected_uncertain:
            assert 'мог быть отправлен' in returned['message'] == notices[0]
            assert not manual_apply_queue.claim_candidate(candidate['token'])
            if late and phase == 'apply':
                assert not repository.claim(URL, '', '')
    else:
        assert isinstance(error, asyncio.CancelledError if termination == 'cancel' else RuntimeError)
        assert returned is None and notices == []
        agent.notify_needs_manual.assert_not_awaited()
        assert not manual_apply_queue.claim_candidate(candidate['token'])
    agent.notify_application.assert_not_awaited()


@pytest.mark.parametrize('phase', ['apply', 'login'])
@pytest.mark.parametrize('late', [False, True])
def test_recovered_ui_exception_rechecks_owned_shutdown_before_manual_outcome(monkeypatch, tmp_path, phase, late):
    run_exception(monkeypatch, tmp_path, phase=phase, late=late)


@pytest.mark.parametrize('late', [False, True])
def test_nonrecoverable_ui_exception_stays_uncertain_before_shutdown(monkeypatch, tmp_path, late):
    run_exception(monkeypatch, tmp_path, late=late, recovered=False)


@pytest.mark.parametrize('termination', ['failure', 'cancel'])
@pytest.mark.parametrize('late', [False, True])
def test_ui_exception_unproven_shutdown_persists_manual_uncertainty(monkeypatch, tmp_path, termination, late):
    run_exception(monkeypatch, tmp_path, late=late, termination=termination)


@pytest.mark.parametrize('termination', ['failure', 'cancel'])
@pytest.mark.parametrize('late', [False, True])
def test_normal_result_unproven_shutdown_stops_before_notification(monkeypatch, tmp_path, termination, late):
    run_exception(monkeypatch, tmp_path, late=late, termination=termination, operation_raises=False)
