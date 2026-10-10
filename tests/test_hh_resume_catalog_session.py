"""Observed catalog background events; every browser request stays offline."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

import config
from browser_action_boundary import bootstrap_boundary
from hh.recovery import BLOCK_WORKER_CONSTRUCTORS, monitor_page
from hh.ui import HHUIGuard, HHUnexpectedUI
from hh_client import HHClient

CATALOG = (Path(__file__).parent / 'fixtures/hh_resume_catalog_background.html').read_text()
URL = 'https://hh.ru/applicant/resumes'
PROFILE = 'https://spb.hh.ru/applicant/profile/me'


async def scenario(tmp_path, check, body=CATALOG):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=[
            '--disable-background-networking', '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost'])
        try:
            context = await browser.new_context(service_workers='block')
            await context.add_init_script(script=BLOCK_WORKER_CONSTRUCTORS)
            requests = []
            async def route(item):
                requests.append((item.request.method, item.request.url))
                if item.request.url == URL:
                    await item.fulfill(status=200, content_type='text/html', body=body)
                else:
                    await item.abort()
            await context.route('**/*', route)
            response = SimpleNamespace(status=200, url=PROFILE,
                headers={'content-type': 'text/html'}, text=AsyncMock(return_value=body), dispose=AsyncMock())
            # APIRequest does not use Page routes: stub it too, with no live network.
            context.request.get = AsyncMock(return_value=response)
            client = HHClient()
            client._context = context
            client._page = await context.new_page()
            client._ui_guard = HHUIGuard(tmp_path, notify=AsyncMock(return_value=False))
            monitor_page(client, client._page)
            await bootstrap_boundary(client._page)
            await check(client, requests, response)
        finally:
            await browser.close()


@pytest.mark.parametrize('source', ['frameattached', 'telemetry_post'])
def test_observed_catalog_events_remain_possible_external_action(tmp_path, source):
    body = CATALOG if source == 'frameattached' else '''<a data-qa="resume-card-link-synthetic">Synthetic</a>
    <script>fetch('/anatskytics', {method:'POST', body:'synthetic'}).catch(()=>{});</script>'''
    async def check(client, requests, response):
        observed = asyncio.Event()
        client._context.on('request', lambda r: observed.set() if r.method == 'POST' else None)
        client._page.on('frameattached', lambda f: observed.set())
        await client._page.goto(URL)
        await asyncio.wait_for(observed.wait(), 5)
        with pytest.raises(HHUnexpectedUI) as raised:
            await client._dismiss_whats_new_modal()
        assert raised.value.stage == 'resume_catalog'
        assert raised.value.hh_stop_reason == 'possible_external_action'
        assert raised.value.hh_uncertain is True
        assert client._page._hh_action_monitor.unknown
        assert client._context._hh_action_watch.unknown
        assert getattr(client, '_external_attempt', None) is None
        if source == 'frameattached':
            assert await client._page.locator('#mts-pixel').count() == 1
        else:
            assert ('POST', 'https://hh.ru/anatskytics') in requests
    asyncio.run(scenario(tmp_path, check, body))


def test_session_proof_reads_observed_catalog_without_loading_its_scripts(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'HH_BASE_URL', 'https://hh.ru')
    async def check(client, requests, response):
        assert await client.is_logged_in() is True
        assert client._page.url == 'about:blank'
        assert requests == []
        client._context.request.get.assert_awaited_once_with(URL, max_redirects=0, timeout=30000)
        response.dispose.assert_awaited_once()
        assert not client._page._hh_action_monitor.unknown
        assert not client._context._hh_action_watch.unknown
    asyncio.run(scenario(tmp_path, check))


def test_session_proof_does_not_clear_preexisting_uncertainty(tmp_path):
    async def check(client, requests, response):
        client._page._hh_action_monitor.mark_unknown()
        with pytest.raises(HHUnexpectedUI) as raised:
            await client.is_logged_in()
        assert raised.value.hh_stop_reason == 'possible_external_action'
        client._context.request.get.assert_not_awaited()
        assert client._context._hh_action_watch.unknown
    asyncio.run(scenario(tmp_path, check))
