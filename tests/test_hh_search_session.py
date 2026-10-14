"""Offline reproduction of the observed search action-monitor latch."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

import config
from browser_action_boundary import bootstrap_boundary
from hh.recovery import BLOCK_WORKER_CONSTRUCTORS, monitor_page
from hh.ui import HHUIGuard, HHUnexpectedUI
from hh_client import HHClient
from state_store.native_apply import NativeApplyRepository, run_native_attempt

URL = 'https://hh.ru/search/vacancy?text=synthetic'
SSR = '''<section data-qa="vacancy-serp__results"><article data-qa="vacancy-serp__vacancy">
<a data-qa="serp-item__title" href="/vacancy/123">QA Engineer</a>
<a data-qa="vacancy-serp__vacancy-employer">Synthetic employer</a>
<div data-qa="vacancy-serp__vacancy_snippet_requirement">Python</div>
</article></section>'''
BACKGROUND = '''<script>
const pixel=document.createElement('iframe');pixel.id='mts-pixel';document.body.append(pixel);
fetch('/anatskytics',{method:'POST',body:'synthetic'}).catch(()=>{});
</script>'''


async def retain_unproven_attempt(client, tmp_path):
    """Keep a real failed receipt whose lack of external action is unproven."""
    repository = NativeApplyRepository(tmp_path / 'uncertainty-cookies.json', 'hh')

    async def operation():
        return {'ok': False}

    await run_native_attempt(client, repository, 'https://hh.ru/vacancy/999999', operation)


async def browser_scenario(tmp_path, check, body=SSR + BACKGROUND):
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
            response = SimpleNamespace(status=200, url=URL, headers={'content-type':'text/html'},
                text=AsyncMock(return_value=body), dispose=AsyncMock())
            # APIRequest bypasses Page routing, so stub it independently.
            context.request.get = AsyncMock(return_value=response)
            client = HHClient()
            client._page = await context.new_page()
            client._context = context
            client._ui_guard = HHUIGuard(tmp_path, notify=AsyncMock(return_value=False))
            monitor_page(client, client._page)
            await bootstrap_boundary(client._page)
            await check(client, requests, response)
        finally:
            await browser.close()


@pytest.mark.parametrize('source', ['frameattached','telemetry_post'])
def test_observed_search_event_sets_uncertainty_before_parsing(tmp_path, source):
    effect = '''<script>
    const pixel=document.createElement('iframe');pixel.id='mts-pixel';document.body.append(pixel);
    </script>''' if source == 'frameattached' else '''<script>
    fetch('/anatskytics',{method:'POST',body:'synthetic'}).catch(()=>{});</script>'''
    async def check(client, requests, response):
        if source == 'telemetry_post':
            await retain_unproven_attempt(client, tmp_path)
        observed = asyncio.Event()
        client._page.on('frameattached', lambda f: observed.set() if source == 'frameattached' else None)
        client._context.on('request', lambda r: observed.set() if source == 'telemetry_post' and r.method=='POST' else None)
        await client._page.goto(URL)
        await asyncio.wait_for(observed.wait(),5)
        with pytest.raises(HHUnexpectedUI) as raised:
            await client._ensure_expected_ui('search')
        assert raised.value.stage == 'search'
        assert raised.value.hh_stop_reason == 'possible_external_action'
        assert raised.value.hh_uncertain is True
        assert client._page._hh_action_monitor.unknown
        assert client._context._hh_action_watch.unknown
        assert getattr(client, '_external_attempt', None) is None
        if source=='frameattached':
            assert all(method=='GET' for method,url in requests)
            assert await client._page.locator('#mts-pixel').count()==1
    asyncio.run(browser_scenario(tmp_path, check, SSR + effect))


def test_search_reads_observed_ssr_without_background_events(tmp_path):
    async def check(client, requests, response):
        values = await client._search_vacancies('synthetic',page=1,url=URL)
        assert len(values)==1 and values[0]['id']=='123'
        assert values[0]['company']=='Synthetic employer'
        assert values[0]['snippet']=='Python'
        assert requests==[] and client._page.url=='about:blank'
        assert not client._page._hh_action_monitor.unknown
        assert not client._context._hh_action_watch.unknown
        response.dispose.assert_awaited_once()
        client._context.request.get.assert_awaited_once_with(URL,max_redirects=0,timeout=30000)
    asyncio.run(browser_scenario(tmp_path, check))


@pytest.mark.parametrize('during',['before','get','text','dispose'])
def test_search_preserves_uncertainty_and_does_not_retry(tmp_path,during):
    async def check(client, requests, response):
        await retain_unproven_attempt(client, tmp_path)
        if during=='before':
            client._page._hh_action_monitor.mark_unknown()
        else:
            async def poison(*args,**kwargs):
                client._page._hh_action_monitor.mark_unknown()
                return response if during=='get' else SSR
            if during=='get': client._context.request.get=AsyncMock(side_effect=poison)
            elif during=='text': response.text=AsyncMock(side_effect=poison)
            else: response.dispose=AsyncMock(side_effect=poison)
        with pytest.raises(HHUnexpectedUI) as raised:
            await client._search_vacancies('synthetic',page=1,url=URL)
        assert raised.value.hh_uncertain and raised.value.hh_stop_reason=='possible_external_action'
        assert requests==[] and client._page.url=='about:blank'
        assert client._context.request.get.await_count==(0 if during=='before' else 1)
        assert client._context._hh_action_watch.unknown
    asyncio.run(browser_scenario(tmp_path, check))


@pytest.mark.parametrize('owner',['page','context','request_context'])
def test_search_rejects_ownership_change_during_get(tmp_path,owner):
    async def check(client,requests,response):
        original_context=client._context
        async def replace(*args,**kwargs):
            if owner=='page': client._page=object()
            elif owner=='context': client._context=object()
            else: original_context._request=object()
            return response
        if owner=='request_context':
            # BrowserContext.request is a property backed by _impl_obj._request.
            async def replace_request(*args,**kwargs):
                original_context._impl_obj._request=object()
                return response
            client._context.request.get=AsyncMock(side_effect=replace_request)
        else:
            client._context.request.get=AsyncMock(side_effect=replace)
        with pytest.raises(HHUnexpectedUI) as raised:
            await client._search_vacancies('synthetic',page=1,url=URL)
        assert raised.value.hh_stop_reason=='browser_ownership_changed'
        assert raised.value.hh_uncertain is True
        response.dispose.assert_awaited_once()
        response.text.assert_not_awaited()
    asyncio.run(browser_scenario(tmp_path,check))
