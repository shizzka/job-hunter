"""A vacancy-specific access denial is terminal before matching or apply intent."""
import asyncio
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import agent
import config
import seen
from hh import resume_refresh
from hh.recovery import ContextActions, PageActions
from hh.vacancy_details import HHVacancyNavigationError
from hh_client import HHClient
from state_store.native_apply import NativeAttempt, NativeApplyRepository
from state_store.private_journal import read_json_records
from tests.test_observability import configure_synthetic_search

ORIGINAL_FETCH_DETAILS = agent.apply_orchestrator.fetch_vacancy_details


class SyntheticPage:
    """Hashable page identity for real session action ownership checks."""


@pytest.fixture
def forbidden_pipeline(tmp_path, monkeypatch):
    def deny_network(*args, **kwargs):
        raise AssertionError('Forbidden pipeline regression must remain offline')
    monkeypatch.setattr(socket.socket, 'connect', deny_network)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny_network)
    for key, name in {
        'JOB_HUNTER_HOME': '', 'RUN_HISTORY_FILE': 'runs.jsonl',
        'RUNTIME_STATUS_FILE': 'runtime.json', 'SEEN_VACANCIES_FILE': 'seen.json',
        'HH_STATE_DIR': 'hh', 'HH_GUARD_STATE_FILE': 'guard.json',
        'ANALYTICS_EVENTS_FILE': 'events.jsonl', 'ANALYTICS_STATE_FILE': 'analytics.json',
        'HH_RESUME_PIPELINE_FILE': 'pipeline.json', 'HH_COOKIES_FILE': 'cookies.json',
    }.items():
        monkeypatch.setattr(config, key, str(tmp_path / name))
    monkeypatch.setattr(config, 'ANALYTICS_ENABLED', True)
    configure_synthetic_search(monkeypatch, tmp_path)
    # Restore real handling after the shared harness stubs it: swallowing the
    # transport failure is the original bug under test.
    monkeypatch.setattr(agent.apply_orchestrator, 'fetch_vacancy_details', ORIGINAL_FETCH_DETAILS)
    monkeypatch.setattr(agent.search_pipeline.filters, 'check_vacancy', lambda vacancy: None)
    client = HHClient()
    page = SyntheticPage()
    page.main_frame = SimpleNamespace(url='about:blank')
    page.frames, page.workers = [page.main_frame], []
    page.is_closed = lambda: False
    page.url = 'about:blank'
    context = SimpleNamespace(request=object(), service_workers=[], pages=[page])
    page.context = context
    client._page, client._context, client._ui_guard = page, context, object()
    page._hh_passive_document_epoch = {'owner': client, 'value': 1}
    monitor = PageActions(client, page)
    watch = ContextActions(client, context, pages={page: monitor})
    page._hh_action_monitor = monitor
    context._hh_action_watch = watch
    context._hh_workers_owner = client
    client._hh_blocked_worker_context = context
    context.cookies = AsyncMock(return_value=[{
        'name': 'hhtoken', 'value': 'synthetic-only', 'domain': '.hh.ru',
        'path': '/', 'expires': -1, 'secure': True, 'httpOnly': True, 'sameSite': 'Lax',
    }])
    client.start, client.stop = AsyncMock(), AsyncMock()
    client.hard_stop_browser = AsyncMock(return_value=True)
    client.is_logged_in = AsyncMock(return_value=True)
    client.get_negotiation_statuses = AsyncMock(return_value=[])
    client._ensure_expected_ui = AsyncMock()
    monkeypatch.setattr(agent, 'HHClient', lambda: client)
    # Fresh read-only authenticated catalog, independent of startup login.
    health = AsyncMock(return_value=[])
    monkeypatch.setattr(resume_refresh, 'get_resume_ids_readonly', health)
    vacancies = [{
        'id': str(vid), 'source': 'hh', 'source_label': 'hh.ru',
        'title': f'QA {vid}', 'company': f'Synthetic {vid}',
        'url': f'https://hh.ru/vacancy/{vid}',
    } for vid in (101, 102, 103)]
    monkeypatch.setattr(agent.search_pipeline, 'collect_all', AsyncMock(return_value=vacancies))
    matched, covered, applied, details_read = [], [], [], []
    denied_ids, error_overrides = {'101'}, {}
    document = SimpleNamespace(proven=True, url_override=None, marker=None)
    marker_locator = SimpleNamespace(count=AsyncMock(return_value=1))

    async def marker_content(attribute, **kwargs):
        assert attribute == 'content'
        return document.marker

    marker_locator.get_attribute = AsyncMock(side_effect=marker_content)

    def locate_marker(selector):
        assert selector == 'meta[data-jh-vacancy-forbidden]'
        return marker_locator

    page.locator = locate_marker

    async def details(url):
        details_read.append(url.rsplit('/', 1)[-1])
        vid = url.rsplit('/', 1)[-1]
        if vid in denied_ids:
            error = HHVacancyNavigationError(url,
                response_url=f'https://spb.hh.ru/vacancy/{vid}', status=403,
                content_type='text/html', redirect_chain=[
                    {'url': url, 'status': 302},
                    {'url': f'https://spb.hh.ru/vacancy/{vid}', 'status': 403},
                ])
            error.metadata['method'] = 'GET'
            error.metadata.update(error_overrides)
            page.url = document.url_override if document.url_override is not None else url
            page.main_frame.url = page.url
            page._hh_passive_document_epoch['value'] += 1
            if document.proven:
                token = f'synthetic-inert-{vid}'
                error._passive_document_token = token
                document.marker = token
            else:
                document.marker = None
            raise error
        return 'Test Python services and browser lifecycle.'
    client.get_vacancy_details = AsyncMock(side_effect=details)

    async def evaluate(vacancy, details):
        matched.append(vacancy['id'])
        return {'score': 90, 'reason': 'Synthetic', 'should_apply': True, 'red_flags': []}

    async def cover(vacancy, *args, **kwargs):
        covered.append(vacancy['id'])
        return 'Synthetic cover'

    async def dispatch(vacancy, *args, **kwargs):
        applied.append(vacancy['id'])
        if vacancy['id'] == '101':
            # Original swallowed details enter apply and a second navigation
            # fails, halting all later candidates for this HH source.
            return {'ok': False, 'hh_hard_stop': True, 'uncertain': False,
                'hh_recovery_reason': 'vacancy_navigation_failed',
                'hh_navigation_failure': {
                    'stage': 'VACANCY_NAVIGATION', 'reason_code': 'vacancy_navigation_failed',
                    'status': 403, 'response_origin': 'https://spb.hh.ru',
                }}
        return {'ok': True}
    monkeypatch.setattr(agent, 'evaluate_vacancy', evaluate)
    monkeypatch.setattr(agent, 'generate_cover_letter', cover)
    monkeypatch.setattr(agent.apply_orchestrator, 'dispatch_apply', dispatch)
    return SimpleNamespace(client=client, health=health, vacancies=vacancies,
        matched=matched, covered=covered, applied=applied, details_read=details_read,
        denied_ids=denied_ids, error_overrides=error_overrides, document=document)


def test_first_preapply_get_403_is_skipped_and_next_two_hh_vacancies_continue(forbidden_pipeline):
    fake = forbidden_pipeline
    result = asyncio.run(agent.do_search())
    events = read_json_records(config.ANALYTICS_EVENTS_FILE)
    decisions = [event for event in events if event.get('event') == 'decision']
    # These prove no denied-vacancy matcher, cover generation, or apply intent.
    assert fake.matched == ['102', '103']
    assert fake.covered == ['102', '103']
    assert fake.applied == ['102', '103']
    assert fake.details_read == ['101', '102', '103']
    fake.health.assert_awaited_once_with(fake.client)
    assert result['applied'] == 2 and result['skipped'] == 1
    assert result['hh_recovery']['stopped'] is False
    fake.client.hard_stop_browser.assert_not_awaited()
    assert result['funnel']['not_processed'] == 0
    assert result['reason_breakdown']['vacancy_forbidden'] == 1
    assert 'apply_uncertain' not in result['reason_breakdown']
    assert seen.all_entries()['101']['action'] == 'skipped_unavailable'
    denied = [event for event in decisions if event.get('vacancy_id') == '101']
    assert len(denied) == 1
    assert denied[0]['decision'] == 'skipped_unavailable'
    assert denied[0]['reason_code'] == 'vacancy_forbidden'
    assert not any(event.get('decision') in {'not_processed', 'apply_uncertain', 'guard_stop'}
                   for event in decisions)


def assert_preapply_source_stopped(fake, result, *, uncertain=False):
    decisions = [event for event in read_json_records(config.ANALYTICS_EVENTS_FILE)
                 if event.get('event') == 'decision']
    assert result['hh_recovery']['stopped'] is True
    assert result['applied'] == 0
    assert result['funnel']['not_processed'] == 2
    assert fake.matched == fake.covered == fake.applied == []
    assert fake.details_read == ['101']
    fake.client.hard_stop_browser.assert_awaited()
    first = [event for event in decisions if event.get('vacancy_id') == '101']
    assert len(first) == 1
    assert first[0]['decision'] == ('apply_uncertain' if uncertain else 'guard_stop')
    assert not any(event.get('decision') == 'skipped_unavailable' for event in decisions)
    assert ('apply_uncertain' in result['reason_breakdown']) is uncertain
    assert 'vacancy_forbidden' not in result['reason_breakdown']
    assert seen.all_entries()['101']['action'] == ('apply_uncertain' if uncertain else 'manual_hh_guard_stop')
    assert not any(vid in seen.all_entries() for vid in ('102', '103'))


def test_vacancy_403_with_auth_catalog_http_403_stops_hh(forbidden_pipeline):
    fake = forbidden_pipeline
    fake.health.side_effect = ValueError('Synthetic authenticated catalog HTTP 403')
    result = asyncio.run(agent.do_search())
    assert_preapply_source_stopped(fake, result)
    fake.health.assert_awaited_once_with(fake.client)
    assert fake.client._hh_recovery_stop_reason == 'vacancy_forbidden_auth_unproven'


def test_vacancy_403_with_changed_auth_cookie_stops_hh(forbidden_pipeline):
    fake = forbidden_pipeline
    before = fake.client._context.cookies.return_value
    after = [dict(before[0], value='synthetic-replaced')]
    fake.client._context.cookies.side_effect = [before, after]
    result = asyncio.run(agent.do_search())
    assert_preapply_source_stopped(fake, result)
    fake.health.assert_awaited_once_with(fake.client)
    assert fake.client._hh_recovery_stop_reason == 'auth_cookie_changed'


@pytest.mark.parametrize('metadata', [
    {'response_url': 'https://spb.hh.ru/vacancy/999'},
    {'response_url': 'https://hh.ru/applicant/resumes'},
    {'response_url': 'https://untrusted.invalid/vacancy/101'},
    {'requested_url': 'https://hh.ru/vacancy/999'},
    {'method': 'POST'},
], ids=['other-vacancy', 'auth-endpoint', 'foreign-origin', 'wrong-request', 'non-get'])
def test_unproven_403_scope_stops_hh(forbidden_pipeline, metadata):
    fake = forbidden_pipeline
    fake.error_overrides.update(metadata)
    result = asyncio.run(agent.do_search())
    assert_preapply_source_stopped(fake, result)
    fake.health.assert_not_awaited()
    assert fake.client._hh_recovery_stop_reason == 'vacancy_forbidden_scope_unproven'


@pytest.mark.parametrize('evidence', ['watch', 'monitor', 'native-attempt'])
def test_vacancy_403_preserves_actual_action_uncertainty(forbidden_pipeline, evidence):
    fake = forbidden_pipeline
    if evidence == 'watch':
        fake.client._context._hh_action_watch.unknown = True
    elif evidence == 'monitor':
        fake.client._page._hh_action_monitor.unknown = True
    else:
        repository = NativeApplyRepository(fake.client._cookie_paths.cookies_file, 'hh')
        attempt = NativeAttempt(repository, fake.vacancies[0]['url'], 'synthetic-owner', client=fake.client)
        attempt.uncertain = True
        fake.client._external_attempt = attempt
    result = asyncio.run(agent.do_search())
    assert_preapply_source_stopped(fake, result, uncertain=True)
    fake.health.assert_not_awaited()


def test_three_consecutive_distinct_vacancy_403s_halt_source(forbidden_pipeline, monkeypatch):
    fake = forbidden_pipeline
    fourth = dict(fake.vacancies[-1], id='104', title='QA 104', url='https://hh.ru/vacancy/104')
    monkeypatch.setattr(agent.search_pipeline, 'collect_all', AsyncMock(return_value=fake.vacancies + [fourth]))
    fake.denied_ids.update({'102', '103'})
    result = asyncio.run(agent.do_search())
    decisions = [event for event in read_json_records(config.ANALYTICS_EVENTS_FILE)
                 if event.get('event') == 'decision']
    assert fake.details_read == ['101', '102', '103']
    assert fake.matched == fake.covered == fake.applied == []
    assert fake.health.await_count == 2
    assert result['hh_recovery']['stopped'] is True
    assert result['reason_breakdown']['vacancy_forbidden'] == 2
    assert result['funnel']['not_processed'] == 1
    assert 'apply_uncertain' not in result['reason_breakdown']
    assert fake.client._hh_recovery_stop_reason == 'repeated_vacancy_forbidden'
    assert {event['vacancy_id']: event['decision'] for event in decisions} == {
        '101': 'skipped_unavailable', '102': 'skipped_unavailable',
        '103': 'guard_stop', '104': 'not_processed',
    }


def test_successful_details_between_403s_resets_consecutive_denial_limit(forbidden_pipeline, monkeypatch):
    fake = forbidden_pipeline
    vacancies = [dict(fake.vacancies[0], id=str(vid), title=f'QA {vid}',
                      url=f'https://hh.ru/vacancy/{vid}') for vid in range(101, 106)]
    monkeypatch.setattr(agent.search_pipeline, 'collect_all', AsyncMock(return_value=vacancies))
    fake.denied_ids.update({'102', '104', '105'})
    result = asyncio.run(agent.do_search())
    assert fake.details_read == ['101', '102', '103', '104', '105']
    assert fake.matched == fake.covered == fake.applied == ['103']
    assert fake.health.await_count == 4
    assert result['applied'] == 1
    assert result['reason_breakdown']['vacancy_forbidden'] == 4
    assert result['hh_recovery']['stopped'] is False
    assert result['funnel']['not_processed'] == 0
    assert 'apply_uncertain' not in result['reason_breakdown']
    fake.client.hard_stop_browser.assert_not_awaited()


def test_vacancy_403_without_authenticated_cookie_proof_stops_hh(forbidden_pipeline):
    fake = forbidden_pipeline
    fake.client._context.cookies.return_value = []
    result = asyncio.run(agent.do_search())
    assert_preapply_source_stopped(fake, result)
    fake.health.assert_not_awaited()
    assert fake.client._hh_recovery_stop_reason == 'vacancy_forbidden_auth_unproven'


def test_vacancy_403_after_submit_boundary_never_becomes_unavailable(forbidden_pipeline):
    fake = forbidden_pipeline
    fake.client._submit_boundary_id = 'synthetic-existing-boundary'
    result = asyncio.run(agent.do_search())
    assert_preapply_source_stopped(fake, result)
    fake.health.assert_not_awaited()
    assert fake.client._hh_recovery_stop_reason == 'vacancy_forbidden_action_boundary_unproven'


@pytest.mark.parametrize('status', [401, 404, 429, 500])
def test_non_403_transport_error_cannot_be_classified_as_vacancy_forbidden(forbidden_pipeline, status):
    from hh.vacancy_details import classify_vacancy_forbidden
    fake = forbidden_pipeline
    url = fake.vacancies[0]['url']
    error = HHVacancyNavigationError(url, response_url=url, status=status,
                                    content_type='text/html', method='GET')
    assert asyncio.run(classify_vacancy_forbidden(fake.client, error, url)) is False
    fake.health.assert_not_awaited()
    fake.client._context.cookies.assert_not_awaited()
    assert not getattr(fake.client, '_hh_recovery_stop_reason', '')


@pytest.mark.parametrize('current_url', [
    'about:blank', 'https://hh.ru/applicant/vacancy_response?vacancyId=101',
    'chrome-error://chromewebdata/', 'https://hh.ru/vacancy/101',
], ids=['blank', 'active-response', 'chrome-error', 'vacancy-without-inert-proof'])
def test_unproven_current_document_never_becomes_vacancy_unavailable(forbidden_pipeline, current_url):
    fake = forbidden_pipeline
    fake.document.url_override = current_url
    if current_url == 'https://hh.ru/vacancy/101':
        fake.document.proven = False
    result = asyncio.run(agent.do_search())
    assert_preapply_source_stopped(fake, result)
    fake.health.assert_not_awaited()


@pytest.mark.parametrize('field', ['uncertain', 'acting', 'command_started'])
def test_retained_native_action_evidence_never_becomes_vacancy_unavailable(forbidden_pipeline, field):
    fake = forbidden_pipeline
    repository = NativeApplyRepository(fake.client._cookie_paths.cookies_file, 'hh')
    attempt = NativeAttempt(repository, fake.vacancies[0]['url'], 'retained-owner', client=fake.client)
    setattr(attempt, field, True)
    fake.client._page._hh_action_monitor.last_attempt = attempt
    assert getattr(fake.client, '_external_attempt', None) is None
    result = asyncio.run(agent.do_search())
    assert_preapply_source_stopped(fake, result, uncertain=True)
    fake.health.assert_not_awaited()


@pytest.mark.parametrize('when', ['before-route', 'during-route'])
@pytest.mark.parametrize('tamper', ['url', 'marker', 'epoch'])
def test_next_navigation_rechecks_owned_inert_denial_document(forbidden_pipeline, when, tamper):
    from hh.ui import HHUnexpectedUI
    from hh.vacancy_details import classify_vacancy_forbidden, passive_details_navigation
    fake = forbidden_pipeline
    page = fake.client._page
    url = fake.vacancies[0]['url']
    token = 'synthetic-stale-proof'
    error = HHVacancyNavigationError(url, response_url=url, status=403,
                                    content_type='text/html', method='GET')
    error._passive_document_token = token
    page.url = page.main_frame.url = url
    fake.document.marker = token

    def mutate():
        if tamper == 'url':
            page.url = page.main_frame.url = 'https://hh.ru/applicant/vacancy_response?vacancyId=101'
        elif tamper == 'marker':
            fake.document.marker = 'synthetic-unowned-document'
        else:
            page._hh_passive_document_epoch['value'] += 1

    async def install(*args, **kwargs):
        if when == 'during-route':
            mutate()
    page.route = AsyncMock(side_effect=install)
    page.unroute = AsyncMock()
    page.goto = AsyncMock()

    async def check():
        assert await classify_vacancy_forbidden(fake.client, error, url) is True
        assert callable(fake.client._hh_forbidden_document_proof)
        if when == 'before-route':
            mutate()
        with pytest.raises(HHUnexpectedUI) as raised:
            async with passive_details_navigation(fake.client, fake.vacancies[1]['url']):
                await page.goto(fake.vacancies[1]['url'])
        assert raised.value.hh_stop_reason == 'vacancy_forbidden_document_unproven'
        assert raised.value.hh_uncertain is False
        page.goto.assert_not_awaited()
        if when == 'before-route':
            page.route.assert_not_awaited()
            page.unroute.assert_not_awaited()
        else:
            page.route.assert_awaited_once()
            page.unroute.assert_awaited_once()
    asyncio.run(check())



def test_same_url_document_navigation_during_marker_read_stops_before_matching(forbidden_pipeline):
    fake = forbidden_pipeline
    page = fake.client._page
    locator = page.locator('meta[data-jh-vacancy-forbidden]')
    original_read = locator.get_attribute

    async def navigate_while_reading(attribute, **kwargs):
        marker = await original_read(attribute, **kwargs)
        # A replacement document retains the same URL and a stale locator can
        # still return the old matching marker. Epoch is independent evidence.
        page._hh_passive_document_epoch['value'] += 1
        assert page.url == page.main_frame.url == fake.vacancies[0]['url']
        return marker
    locator.get_attribute = AsyncMock(side_effect=navigate_while_reading)
    result = asyncio.run(agent.do_search())
    assert_preapply_source_stopped(fake, result)
    fake.health.assert_not_awaited()
