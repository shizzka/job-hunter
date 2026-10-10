"""Offline reproduction of the static MTS srcdoc iframe seen on vacancy_details."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import Route, async_playwright

from browser_action_boundary import bootstrap_boundary
from hh.recovery import BLOCK_WORKER_CONSTRUCTORS, monitor_page
from hh.ui import HHUIGuard, HHUnexpectedUI
from hh_client import HHClient

URL = 'https://hh.ru/vacancy/123'
NEXT_URL = 'https://hh.ru/vacancy/456'
FIXTURE = Path(__file__).parent / 'fixtures' / 'hh_vacancy_details_background.html'
HTML = FIXTURE.read_text()


async def browser_scenario(tmp_path, monkeypatch, check, body=HTML):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=[
            '--disable-background-networking',
            '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost'])
        try:
            context = await browser.new_context(service_workers='block')
            await context.add_init_script(script=BLOCK_WORKER_CONSTRUCTORS)
            requests, frames, fetched = [], [], []

            async def route(item):
                requests.append((item.request.method, item.request.url))
                if item.request.url == URL:
                    await item.fulfill(status=200, content_type='text/html', body=body)
                elif item.request.url == NEXT_URL:
                    await item.fulfill(status=200, content_type='text/html',
                        body='<main>Next page</main><script>window.nextPageScriptRan=true</script>')
                else:
                    await item.abort()
            await context.route('**/*', route)

            async def fetch(item, **kwargs):
                assert item.request.url == URL
                assert item.request.method == 'GET'
                fetched.append((item.request.url, kwargs))
                return SimpleNamespace(status=200, url=URL,
                    headers={'content-type':'text/html'},
                    body=AsyncMock(return_value=body.encode()),
                    text=AsyncMock(return_value=body), dispose=AsyncMock())
            # Route.fetch uses APIRequest transport and bypasses browser route interception.
            monkeypatch.setattr(Route, 'fetch', fetch)
            client = HHClient()
            client._page = await context.new_page()
            client._context = context
            client._ui_guard = HHUIGuard(tmp_path, notify=AsyncMock(return_value=False))
            monitor_page(client, client._page)
            await bootstrap_boundary(client._page)
            client._page.on('frameattached', lambda frame: frames.append(frame))
            await check(client, requests, frames, fetched)
        finally:
            await browser.close()


def test_observed_static_mts_iframe_first_sets_details_uncertainty(tmp_path, monkeypatch):
    # Remove telemetry to isolate the very first observed source: parser iframe attachment.
    body = HTML[:HTML.index('<script>window.syntheticPageScriptRan')] + '</body></html>'
    async def check(client, requests, frames, fetched):
        await client._page.goto(URL)
        assert frames
        with pytest.raises(HHUnexpectedUI) as raised:
            await client._ensure_expected_ui('vacancy_details')
        assert raised.value.stage == 'vacancy_details'
        assert raised.value.hh_stop_reason == 'possible_external_action'
        assert raised.value.hh_uncertain is True
        assert client._page._hh_action_monitor.unknown
        assert client._context._hh_action_watch.unknown
        assert getattr(client, '_external_attempt', None) is None
        assert all(method == 'GET' for method, _ in requests)
        owner = client._page.locator('div > iframe#mts-pixel')
        assert await owner.count() == 1
        assert await owner.get_attribute('src') is None
        assert 'stream_land' in await owner.get_attribute('srcdoc')
        assert fetched == []
    asyncio.run(browser_scenario(tmp_path, monkeypatch, check, body))


def test_details_retains_browser_dom_without_observed_background_actions(tmp_path, monkeypatch):
    async def check(client, requests, frames, fetched):
        page = client._page
        assert await client.get_vacancy_details(URL) == 'Test browser lifecycle and Python services.'
        assert client._page is page and page.url == URL
        assert await page.locator('[data-qa="vacancy-title"]').inner_text() == 'QA Engineer'
        assert await page.locator('[data-qa="vacancy-company-name"]').inner_text() == 'Synthetic employer'
        assert await page.locator('[data-qa="vacancy-response-link-top"]').get_attribute('href') == '/applicant/vacancy_response?vacancyId=123'
        assert await page.evaluate('Boolean(window.__jhActionBoundary)')
        assert await page.evaluate('Boolean(window.syntheticPageScriptRan)') is False
        assert frames == []
        assert not client._page._hh_action_monitor.unknown
        assert not client._context._hh_action_watch.unknown
        assert len(fetched) == 1
        assert all(method == 'GET' and url == URL for method, url in requests)
        # Stage policy must not leak into the next document or resume old suspended scripts.
        await page.goto(NEXT_URL)
        assert await page.evaluate('window.nextPageScriptRan') is True
        assert await page.evaluate('Boolean(window.syntheticPageScriptRan)') is False
        assert not client._page._hh_action_monitor.unknown
        assert not client._context._hh_action_watch.unknown
    asyncio.run(browser_scenario(tmp_path, monkeypatch, check))


@pytest.mark.parametrize('body', [
    HTML.replace('id="mts-pixel"', 'id="unknown-embed"'),
    HTML.replace('document.head.appendChild(sfmb);', 'document.body.appendChild(sfmb);'),
    HTML.replace('https://sm.rtb.mts.ru/s?p=', 'https://unknown.invalid/action?p='),
    HTML.replace('style="display:none" srcdoc=', 'style="display:none" src="https://unknown.invalid/" srcdoc='),
], ids=['unknown-id', 'same-id-altered-script', 'same-id-altered-network', 'same-id-added-src'])
def test_unknown_or_same_id_tampered_static_embed_fails_closed(tmp_path, monkeypatch, body):
    async def check(client, requests, frames, fetched):
        with pytest.raises(ValueError, match='Unknown vacancy details embed'):
            await client.get_vacancy_details(URL)
        assert getattr(client, '_external_attempt', None) is None
        assert all(method == 'GET' for method, _ in requests)
        assert frames == []
        assert not client._page._hh_action_monitor.unknown
        assert not client._context._hh_action_watch.unknown
    asyncio.run(browser_scenario(tmp_path, monkeypatch, check, body))


def test_details_does_not_clear_preexisting_uncertainty(tmp_path, monkeypatch):
    async def check(client, requests, frames, fetched):
        client._page._hh_action_monitor.mark_unknown()
        with pytest.raises(HHUnexpectedUI) as raised:
            await client.get_vacancy_details(URL)
        assert raised.value.hh_uncertain
        assert raised.value.hh_stop_reason == 'possible_external_action'
        assert client._page.url == 'about:blank'
        assert fetched == [] and requests == [] and frames == []
        assert client._page._hh_action_monitor.unknown
        assert client._context._hh_action_watch.unknown
    asyncio.run(browser_scenario(tmp_path, monkeypatch, check))


def test_details_policy_does_not_exempt_unknown_frame_after_navigation(tmp_path, monkeypatch):
    async def check(client, requests, frames, fetched):
        await client.get_vacancy_details(URL)
        await client._page.goto(NEXT_URL)
        observed = asyncio.Event()
        client._page.on('frameattached', lambda frame: observed.set())
        await client._page.evaluate("document.body.appendChild(document.createElement('iframe'))")
        await asyncio.wait_for(observed.wait(), 5)
        with pytest.raises(HHUnexpectedUI) as raised:
            await client._ensure_expected_ui('response_controls')
        assert raised.value.hh_stop_reason == 'possible_external_action'
        assert raised.value.hh_uncertain is True
        assert client._page._hh_action_monitor.unknown
        assert client._context._hh_action_watch.unknown
        assert getattr(client, '_external_attempt', None) is None
    asyncio.run(browser_scenario(tmp_path, monkeypatch, check))


@pytest.mark.parametrize('separator', ['\u2028', '\u0085'])
def test_inert_details_preserves_source_with_unicode_line_separator(separator):
    from hh.vacancy_details import inert_details_html
    original = HTML.replace('<main>', '<main>Unicode' + separator + 'text', 1)
    actual = inert_details_html(original)
    opening = '<template data-jh-passive-details="mts-pixel">'
    assert actual.count(opening) == 1
    assert actual.replace(opening, '', 1).replace('</iframe></template>', '</iframe>', 1) == original


@pytest.mark.parametrize('body', [
    HTML.replace('id="mts-pixel"', 'id="mts-pixel" id="other"'),
    HTML.replace('</iframe>', 'unexpected fallback</iframe>'),
    HTML.replace('</iframe>', '', 1),
    HTML.replace('</iframe>', '</iframe><iframe id="unknown"></iframe>', 1),
    HTML.replace('</iframe>', '<iframe id="nested"></iframe></iframe>', 1),
], ids=['duplicate-attribute', 'unexpected-content', 'unclosed', 'additional-unknown', 'nested'])
def test_inert_details_rejects_malformed_duplicate_embeds(body):
    from hh.vacancy_details import inert_details_html
    with pytest.raises(ValueError):
        inert_details_html(body)


def fake_navigation_session(body=HTML, status=200, headers=None):
    """Native routing API shape with no browser/network needed for transport failures."""
    page = SimpleNamespace(main_frame=object())
    async def install(pattern, handler):
        page.handler = handler
    page.route = AsyncMock(side_effect=install)
    page.unroute = AsyncMock()
    client = HHClient()
    client._page = page
    client._context = SimpleNamespace(request=object())
    client._ui_guard = object()
    client._ensure_expected_ui = AsyncMock()
    response = SimpleNamespace(status=status, url=URL,
        headers=headers or {'content-type':'text/html'},
        text=AsyncMock(return_value=body), dispose=AsyncMock())
    request = SimpleNamespace(method='GET', url=URL, frame=page.main_frame,
        is_navigation_request=lambda: True)
    route = SimpleNamespace(request=request, fetch=AsyncMock(return_value=response),
        fulfill=AsyncMock(), abort=AsyncMock(), fallback=AsyncMock())
    return client, page, route, response


@pytest.mark.parametrize('location', [
    'https://evil.invalid/vacancy/123', '/vacancy/456',
    '/vacancy/123?resumeId=synthetic&letter=synthetic', '',
])
def test_details_redirect_trust_rejects_before_fulfill(location):
    from hh.vacancy_details import passive_details_navigation
    async def check():
        client, page, route, response = fake_navigation_session(status=302,
            headers={'content-type':'text/html', 'location':location})
        with pytest.raises(ValueError):
            async with passive_details_navigation(client, URL):
                await page.handler(route)
        route.fulfill.assert_not_awaited()
        route.abort.assert_awaited_once()
        response.dispose.assert_awaited_once()
        page.unroute.assert_awaited_once()
        route.fetch.assert_awaited_once_with(max_redirects=0, max_retries=0, timeout=20000)
    asyncio.run(check())


def test_details_route_preserves_trusted_redirect_as_native_navigation():
    from hh.vacancy_details import passive_details_navigation
    async def check():
        client, page, route, response = fake_navigation_session(status=302,
            headers={'location':'/vacancy/123?from=synthetic'})
        async with passive_details_navigation(client, URL):
            await page.handler(route)
        route.fulfill.assert_awaited_once_with(status=302,
            headers={'location':'/vacancy/123?from=synthetic'}, body='')
        response.text.assert_not_awaited()
        response.dispose.assert_awaited_once()
        route.abort.assert_not_awaited()
        page.unroute.assert_awaited_once()
    asyncio.run(check())


def test_details_route_rejects_ownership_change_after_fetch():
    from hh.vacancy_details import passive_details_navigation
    async def check():
        client, page, route, response = fake_navigation_session()
        async def replace(**kwargs):
            client._page = object()
            return response
        route.fetch.side_effect = replace
        with pytest.raises(HHUnexpectedUI) as raised:
            async with passive_details_navigation(client, URL):
                await page.handler(route)
        assert raised.value.hh_stop_reason == 'browser_ownership_changed'
        assert raised.value.hh_uncertain
        route.fulfill.assert_not_awaited()
        route.abort.assert_awaited_once()
        response.text.assert_not_awaited()
        response.dispose.assert_awaited_once()
        page.unroute.assert_awaited_once()
    asyncio.run(check())


def test_details_route_cancellation_aborts_without_fulfilling_or_retrying():
    from hh.vacancy_details import passive_details_navigation
    async def check():
        client, page, route, response = fake_navigation_session()
        started = asyncio.Event()
        async def blocked_fetch(**kwargs):
            started.set()
            await asyncio.Future()
        route.fetch.side_effect = blocked_fetch
        with pytest.raises(asyncio.CancelledError):
            async with passive_details_navigation(client, URL):
                task = asyncio.create_task(page.handler(route))
                await asyncio.wait_for(started.wait(), 1)
                task.cancel()
                await task
        route.abort.assert_awaited_once()
        route.fulfill.assert_not_awaited()
        assert route.fetch.await_count == 1
        page.unroute.assert_awaited_once()
    asyncio.run(check())


def test_details_route_registration_ownership_race_never_enters_navigation():
    from hh.vacancy_details import passive_details_navigation
    async def check():
        client, page, route, response = fake_navigation_session()
        replacement = SimpleNamespace(goto=AsyncMock())
        async def install_then_replace(pattern, handler):
            page.handler = handler
            client._page = replacement
        page.route.side_effect = install_then_replace
        with pytest.raises(HHUnexpectedUI) as raised:
            async with passive_details_navigation(client, URL):
                await client._page.goto(URL)
        assert raised.value.hh_stop_reason == 'browser_ownership_changed'
        assert raised.value.hh_uncertain is True
        assert client._hh_recovery_stop_reason == 'browser_ownership_changed'
        replacement.goto.assert_not_awaited()
        route.fetch.assert_not_awaited()
        route.fulfill.assert_not_awaited()
        page.unroute.assert_awaited_once()
    asyncio.run(check())


def test_details_route_unroute_failure_still_drains_owned_pending_fetch():
    from hh.vacancy_details import passive_details_navigation
    async def check():
        client, page, route, response = fake_navigation_session()
        started, drained = asyncio.Event(), asyncio.Event()
        async def blocked_fetch(**kwargs):
            started.set()
            try:
                await asyncio.Future()
            finally:
                drained.set()
        route.fetch.side_effect = blocked_fetch
        page.unroute.side_effect = RuntimeError('synthetic unroute failure')
        with pytest.raises(RuntimeError, match='synthetic unroute failure'):
            async with passive_details_navigation(client, URL):
                handler_task = asyncio.create_task(page.handler(route))
                await asyncio.wait_for(started.wait(), 1)
        # Cleanup must already have drained the owned fetch despite unroute failing.
        assert drained.is_set()
        await asyncio.wait_for(handler_task, 1)
        assert handler_task.done()
        route.abort.assert_awaited_once()
        route.fulfill.assert_not_awaited()
        assert route.fetch.await_count == 1
        page.unroute.assert_awaited_once()
    asyncio.run(check())
