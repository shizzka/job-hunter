"""Offline startup regression for search-20261011T000123-850241-6b575a5e486b."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

import agent
import analytics
import config
from browser_action_boundary import bootstrap_boundary
from tests.test_hh_search_session import retain_unproven_attempt
from hh.recovery import BLOCK_WORKER_CONSTRUCTORS, monitor_page
from hh.ui import HHUIGuard, HHUnexpectedUI
from hh_client import HHClient
from tests.test_observability import configure_synthetic_search

URL = 'https://hh.ru/applicant/negotiations'
HTML = (Path(__file__).parent / 'fixtures/hh_negotiations_background.html').read_text()
CATALOG_URL = 'https://hh.ru/applicant/resumes'
CATALOG_HTML = (Path(__file__).parent / 'fixtures/hh_resume_catalog_background.html').read_text()
EXPECTED = [{'id': '123', 'status': 'Просмотрен', 'title': 'QA Engineer',
             'company': 'Synthetic employer', 'url': 'https://hh.ru/vacancy/123?from=negotiations'}]


async def scenario(tmp_path, check):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=[
            '--disable-background-networking', '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost'])
        try:
            context = await browser.new_context(service_workers='block')
            await context.add_init_script(script=BLOCK_WORKER_CONSTRUCTORS)
            requests = []
            async def route(item):
                requests.append((item.request.method, item.request.url))
                if item.request.url in {URL, CATALOG_URL}:
                    body = HTML if item.request.url == URL else CATALOG_HTML
                    await item.fulfill(status=200, content_type='text/html', body=body)
                else:
                    await item.abort()
            await context.route('**/*', route)
            response = SimpleNamespace(status=200, url=URL, headers={'content-type': 'text/html'},
                                      text=AsyncMock(return_value=HTML), dispose=AsyncMock())
            # APIRequest bypasses Page routes; stub it separately to keep all I/O offline.
            catalog_response = SimpleNamespace(status=200, url=CATALOG_URL, headers={'content-type': 'text/html'},
                                               text=AsyncMock(return_value=CATALOG_HTML), dispose=AsyncMock())
            async def get(url, **kwargs):
                assert url in {URL, CATALOG_URL}
                return response if url == URL else catalog_response
            context.request.get = AsyncMock(side_effect=get)
            client = HHClient()
            client._context = context
            client._page = await context.new_page()
            client._ui_guard = HHUIGuard(tmp_path, notify=AsyncMock(return_value=False))
            monitor_page(client, client._page)
            await bootstrap_boundary(client._page)
            await check(client, requests, response)
        finally:
            await browser.close()


def test_negotiation_statuses_do_not_execute_background_events(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'HH_BASE_URL', 'https://hh.ru')
    async def check(client, requests, response):
        assert await client.get_negotiation_statuses() == EXPECTED
        assert requests == [] and client._page.url == 'about:blank'
        assert not client._page._hh_action_monitor.unknown
        assert not client._context._hh_action_watch.unknown
        client._context.request.get.assert_awaited_once_with(URL, max_redirects=0, timeout=20000)
        response.dispose.assert_awaited_once()
    asyncio.run(scenario(tmp_path, check))


@pytest.mark.parametrize('staged_resumes', [False, True])
def test_blacklisted_first_candidate_does_not_leave_102_hh_candidates_unprocessed(tmp_path, monkeypatch, staged_resumes):
    for key, name in {
        'JOB_HUNTER_HOME': '', 'RUN_HISTORY_FILE': 'runs.jsonl', 'RUNTIME_STATUS_FILE': 'runtime.json',
        'SEEN_VACANCIES_FILE': 'seen.json', 'HH_STATE_DIR': 'hh', 'HH_GUARD_STATE_FILE': 'guard.json',
        'ANALYTICS_EVENTS_FILE': 'events.jsonl', 'ANALYTICS_STATE_FILE': 'analytics.json',
        'HH_RESUME_PIPELINE_FILE': 'resume-pipeline.json',
    }.items():
        monkeypatch.setattr(config, key, str(tmp_path / name))
    configure_synthetic_search(monkeypatch, tmp_path)
    monkeypatch.setattr(agent.hh_pipeline, 'enabled', lambda: staged_resumes)
    monkeypatch.setattr(agent.hh_pipeline, 'get_variants', lambda: [
        {'name': 'normal', 'id': '', 'title': 'Synthetic resume'}])
    monkeypatch.setattr(config, 'HH_BASE_URL', 'https://hh.ru')
    monkeypatch.setattr(config, 'HABR_ENABLED', True)
    monkeypatch.setattr(agent, 'HabrCareerClient', lambda: SimpleNamespace(stop=AsyncMock(), stop_browser=AsyncMock()))
    monkeypatch.setattr(agent.search_pipeline.filters, 'check_vacancy', lambda v: None)
    monkeypatch.setattr(agent.company_blacklist, 'is_blocked', lambda c: c == 'Blocked employer')
    vacancies = [{'id': 'habr:first', 'source': 'habr', 'title': 'QA', 'company': 'Blocked employer'}] + [
        {'id': str(i), 'source': 'hh', 'title': f'QA {i}', 'company': f'Employer {i}'} for i in range(102)]
    queue = agent.MatcherDeferredQueue(tmp_path, clock=lambda: 1000)
    for vacancy in vacancies[1:]:
        queue.defer(vacancy, 'Synthetic saved details', 'llm_limits_exhausted')
    async def collect(*args, **kwargs):
        return vacancies[:1]
    monkeypatch.setattr(agent.search_pipeline, 'collect_all', collect)
    evaluate = AsyncMock(return_value={'score': 10, 'reason': 'Synthetic low score',
                                     'should_apply': False, 'red_flags': []})
    monkeypatch.setattr(agent, 'evaluate_vacancy', evaluate)
    dispatch = AsyncMock(side_effect=AssertionError('No submission in regression'))
    monkeypatch.setattr(agent.apply_orchestrator, 'dispatch_apply', dispatch)
    async def check(client, requests, response):
        client.start = AsyncMock()
        client.stop = AsyncMock()
        client.hard_stop_browser = AsyncMock(return_value=True)
        client.is_logged_in = AsyncMock(return_value=True)
        monkeypatch.setattr(agent, 'HHClient', lambda: client)
        with analytics.observe_search('synthetic-startup-regression', 'search'):
            result = await agent.do_search()
        # On the broken tree these are stopped=True, 0 evaluations, and 102 not_processed.
        assert result['hh_recovery']['stopped'] is False
        assert evaluate.await_count == 102
        assert result['funnel']['keyword_pass'] == 103
        assert result['funnel']['evaluated'] == 102
        assert result['funnel']['not_processed'] == 0
        assert result['reason_breakdown'] == {'company_blacklisted': 1, 'low_score': 102}
        dispatch.assert_not_awaited()
        agent.generate_cover_letter.assert_not_awaited()
        assert requests == []
        assert queue.store.load()['items'] == {}
        if staged_resumes:
            assert agent.hh_pipeline.get_resolved_variants()[0]['id'] == 'synthetic'
    asyncio.run(scenario(tmp_path, check))


@pytest.mark.parametrize('during', ['before', 'get', 'text', 'dispose'])
def test_negotiations_preserve_existing_or_new_uncertainty(tmp_path, during):
    async def check(client, requests, response):
        await retain_unproven_attempt(client, tmp_path)
        if during == 'before':
            client._page._hh_action_monitor.mark_unknown()
        else:
            async def poison(*args, **kwargs):
                client._page._hh_action_monitor.mark_unknown()
                return response if during == 'get' else HTML
            if during == 'get':
                client._context.request.get = AsyncMock(side_effect=poison)
            elif during == 'text':
                response.text = AsyncMock(side_effect=poison)
            else:
                response.dispose = AsyncMock(side_effect=poison)
        with pytest.raises(HHUnexpectedUI) as raised:
            await client.get_negotiation_statuses()
        assert raised.value.hh_stop_reason == 'possible_external_action'
        assert raised.value.hh_uncertain is True
        assert client._context._hh_action_watch.unknown
        assert client._context.request.get.await_count == (0 if during == 'before' else 1)
        assert requests == []
    asyncio.run(scenario(tmp_path, check))


@pytest.mark.parametrize('owner', ['page', 'context', 'request_context'])
def test_negotiations_reject_ownership_change(tmp_path, owner):
    async def check(client, requests, response):
        context = client._context
        async def replace(*args, **kwargs):
            if owner == 'page':
                client._page = object()
            elif owner == 'context':
                client._context = object()
            else:
                context._impl_obj._request = object()
            return response
        context.request.get = AsyncMock(side_effect=replace)
        with pytest.raises(HHUnexpectedUI) as raised:
            await client.get_negotiation_statuses()
        assert raised.value.hh_stop_reason == 'browser_ownership_changed'
        assert raised.value.hh_uncertain is True
        response.text.assert_not_awaited()
        response.dispose.assert_awaited_once()
        assert requests == []
    asyncio.run(scenario(tmp_path, check))


@pytest.mark.parametrize('selector', [
    'data-qa="negotiations-item"', 'class="negotiations-item"', 'class="resume-negotiations-item"',
])
def test_negotiation_parser_preserves_existing_card_selectors(selector):
    from hh.negotiations import parse_negotiation_statuses
    body = HTML.replace('data-qa="negotiations-item"', selector)
    assert parse_negotiation_statuses(body, base_url=URL) == EXPECTED


@pytest.mark.parametrize('body', [
    '<div data-qa="captcha">Challenge</div>',
    '<div role="dialog">Unknown dialog</div>',
    '<dialog open>Unknown dialog</dialog>',
])
def test_negotiation_parser_rejects_challenges_and_unknown_dialogs(body):
    from hh.negotiations import parse_negotiation_statuses
    with pytest.raises(ValueError):
        parse_negotiation_statuses(HTML + body, base_url=URL)


@pytest.mark.parametrize('change', ['login_redirect', 'anonymous', 'status', 'content_type'])
def test_negotiations_fail_closed_on_unproven_server_response(change):
    from hh.negotiations import read_negotiation_statuses
    response = SimpleNamespace(status=200, url=URL, headers={'content-type': 'text/html'},
                              text=AsyncMock(return_value=HTML), dispose=AsyncMock())
    if change == 'login_redirect':
        response.status = 302
        response.headers['location'] = '/account/login'
    elif change == 'anonymous':
        response.text = AsyncMock(return_value='<script>{"userType":"anonymous"}</script>')
    elif change == 'status':
        response.status = 503
    else:
        response.headers['content-type'] = 'application/json'
    context = SimpleNamespace(request=SimpleNamespace(get=AsyncMock(return_value=response)))
    client = SimpleNamespace(_context=context, _page=object(), _ensure_expected_ui=AsyncMock())
    with pytest.raises(ValueError):
        asyncio.run(read_negotiation_statuses(client, base_url='https://hh.ru'))
    response.dispose.assert_awaited_once()
    assert context.request.get.await_count == 1
