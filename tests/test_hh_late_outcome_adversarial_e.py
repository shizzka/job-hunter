"""Independent end-to-end timing probes: real receipt/monitor/agent, no network."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import agent
import apply_orchestrator
import config
import manual_apply_queue
import seen
from hh.recovery import monitor_page
from hh.browser import CookieBinding, stop_browser
from state_store.hh_cookies import HHCookieRepository
from state_store.native_apply import NativeApplyRepository, run_native_attempt
from state_store.private_journal import read_json_records
from tests.test_observability import configure_synthetic_search


URL = 'https://hh.ru/vacancy/4'
DISPATCH = apply_orchestrator.dispatch_apply


class Emitter:
    def __init__(self):
        self.handlers = {}

    def on(self, event, handler):
        self.handlers[event] = handler

    def emit(self, event, value):
        self.handlers[event](value)


def run_case(monkeypatch, home, point, stop_outcome='success'):
    real_sleep = asyncio.sleep
    for key, name in {
        'JOB_HUNTER_HOME': '', 'SEEN_VACANCIES_FILE': 'seen.json',
        'RUN_HISTORY_FILE': 'runs.jsonl', 'RUNTIME_STATUS_FILE': 'runtime.json',
        'ANALYTICS_EVENTS_FILE': 'events.jsonl', 'ANALYTICS_STATE_FILE': 'analytics.json',
        'HH_STATE_DIR': 'hh', 'HH_GUARD_STATE_FILE': 'guard.json',
        'HH_COOKIES_FILE': 'cookies.json', 'HH_RESUME_PIPELINE_FILE': 'pipeline.json',
    }.items():
        monkeypatch.setattr(config, key, str(home / name))
    monkeypatch.setattr(config, 'ANALYTICS_ENABLED', True)
    monkeypatch.setattr(config, 'HH_PRIMARY_RESUME_ID', 'synthetic-exact')
    configure_synthetic_search(monkeypatch, home)
    context, page, socket = Emitter(), Emitter(), Emitter()
    page.context = context
    client = SimpleNamespace(_page=page, _context=context, start=AsyncMock(), stop=AsyncMock(),
                             is_logged_in=AsyncMock(return_value=True),
                             get_negotiation_statuses=AsyncMock(return_value=[]))
    monitor_page(client, page)
    repository = NativeApplyRepository(home / 'cookies.json', 'hh')
    signals, calls, notes = [], [], []

    def emit_late_action():
        if signals:
            return
        assert repository.get(URL)['status'] in {'failed', 'completed'}
        page.emit('websocket', socket)
        socket.emit('framesent', 'synthetic late external action')
        assert repository.get(URL)['status'] == 'uncertain'
        signals.append(point)

    async def lower(vacancy, *args, **kwargs):
        calls.append(vacancy['id'])
        async def operation():
            if point.startswith('exception'):
                raise RuntimeError('Synthetic known preparation refusal')
            if point in {'halt', 'known_guard'}:
                attempt = client._external_attempt
                attempt.begin()
                attempt.confirm_no_action()
                return {'ok': False, 'hh_hard_stop': True,
                        'hh_recovery_reason': 'native_attempt_already_acting'}
            if point.startswith('success'):
                return {'ok': True}
            if point.startswith('failure'):
                return {'ok': False, 'message': 'Synthetic known no-action refusal'}
            return {'ok': False, 'message': 'Вопросы, пропускаем', 'requires_questions': True}
        return await run_native_attempt(client, repository, URL, operation)

    async def capture(*args, **kwargs):
        if point in {'trace_capture', 'trace_capture_error', 'trace_capture_cancel', 'exception_capture'}:
            emit_late_action()
            await asyncio.sleep(0)
        if point == 'trace_capture_error':
            raise RuntimeError('Synthetic diagnostic failure after late receipt')
        if point == 'trace_capture_cancel':
            raise asyncio.CancelledError()

    def finish(**kwargs):
        if point == 'trace_finish':
            emit_late_action()

    trace = SimpleNamespace(last_stage='RESULT_CHECK', trace_id='synthetic-e-late', trace_dir=home,
                            event=lambda *args, **kwargs: None, capture=capture, finish=finish)
    async def snapshot(*args, **kwargs):
        if point in {'questions_snapshot', 'failure_snapshot', 'exception_snapshot'}:
            emit_late_action()
            await asyncio.sleep(0)
        return {}

    async def status(action, message, status):
        if ((point == 'questions_status' and action == 'search_manual' and 'вопросы' in message)
                or (point == 'failure_status' and action == 'search_manual' and 'не ушёл' in message)
                or (point == 'success_status' and action == 'search_apply_done')):
            emit_late_action()
            await asyncio.sleep(0)

    async def task_done(*args, **kwargs):
        if point == 'success_task':
            emit_late_action()
            await asyncio.sleep(0)

    async def hard_stop():
        if point == 'halt':
            # The original durable zero-dispatch manual classification MUST
            # exist before yielding into process termination.
            assert seen._load()['4']['action'] in {'manual_hh_guard_stop', 'apply_uncertain'}
            emit_late_action()
            await asyncio.sleep(0)
            if stop_outcome == 'cancel':
                raise asyncio.CancelledError()
            return stop_outcome == 'success'
        return True

    async def manual_note(*args, **kwargs):
        notes.append(str(kwargs.get('note', '')))

    client.hard_stop_browser = AsyncMock(side_effect=hard_stop)
    if point == 'success_pause':
        async def pause(seconds):
            if seconds == 3:
                emit_late_action()
            await real_sleep(0)
        monkeypatch.setattr(agent.asyncio, 'sleep', pause)
    if point.endswith('_cleanup'):
        cookie_repository = HHCookieRepository(home / 'cookies.json')
        _, revision = cookie_repository.snapshot()
        browser = SimpleNamespace(contexts=[context], _hh_owner_session=client)
        context.browser = browser
        client._browser, client._pw = browser, SimpleNamespace(stop=AsyncMock())
        client._cookie_binding = CookieBinding(cookie_repository, revision, False, context=context)
        async def cookies():
            emit_late_action()
            await asyncio.sleep(0)
            return []
        context.cookies = AsyncMock(side_effect=cookies)
        async def terminate(owned_browser):
            assert owned_browser is browser
            return True
        async def stop():
            return await stop_browser(client, terminate=terminate)
        client.stop = AsyncMock(side_effect=stop)
    vacancy = {'id': '4', 'source': 'hh', 'title': 'Synthetic', 'company': 'Synthetic', 'url': URL}
    candidates = [vacancy]
    if point not in {'known_questions', 'known_guard'} and not point.endswith('_cleanup'):
        candidates.append({**vacancy, 'id': '5', 'url': 'https://hh.ru/vacancy/5'})
    monkeypatch.setattr(agent, 'HHClient', lambda: client)
    monkeypatch.setattr(agent.search_pipeline, 'collect_all', AsyncMock(return_value=candidates))
    monkeypatch.setattr(apply_orchestrator, 'dispatch_apply', DISPATCH)
    monkeypatch.setattr(apply_orchestrator, '_dispatch_apply', lower)
    monkeypatch.setattr(apply_orchestrator, 'create_hh_apply_trace', lambda *args, **kwargs: trace)
    monkeypatch.setattr(agent, '_save_autoapply_failure_snapshot', snapshot)
    monkeypatch.setattr(agent, 'office_log', status)
    monkeypatch.setattr(agent, 'task_complete', task_done)
    monkeypatch.setattr(agent, 'notify_needs_manual', AsyncMock(side_effect=manual_note))
    cancelled = False
    result = None
    try:
        result = asyncio.run(agent.do_search())
    except asyncio.CancelledError:
        cancelled = True
    events = read_json_records(config.ANALYTICS_EVENTS_FILE)
    runs = read_json_records(config.RUN_HISTORY_FILE)
    funnel = (result or {}).get('funnel') or (runs[-1].get('funnel', {}) if runs else {})
    return {
        'receipt': repository.get(URL)['status'], 'seen': seen._load().get('4', {}).get('action'),
        'decisions': [event['decision'] for event in events if event.get('event') == 'decision'
                      and event.get('vacancy_id') == '4'],
        'applications': [event['outcome'] for event in events if event.get('event') == 'application_result'],
        'uncertain': funnel.get('uncertain', 0), 'applied': funnel.get('applied', 0),
        'stopped': (result or {}).get('hh_recovery', {}).get('stopped'),
        'hard_stops': client.hard_stop_browser.await_count,
        'success_notifications': agent.notify_application.await_count,
        'calls': calls, 'notes': notes, 'signals': signals, 'cancelled': cancelled,
        'details_calls': [call.args[0]['id'] for call in apply_orchestrator.fetch_vacancy_details.await_args_list],
    }


@pytest.mark.parametrize('point', [
    'trace_capture', 'trace_capture_error', 'trace_finish',
    'questions_snapshot', 'questions_status', 'failure_snapshot', 'failure_status',
    'exception_capture', 'exception_snapshot', 'success_status', 'success_task',
])
def test_same_vacancy_late_receipt_precedes_next_outcome_side_effect(monkeypatch, tmp_path, point):
    observed = run_case(monkeypatch, tmp_path, point)
    assert observed['signals'] == [point]
    assert observed['receipt'] == 'uncertain'
    assert observed['seen'] == 'apply_uncertain', observed
    assert observed['uncertain'] == 1, observed
    assert observed['applied'] == 0 and observed['decisions'][-1] == 'apply_uncertain', observed
    assert observed['hard_stops'] >= 1 and observed['stopped'] is True, observed
    assert observed['calls'] == ['4'], observed
    assert observed['details_calls'] == ['4'], observed
    assert observed['success_notifications'] == 0, observed
    assert observed['notes'] and all('мог' in note.casefold() and 'повтор' in note.casefold()
                                     for note in observed['notes']), observed


@pytest.mark.parametrize('stop_outcome', ['success', 'failure', 'cancel'])
def test_late_receipt_during_halt_promotes_prior_durable_guard_stop(monkeypatch, tmp_path, stop_outcome):
    observed = run_case(monkeypatch, tmp_path, 'halt', stop_outcome)
    assert observed['receipt'] == 'uncertain'
    assert observed['seen'] == 'apply_uncertain', observed
    assert observed['uncertain'] == 1, observed
    assert observed['applied'] == 0 and observed['decisions'][-1] == 'apply_uncertain', observed
    assert observed['calls'] == ['4'] and observed['success_notifications'] == 0, observed
    assert observed['details_calls'] == ['4'], observed
    if stop_outcome == 'success':
        assert observed['notes'] and all('мог' in note.casefold() for note in observed['notes']), observed
    if stop_outcome == 'cancel':
        assert observed['cancelled'] is True


@pytest.mark.parametrize('point,seen_action', [
    ('known_questions', 'skipped_questions'), ('known_guard', 'manual_hh_guard_stop'),
])
def test_owned_clean_no_action_semantics_remain_manual_without_uncertainty(monkeypatch, tmp_path, point, seen_action):
    observed = run_case(monkeypatch, tmp_path, point)
    assert observed['signals'] == []
    assert observed['receipt'] == 'failed' and observed['seen'] == seen_action, observed
    assert observed['uncertain'] == 0 and observed['success_notifications'] == 0, observed


@pytest.mark.parametrize('point', ['questions_cleanup', 'success_cleanup'])
def test_search_final_cookie_capture_promotes_owned_terminal_disposition(monkeypatch, tmp_path, point):
    observed = run_case(monkeypatch, tmp_path, point)
    assert observed['signals'] == [point] and observed['receipt'] == 'uncertain'
    assert observed['seen'] == 'apply_uncertain', observed
    assert observed['uncertain'] == 1 and observed['applied'] == 0, observed
    assert observed['decisions'][-1] == 'apply_uncertain', observed
    assert observed['calls'] == observed['details_calls'] == ['4'], observed


def test_late_action_during_between_vacancy_pause_stops_before_next_details(monkeypatch, tmp_path):
    observed = run_case(monkeypatch, tmp_path, 'success_pause')
    assert observed['signals'] == ['success_pause'] and observed['receipt'] == 'uncertain'
    assert observed['seen'] == 'apply_uncertain' and observed['uncertain'] == 1, observed
    assert observed['applied'] == 0 and observed['decisions'][-1] == 'apply_uncertain', observed
    assert observed['calls'] == observed['details_calls'] == ['4'], observed


def test_late_diagnostic_cancellation_persists_uncertainty_before_propagation(monkeypatch, tmp_path):
    observed = run_case(monkeypatch, tmp_path, 'trace_capture_cancel')
    assert observed['cancelled'] is True and observed['receipt'] == 'uncertain'
    assert observed['seen'] == 'apply_uncertain' and observed['uncertain'] == 1, observed
    assert observed['applied'] == 0 and observed['decisions'][-1] == 'apply_uncertain', observed
    assert observed['calls'] == observed['details_calls'] == ['4'], observed


@pytest.mark.parametrize('preliminary_ok', [False, True])
@pytest.mark.parametrize('deliver_late', [False, True])
def test_manual_token_final_shutdown_cannot_return_stale_owned_receipt(monkeypatch, tmp_path, preliminary_ok, deliver_late):
    for key, name in {'JOB_HUNTER_HOME': '', 'SEEN_VACANCIES_FILE': 'seen.json',
        'RUN_HISTORY_FILE': 'runs.jsonl', 'RUNTIME_STATUS_FILE': 'runtime.json',
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
    monkeypatch.setattr(agent.hh_guard, 'record_apply_success', lambda: None)
    monkeypatch.setattr(agent, 'generate_cover_letter', AsyncMock(return_value='Synthetic authorized cover'))
    monkeypatch.setattr(agent, 'notify_needs_manual', AsyncMock())
    monkeypatch.setattr(agent, 'notify_application', AsyncMock())
    vacancy = {'id': '4', 'source': 'hh', 'title': 'Synthetic', 'company': 'Synthetic', 'url': URL}
    candidate = manual_apply_queue.create_candidate(vacancy, {'score': 55, 'reason': 'Synthetic'},
                                                    details='Ordinary synthetic vacancy', profile_name='synthetic')
    context, page, socket = Emitter(), Emitter(), Emitter()
    page.context = context
    client = SimpleNamespace(_page=page, _context=context, start=AsyncMock(),
                             is_logged_in=AsyncMock(return_value=True))
    monitor_page(client, page)
    repository = NativeApplyRepository(tmp_path / 'cookies.json', 'hh')
    cookie_repository = HHCookieRepository(tmp_path / 'cookies.json')
    _, revision = cookie_repository.snapshot()
    browser = SimpleNamespace(contexts=[context], _hh_owner_session=client)
    context.browser = browser
    client._browser, client._pw = browser, SimpleNamespace(stop=AsyncMock())
    client._cookie_binding = CookieBinding(cookie_repository, revision, False, context=context)
    preliminary, signals = [], []

    async def cookies():
        preliminary.append(repository.get(URL)['status'])
        if deliver_late:
            page.emit('websocket', socket)
            socket.emit('framesent', 'Synthetic late action while shutdown awaits cookie capture')
        await asyncio.sleep(0)
        if deliver_late:
            signals.append(repository.get(URL)['status'])
        return []

    context.cookies = AsyncMock(side_effect=cookies)
    async def terminate(owned_browser):
        assert owned_browser is browser
        return True

    async def stop():
        # Production ownership/cookie-capture/termination/cleanup sequence;
        # only the external browser and process death are synthetic.
        return await stop_browser(client, terminate=terminate)

    client.stop = AsyncMock(side_effect=stop)
    monkeypatch.setattr(agent, 'HHClient', lambda: client)
    monkeypatch.setattr(apply_orchestrator, 'dispatch_apply', DISPATCH)
    monkeypatch.setattr(apply_orchestrator, 'create_hh_apply_trace', lambda *args, **kwargs: None)

    async def lower(*args, **kwargs):
        async def operation():
            return {'ok': preliminary_ok, 'message': 'Synthetic clean owned outcome'}
        return await run_native_attempt(client, repository, URL, operation)

    monkeypatch.setattr(apply_orchestrator, '_dispatch_apply', lower)
    returned = asyncio.run(agent.do_manual_apply_token(candidate['token']))
    events = read_json_records(config.ANALYTICS_EVENTS_FILE)
    observed = {'native': repository.get(URL)['status'], 'seen': seen._load().get('4', {}).get('action'),
                'queue': manual_apply_queue.get_candidate(candidate['token'])['status'], 'result': returned,
                'decisions': [event['decision'] for event in events if event.get('event') == 'decision']}
    assert preliminary == ['completed' if preliminary_ok else 'failed']
    context.cookies.assert_awaited_once()
    assert client._page is None and client._context is None
    if not deliver_late:
        assert signals == [] and observed['native'] == preliminary[0], observed
        assert returned['ok'] is preliminary_ok and not returned.get('uncertain'), observed
        assert observed['queue'] == ('applied' if preliminary_ok else 'failed'), observed
        assert observed['seen'] == ('manual_ai_applied' if preliminary_ok else None), observed
        return
    assert signals == ['uncertain']
    assert observed['native'] == 'uncertain'
    assert observed['seen'] == 'apply_uncertain' and observed['queue'] == 'uncertain', observed
    assert returned.get('uncertain') is True and returned.get('ok') is False, observed
    assert observed['decisions'][-1] == 'apply_uncertain', observed
    assert not repository.claim(URL, '', '')
    assert not manual_apply_queue.claim_candidate(candidate['token'])
