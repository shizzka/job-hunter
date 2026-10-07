"""Owned receipt history cannot become retryable after a later HH attempt."""
import asyncio
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import agent
import apply_orchestrator
import config
import manual_apply_queue
import seen
from hh.recovery import monitor_page
from state_store.native_apply import NativeApplyRepository, run_native_attempt
from state_store.private_journal import read_json_records
from tests.test_hh_late_outcome_adversarial_e import Emitter
from tests.test_observability import configure_synthetic_search

OLD_URL = 'https://hh.ru/vacancy/4'
NEXT_URL = 'https://hh.ru/vacancy/5'
DISPATCH = apply_orchestrator.dispatch_apply
CREATE_CANDIDATE = manual_apply_queue.create_candidate


def client_page(context=None):
    context = context or Emitter()
    page = Emitter()
    page.context = context
    client = SimpleNamespace(_page=page, _context=context)
    monitor_page(client, page)
    return client, context, page


def known_attempt(client, repository, url, ok=False):
    async def operation():
        return {'ok': ok, 'message': 'Synthetic known clean outcome'}
    result = asyncio.run(run_native_attempt(client, repository, url, operation))
    assert result['ok'] is ok and not result.get('uncertain')
    return client._page._hh_action_monitor.last_attempt


def late_old_post(context, page):
    request = SimpleNamespace(method='POST', url='https://hh.ru/applicant/vacancy_response',
        frame=SimpleNamespace(page=page), headers={'content-type': 'application/x-www-form-urlencoded'},
        post_data='vacancyId=4&resumeId=synthetic&letter=late')
    context.emit('request', request)


@pytest.mark.parametrize('old_ok', [False, True])
@pytest.mark.parametrize('replacement_page', [False, True])
def test_unknown_action_preserves_all_prior_owners_in_owned_context(tmp_path, old_ok, replacement_page):
    client, context, page = client_page()
    repository = NativeApplyRepository(tmp_path / 'cookies.json', 'hh')
    old = known_attempt(client, repository, OLD_URL, old_ok)
    if replacement_page:
        page = Emitter()
        page.context = context
        client._page = page
        monitor_page(client, page)
    current = known_attempt(client, repository, NEXT_URL)
    late_old_post(context, page)
    assert repository.get(OLD_URL)['status'] == 'uncertain'
    assert repository.get(NEXT_URL)['status'] == 'uncertain'
    assert repository.get(OLD_URL)['owner'] == old.owner
    assert repository.get(NEXT_URL)['owner'] == current.owner
    # A restart has no old session latch; persisted ownership must stop it.
    fresh_client, _, _ = client_page()
    actions = []
    async def retry():
        actions.append('external operation must never begin')
        return {'ok': True}
    returned = asyncio.run(run_native_attempt(fresh_client, repository, OLD_URL, retry))
    assert returned['uncertain'] and not returned['ok'] and actions == []


def test_prior_registry_cannot_overwrite_foreign_current_owner(tmp_path):
    client, context, page = client_page()
    repository = NativeApplyRepository(tmp_path / 'cookies.json', 'hh')
    old = known_attempt(client, repository, OLD_URL)
    known_attempt(client, repository, NEXT_URL)
    foreign_owner = repository.claim(OLD_URL, '', '')
    assert foreign_owner and foreign_owner != old.owner
    before = repository.get(OLD_URL)
    late_old_post(context, page)
    assert repository.get(OLD_URL) == before
    assert repository.get(NEXT_URL)['status'] == 'uncertain'
    reconciled = apply_orchestrator.apply_result_with_current_uncertainty(
        {'source': 'hh', 'url': OLD_URL}, client, {'ok': False}, owned_attempt=old)
    assert reconciled['uncertain'] and not reconciled['ok']


def test_unknown_context_does_not_mutate_another_client_or_cookie_destination(tmp_path):
    first, first_context, first_page = client_page()
    second, _, second_page = client_page()
    first_repository = NativeApplyRepository(tmp_path / 'first' / 'cookies.json', 'hh')
    second_repository = NativeApplyRepository(tmp_path / 'second' / 'cookies.json', 'hh')
    assert first_repository.path != second_repository.path
    known_attempt(first, first_repository, OLD_URL)
    known_attempt(first, first_repository, NEXT_URL)
    known_attempt(second, second_repository, OLD_URL)
    before = second_repository.path.read_bytes()
    late_old_post(first_context, first_page)
    assert first_repository.get(OLD_URL)['status'] == 'uncertain'
    assert second_repository.path.read_bytes() == before
    assert second_repository.get(OLD_URL)['status'] == 'failed'
    assert not second_page._hh_action_monitor.unknown
    assert not getattr(second, '_hh_recovery_uncertain', False)


def test_repeated_target_history_never_replaces_the_latest_exact_owner(tmp_path):
    client, context, page = client_page()
    repository = NativeApplyRepository(tmp_path / 'cookies.json', 'hh')
    old = known_attempt(client, repository, OLD_URL)
    current = known_attempt(client, repository, OLD_URL)
    assert old.owner != current.owner
    late_old_post(context, page)
    assert repository.get(OLD_URL)['owner'] == current.owner
    assert repository.get(OLD_URL)['status'] == 'uncertain'
    assert not repository.claim(OLD_URL, '', '')


def test_clean_earlier_receipt_stays_clean_during_next_approved_owned_action(tmp_path):
    client, context, page = client_page()
    repository = NativeApplyRepository(tmp_path / 'cookies.json', 'hh')
    old = known_attempt(client, repository, OLD_URL)
    approvals = []
    async def operation():
        current = client._external_attempt
        current.begin()
        page._hh_request_approval = {'owner': current.owner, 'url': 'https://hh.ru/applicant/vacancy_response',
            'method': 'POST', 'enctype': 'application/x-www-form-urlencoded',
            'entries': [['vacancyId', '5'], ['resumeId', 'synthetic'], ['letter', 'approved']], 'used': False}
        context.emit('request', SimpleNamespace(method='POST', url=page._hh_request_approval['url'],
            frame=SimpleNamespace(page=page), headers={'content-type': 'application/x-www-form-urlencoded'},
            post_data='vacancyId=5&resumeId=synthetic&letter=approved'))
        assert page._hh_request_approval['used'] and page._hh_action_monitor.owners == {current.owner}
        previous = apply_orchestrator.apply_result_with_current_uncertainty(
            {'source': 'hh', 'url': OLD_URL}, client, {'ok': False}, owned_attempt=old)
        approvals.append(previous)
        return {'ok': True}
    returned = asyncio.run(run_native_attempt(client, repository, NEXT_URL, operation))
    assert not approvals[0].get('uncertain'), approvals
    assert returned['ok'] and not returned.get('uncertain')
    assert repository.get(OLD_URL)['status'] == 'failed'
    assert repository.get(NEXT_URL)['status'] == 'completed'
    assert not page._hh_action_monitor.unknown


def test_rejected_foreign_session_cannot_replace_owning_monitor_history(tmp_path):
    owner, context, page = client_page()
    repository = NativeApplyRepository(tmp_path / 'owner' / 'cookies.json', 'hh')
    foreign_repository = NativeApplyRepository(tmp_path / 'foreign' / 'cookies.json', 'hh')
    previous = known_attempt(owner, repository, OLD_URL)
    monitor = page._hh_action_monitor
    before, registry = repository.path.read_bytes(), dict(monitor.attempts)
    foreign = SimpleNamespace(_page=page, _context=context)
    external_operations = []
    async def forbidden():
        external_operations.append(True)
        return {'ok': True}
    returned = asyncio.run(run_native_attempt(foreign, foreign_repository, NEXT_URL, forbidden))
    assert returned['uncertain'] and not returned['ok'] and external_operations == []
    assert monitor.last_attempt is previous and monitor.attempts == registry
    assert repository.path.read_bytes() == before
    assert not previous.uncertain and not monitor.unknown


def synthetic_search(monkeypatch, tmp_path):
    for key, name in {'JOB_HUNTER_HOME': '', 'SEEN_VACANCIES_FILE': 'seen.json',
        'RUN_HISTORY_FILE': 'runs.jsonl', 'RUNTIME_STATUS_FILE': 'runtime.json',
        'ANALYTICS_EVENTS_FILE': 'events.jsonl', 'ANALYTICS_STATE_FILE': 'analytics.json',
        'HH_STATE_DIR': 'hh', 'HH_GUARD_STATE_FILE': 'guard.json',
        'HH_COOKIES_FILE': 'cookies.json', 'HH_RESUME_PIPELINE_FILE': 'pipeline.json'}.items():
        monkeypatch.setattr(config, key, str(tmp_path / name))
    monkeypatch.setattr(config, 'ANALYTICS_ENABLED', True)
    monkeypatch.setattr(config, 'HH_PRIMARY_RESUME_ID', 'synthetic-exact')
    configure_synthetic_search(monkeypatch, tmp_path)
    client, context, page = client_page()
    for name in ('start', 'stop'):
        setattr(client, name, AsyncMock())
    client.is_logged_in = AsyncMock(return_value=True)
    client.get_negotiation_statuses = AsyncMock(return_value=[])
    client.hard_stop_browser = AsyncMock(return_value=True)
    repository = NativeApplyRepository(tmp_path / 'cookies.json', 'hh')
    client._e_notes, client._e_tasks = [], []
    client._e_chat = AsyncMock(return_value={})
    monkeypatch.setitem(sys.modules, 'hh_chat_responder', SimpleNamespace(process_all=client._e_chat))
    monkeypatch.setattr(config, 'HH_CHAT_RESPONDER_ENABLED', True)
    def proven_stop():
        return client.hard_stop_browser.await_count > 0 or (client._page is None and client._context is None)
    async def manual_note(vacancy, *args, **kwargs):
        client._e_notes.append({'id': vacancy['id'], 'note': str(kwargs.get('note', '')),
            'late': bool(getattr(client, '_hh_recovery_uncertain', False)), 'safe': proven_stop()})
    def task(title, description, priority):
        client._e_tasks.append({'title': title, 'description': description,
            'late': bool(getattr(client, '_hh_recovery_uncertain', False)), 'safe': proven_stop()})
        return f'synthetic-task-{len(client._e_tasks)}'
    monkeypatch.setattr(agent, 'notify_needs_manual', AsyncMock(side_effect=manual_note))
    monkeypatch.setattr(agent, 'create_task', task)
    monkeypatch.setattr(agent, 'HHClient', lambda: client)
    monkeypatch.setattr(agent, 'task_complete', AsyncMock())
    monkeypatch.setattr(agent, '_save_autoapply_failure_snapshot', AsyncMock(return_value={}))
    monkeypatch.setattr(apply_orchestrator, 'dispatch_apply', DISPATCH)
    monkeypatch.setattr(apply_orchestrator, 'create_hh_apply_trace', lambda *args, **kwargs: None)
    return client, context, page, repository


def assert_manual_correction(client, vacancy_id, url):
    notes = [note for note in client._e_notes if note['id'] == vacancy_id and note['late']
             and 'мог' in note['note'].casefold() and 'повтор' in note['note'].casefold()]
    assert len(notes) == 1 and notes[0]['safe'], client._e_notes
    tasks = [task for task in client._e_tasks if task['late'] and url in task['description']
             and 'сверк' in task['title'].casefold() and 'мог' in task['description'].casefold()]
    assert len(tasks) == 1 and tasks[0]['safe'], client._e_tasks


@pytest.mark.parametrize('old_ok,queued_manual,stop_outcome', [
    (False, False, 'success'), (False, True, 'success'), (True, False, 'success'),
    (False, False, 'failure'), (False, False, 'cancel'),
])
def test_later_consumer_await_promotes_both_earlier_and_current_vacancies(monkeypatch, tmp_path, old_ok, queued_manual, stop_outcome):
    client, context, page, repository = synthetic_search(monkeypatch, tmp_path)
    calls, signals, tokens = [], [], []
    monkeypatch.setattr(manual_apply_queue, '_queue_path', lambda profile_name=None: tmp_path / 'queue.json')
    if queued_manual:
        capture_note = agent.notify_needs_manual.side_effect
        async def note(vacancy, *args, **kwargs):
            if vacancy['id'] == '4' and not tokens:
                assert repository.get(OLD_URL)['status'] == 'failed'
                tokens.append(CREATE_CANDIDATE(vacancy, {'score': 55, 'reason': 'Synthetic'},
                                              details='Ordinary synthetic details', profile_name='synthetic')['token'])
            await capture_note(vacancy, *args, **kwargs)
        monkeypatch.setattr(agent, 'notify_needs_manual', AsyncMock(side_effect=note))
    if stop_outcome != 'success':
        async def failed_stop():
            assert seen._load()['4']['action'] == seen._load()['5']['action'] == 'apply_uncertain'
            await asyncio.sleep(0)
            if stop_outcome == 'cancel':
                raise asyncio.CancelledError()
            return False
        client.hard_stop_browser = AsyncMock(side_effect=failed_stop)
    async def lower(vacancy, *args, **kwargs):
        calls.append(vacancy['id'])
        async def operation():
            if vacancy['id'] == '4' and old_ok:
                return {'ok': True}
            return {'ok': False, 'requires_questions': True, 'message': 'Вопросы, пропускаем'}
        return await run_native_attempt(client, repository, vacancy['url'], operation)
    async def snapshot(source, vacancy_id, *args, **kwargs):
        if vacancy_id == '5' and not signals:
            # Both NativeAttempt results were previously failed; first row
            # already reached its manual disposition before the late event.
            assert seen._load()['4']['action'] == ('applied' if old_ok else 'skipped_questions')
            late_old_post(context, page)
            signals.append(True)
            await asyncio.sleep(0)
        return {}
    monkeypatch.setattr(agent, '_save_autoapply_failure_snapshot', snapshot)
    monkeypatch.setattr(apply_orchestrator, '_dispatch_apply', lower)
    candidates = [{'id': str(vacancy_id), 'source': 'hh', 'title': f'Synthetic {vacancy_id}', 'company': 'Synthetic',
                   'url': f'https://hh.ru/vacancy/{vacancy_id}'} for vacancy_id in (4, 5, 6)]
    monkeypatch.setattr(agent.search_pipeline, 'collect_all', AsyncMock(return_value=candidates))
    cancelled = False
    try:
        result = asyncio.run(agent.do_search())
    except asyncio.CancelledError:
        cancelled = True
        result = read_json_records(config.RUN_HISTORY_FILE)[-1]
    assert signals == [True], (calls, result, seen._load())
    assert repository.get(OLD_URL)['status'] == repository.get(NEXT_URL)['status'] == 'uncertain'
    assert seen._load()['4']['action'] == seen._load()['5']['action'] == 'apply_uncertain'
    assert result['funnel']['uncertain'] == result['source_stats']['hh']['uncertain'] == 2
    assert result['applied'] == result['funnel']['applied'] == 0
    assert calls == ['4', '5'] and client._e_chat.await_count == 0
    assert [call.args[0]['id'] for call in apply_orchestrator.fetch_vacancy_details.await_args_list] == ['4', '5']
    if stop_outcome != 'success':
        assert cancelled is (stop_outcome == 'cancel')
        assert result['skipped'] == result['manual'] == result['source_stats']['hh']['manual'] == 2
        assert not [note for note in client._e_notes if note['late']], client._e_notes
        assert not [task for task in client._e_tasks if task['late']], client._e_tasks
        return
    assert '6' not in seen._load()
    assert result['source_stats']['hh']['guard_stop'] == 1
    assert result['skipped'] == 3  # Two uncertain rows and one unprocessed HH row.
    assert result['manual'] == result['source_stats']['hh']['manual'] == 2
    assert result['hh_recovery']['stopped'] and client.hard_stop_browser.await_count >= 1
    assert_manual_correction(client, '4', OLD_URL)
    assert_manual_correction(client, '5', NEXT_URL)
    if queued_manual:
        assert len(tokens) == 1 and manual_apply_queue.get_candidate(tokens[0])['status'] == 'pending'
        fresh, _, _ = client_page()
        fresh.start, fresh.stop = AsyncMock(), AsyncMock()
        fresh.is_logged_in = AsyncMock(return_value=True)
        monkeypatch.setattr(agent, 'HHClient', lambda: fresh)
        external_operations = []
        async def retry_lower(vacancy, *args, **kwargs):
            async def operation():
                external_operations.append(vacancy['id'])
                return {'ok': True}
            return await run_native_attempt(fresh, repository, vacancy['url'], operation)
        monkeypatch.setattr(apply_orchestrator, '_dispatch_apply', retry_lower)
        returned = asyncio.run(agent.do_manual_apply_token(tokens[0]))
        assert not returned['ok'] and returned['uncertain'] and external_operations == []
        assert manual_apply_queue.get_candidate(tokens[0])['status'] == 'uncertain'
        assert repository.get(OLD_URL)['status'] == 'uncertain'


def test_unknown_hh_peer_during_habr_status_preserves_habr_success(monkeypatch, tmp_path):
    client, context, page, hh_repository = synthetic_search(monkeypatch, tmp_path)
    monkeypatch.setattr(config, 'HABR_ENABLED', True)
    monkeypatch.setattr(config, 'HABR_MIN_SECONDS_BETWEEN_APPLICATIONS', 0)
    habr = SimpleNamespace(start=AsyncMock(), stop=AsyncMock(), stop_browser=AsyncMock(), _page=None,
                           is_logged_in=AsyncMock(return_value=True))
    monkeypatch.setattr(agent, 'HabrCareerClient', lambda: habr)
    habr_repository = NativeApplyRepository(tmp_path / 'habr-account' / 'cookies.json', 'habr')
    calls, signals, notified = [], [], []
    async def lower(vacancy, *args, **kwargs):
        calls.append(vacancy['id'])
        current, repository = (habr, habr_repository) if vacancy['source'] == 'habr' else (client, hh_repository)
        async def operation():
            return ({'ok': True} if vacancy['source'] == 'habr' else
                    {'ok': False, 'requires_questions': True, 'message': 'Вопросы, пропускаем'})
        return await run_native_attempt(current, repository, vacancy['url'], operation)
    async def status(action, message, status):
        if action == 'search_apply_done' and not signals:
            assert hh_repository.get(OLD_URL)['status'] == 'failed'
            late_old_post(context, page)
            signals.append(True)
            await asyncio.sleep(0)
    async def application_note(vacancy, *args, **kwargs):
        assert vacancy['source'] == 'habr'
        assert client.hard_stop_browser.await_count >= 1, 'HH shutdown must precede the next outcome notification'
        notified.append(vacancy['id'])
    monkeypatch.setattr(agent, 'office_log', status)
    monkeypatch.setattr(agent, 'notify_application', AsyncMock(side_effect=application_note))
    monkeypatch.setattr(apply_orchestrator, '_dispatch_apply', lower)
    candidates = [
        {'id': '4', 'source': 'hh', 'title': 'Synthetic HH 4', 'company': 'Synthetic', 'url': OLD_URL},
        {'id': '5', 'source': 'habr', 'title': 'Synthetic Habr 5', 'company': 'Synthetic',
         'url': 'https://career.habr.com/vacancies/5'},
        {'id': '6', 'source': 'hh', 'title': 'Synthetic HH 6', 'company': 'Synthetic',
         'url': 'https://hh.ru/vacancy/6'},
    ]
    monkeypatch.setattr(agent.search_pipeline, 'collect_all', AsyncMock(return_value=candidates))
    result = asyncio.run(agent.do_search())
    assert signals == [True] and notified == ['5']
    assert hh_repository.get(OLD_URL)['status'] == 'uncertain'
    assert habr_repository.get(candidates[1]['url'])['status'] == 'completed'
    assert seen._load()['4']['action'] == 'apply_uncertain' and seen._load()['5']['action'] == 'applied'
    assert result['applied'] == result['funnel']['applied'] == 1
    assert result['source_stats']['hh']['uncertain'] == 1
    assert result['source_stats']['habr']['applied'] == 1
    assert not result['source_stats']['habr'].get('uncertain')
    assert '6' not in seen._load()
    assert result['source_stats']['hh']['guard_stop'] == 1
    assert result['skipped'] == 2  # One uncertain row and one unprocessed HH row.
    assert result['manual'] == result['source_stats']['hh']['manual'] == 1
    assert result['hh_recovery']['stopped'] and calls == ['4', '5']
    assert_manual_correction(client, '4', OLD_URL)
    assert client._e_chat.await_count == 0


def test_prior_success_and_questions_get_one_correction_after_normal_owned_cleanup(monkeypatch, tmp_path):
    from hh.browser import CookieBinding, stop_browser
    from state_store.hh_cookies import HHCookieRepository
    client, context, page, repository = synthetic_search(monkeypatch, tmp_path)
    monkeypatch.setattr(config, 'HH_CHAT_RESPONDER_ENABLED', False)
    browser = SimpleNamespace(contexts=[context], _hh_owner_session=client)
    context.browser = browser
    client._browser, client._pw = browser, SimpleNamespace(stop=AsyncMock())
    cookies_repository = HHCookieRepository(tmp_path / 'cookies.json')
    _, revision = cookies_repository.snapshot()
    client._cookie_binding = CookieBinding(cookies_repository, revision, False, context=context)
    signals = []
    async def cookies():
        assert repository.get(OLD_URL)['status'] == 'completed'
        assert repository.get(NEXT_URL)['status'] == 'failed'
        late_old_post(context, page)
        signals.append(True)
        await asyncio.sleep(0)
        return []
    context.cookies = AsyncMock(side_effect=cookies)
    async def terminate(owned_browser):
        assert owned_browser is browser
        return True
    async def stop():
        return await stop_browser(client, terminate=terminate)
    client.stop = AsyncMock(side_effect=stop)
    async def lower(vacancy, *args, **kwargs):
        async def operation():
            return ({'ok': True} if vacancy['id'] == '4' else
                    {'ok': False, 'requires_questions': True, 'message': 'Вопросы, пропускаем'})
        return await run_native_attempt(client, repository, vacancy['url'], operation)
    monkeypatch.setattr(apply_orchestrator, '_dispatch_apply', lower)
    candidates = [{'id': str(vacancy_id), 'source': 'hh', 'title': f'Synthetic {vacancy_id}',
                   'company': 'Synthetic', 'url': f'https://hh.ru/vacancy/{vacancy_id}'} for vacancy_id in (4, 5)]
    monkeypatch.setattr(agent.search_pipeline, 'collect_all', AsyncMock(return_value=candidates))
    result = asyncio.run(agent.do_search())
    assert signals == [True] and client._page is None and client._context is None
    context.cookies.assert_awaited_once()
    assert repository.get(OLD_URL)['status'] == repository.get(NEXT_URL)['status'] == 'uncertain'
    assert seen._load()['4']['action'] == seen._load()['5']['action'] == 'apply_uncertain'
    assert result['applied'] == result['funnel']['applied'] == 0
    assert result['skipped'] == result['manual'] == result['source_stats']['hh']['manual'] == 2
    assert result['funnel']['uncertain'] == result['source_stats']['hh']['uncertain'] == 2
    assert_manual_correction(client, '4', OLD_URL)
    assert_manual_correction(client, '5', NEXT_URL)
