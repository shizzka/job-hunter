"""Offline reproduction of the static MTS srcdoc iframe seen on vacancy_details."""
import asyncio
import ssl
import subprocess
from contextlib import contextmanager, nullcontext
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import Route, async_playwright

from browser_action_boundary import bootstrap_boundary
from tests.test_hh_search_session import retain_unproven_attempt
from hh.recovery import BLOCK_WORKER_CONSTRUCTORS, monitor_page
from hh.ui import HHUIGuard, HHUnexpectedUI
from hh_client import HHClient

URL = 'https://hh.ru/vacancy/123'
NEXT_URL = 'https://hh.ru/vacancy/456'
SPB_URL = 'https://spb.hh.ru/vacancy/123?hhtmFrom=negotiation_list'
FIXTURE = Path(__file__).parent / 'fixtures' / 'hh_vacancy_details_background.html'
HTML = FIXTURE.read_text()


@contextmanager
def regional_document_server(tmp_path, body):
    """Serve the bypassed native redirect on loopback; public DNS stays blocked."""
    key, certificate = tmp_path / 'loopback.key', tmp_path / 'loopback.crt'
    subprocess.run(['openssl', 'req', '-x509', '-newkey', 'rsa:2048', '-nodes',
        '-keyout', str(key), '-out', str(certificate), '-days', '1',
        '-subj', '/CN=spb.hh.ru'], check=True, capture_output=True)
    native = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            native.append((self.headers.get('Host'), self.path))
            if native[-1] != ('spb.hh.ru', '/vacancy/123?hhtmFrom=negotiation_list'):
                self.send_error(404)
                return
            data = body.encode()
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Content-Length', str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    tls = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    tls.load_cert_chain(certificate, key)
    server.socket = tls.wrap_socket(server.socket, server_side=True)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_port, native
    finally:
        server.shutdown()
        thread.join()
        server.server_close()


async def browser_scenario(tmp_path, monkeypatch, check, body=HTML, *, redirect=False):
    server = regional_document_server(tmp_path, body) if redirect else nullcontext((None, []))
    with server as (port, native):
        async with async_playwright() as pw:
            mapping = f'MAP spb.hh.ru 127.0.0.1:{port}, ' if redirect else ''
            browser = await pw.chromium.launch(headless=True, args=[
                '--disable-background-networking', '--no-proxy-server',
                '--host-resolver-rules=' + mapping + 'MAP * ~NOTFOUND, EXCLUDE localhost'])
            try:
                context = await browser.new_context(service_workers='block', ignore_https_errors=redirect)
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
                    target = kwargs.get('url', item.request.url)
                    assert target in ({URL, SPB_URL} if redirect else {URL})
                    assert item.request.method == 'GET'
                    fetched.append((target, kwargs))
                    initial_redirect = redirect and target == URL
                    return SimpleNamespace(status=302 if initial_redirect else 200, url=target,
                        headers={'location': SPB_URL} if initial_redirect else {'content-type':'text/html'},
                        body=AsyncMock(return_value=body.encode()),
                        text=AsyncMock(return_value='' if initial_redirect else body), dispose=AsyncMock())
                # Route.fetch uses APIRequest transport and bypasses browser route interception.
                monkeypatch.setattr(Route, 'fetch', fetch)
                client = HHClient()
                client._page = await context.new_page()
                client._context = context
                client._ui_guard = HHUIGuard(tmp_path, notify=AsyncMock(return_value=False))
                monitor_page(client, client._page)
                await bootstrap_boundary(client._page)
                client._page.on('frameattached', lambda frame: frames.append(frame))
                if redirect:
                    await check(client, requests, frames, fetched, native)
                else:
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


def test_regional_details_redirect_never_loads_unsanitized_native_document(tmp_path, monkeypatch):
    async def check(client, requests, frames, fetched, native):
        page = client._page
        try:
            description = await client.get_vacancy_details(URL)
        except HHUnexpectedUI as stopped:
            # RED diagnosis: the fulfilled raw 302 bypasses all browser routes,
            # attaches the observed iframe and marks uncertainty before submit.
            assert stopped.stage == 'vacancy_details'
            assert stopped.hh_stop_reason == 'possible_external_action'
            assert stopped.hh_uncertain is True
            assert native == [('spb.hh.ru', '/vacancy/123?hhtmFrom=negotiation_list')]
            assert frames and len(fetched) == 1
            assert client._page._hh_action_monitor.unknown
            assert client._context._hh_action_watch.unknown
            assert getattr(client, '_external_attempt', None) is None
            raise
        assert description == 'Test browser lifecycle and Python services.'
        assert client._page is page and page.url == URL
        assert [target for target, _ in fetched] == [URL, SPB_URL]
        assert native == [] and frames == []
        assert await page.locator('[data-qa="vacancy-title"]').inner_text() == 'QA Engineer'
        assert await page.locator('template[data-jh-passive-details="mts-pixel"]').count() == 1
        assert await page.evaluate('Boolean(window.syntheticPageScriptRan)') is False
        assert not client._page._hh_action_monitor.unknown
        assert not client._context._hh_action_watch.unknown
        assert getattr(client, '_external_attempt', None) is None
        assert all(method == 'GET' for method, _ in requests)
        await page.goto(NEXT_URL)
        assert await page.evaluate('window.nextPageScriptRan') is True
        assert not client._page._hh_action_monitor.unknown
    asyncio.run(browser_scenario(tmp_path, monkeypatch, check, redirect=True))


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
        await retain_unproven_attempt(client, tmp_path)
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
    page = SimpleNamespace(main_frame=object(), on=lambda *args: None)
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
        route.fetch.assert_awaited_once_with(url=URL, max_redirects=0, max_retries=0, timeout=20000)
    asyncio.run(check())


def test_details_route_follows_trusted_redirect_and_fulfills_only_passive_html():
    from hh.vacancy_details import DETAILS_CSP, inert_details_html, passive_details_navigation
    async def check():
        client, page, route, response = fake_navigation_session(status=302,
            headers={'location': SPB_URL})
        final = SimpleNamespace(status=200, url=SPB_URL,
            headers={'content-type':'text/html', 'content-length':'999', 'content-encoding':'gzip',
                'content-security-policy':"img-src 'self'"},
            text=AsyncMock(return_value=HTML), dispose=AsyncMock())
        route.fetch.side_effect = [response, final]
        async with passive_details_navigation(client, URL):
            await page.handler(route)
        assert route.fetch.await_args_list[0].kwargs == {
            'url': URL, 'max_redirects':0, 'max_retries':0, 'timeout':20000}
        assert route.fetch.await_args_list[1].kwargs == {
            'url': SPB_URL, 'max_redirects':0, 'max_retries':0, 'timeout':20000}
        route.fulfill.assert_awaited_once_with(status=200,
            headers={'content-type':'text/html',
                'content-security-policy':"img-src 'self', " + DETAILS_CSP},
            body=inert_details_html(HTML))
        response.text.assert_not_awaited()
        response.dispose.assert_awaited_once()
        final.dispose.assert_awaited_once()
        route.abort.assert_not_awaited()
        page.unroute.assert_awaited_once()
    asyncio.run(check())


@pytest.mark.parametrize('final_url', [
    'https://evil.invalid/vacancy/123', 'https://spb.hh.ru/vacancy/456',
    'https://spb.hh.ru/vacancy/123?resumeId=synthetic',
])
def test_details_route_rejects_unsafe_final_response_url(final_url):
    from hh.vacancy_details import passive_details_navigation
    async def check():
        client, page, route, response = fake_navigation_session(status=302,
            headers={'location': SPB_URL})
        final = SimpleNamespace(status=200, url=final_url, headers={'content-type':'text/html'},
            text=AsyncMock(return_value=HTML), dispose=AsyncMock())
        route.fetch.side_effect = [response, final]
        with pytest.raises(ValueError):
            async with passive_details_navigation(client, URL):
                await page.handler(route)
        assert route.fetch.await_count == 2
        route.fulfill.assert_not_awaited()
        route.abort.assert_awaited_once()
        final.text.assert_not_awaited()
        response.dispose.assert_awaited_once()
        final.dispose.assert_awaited_once()
    asyncio.run(check())


@pytest.mark.parametrize('locations', [
    [SPB_URL, URL],
    ['/vacancy/123?hop=1', '/vacancy/123?hop=2', '/vacancy/123?hop=3', '/vacancy/123?hop=4'],
], ids=['redirect-loop', 'redirect-limit'])
def test_details_route_rejects_redirect_loop_and_limit_without_native_navigation(locations):
    from hh.vacancy_details import passive_details_navigation
    from urllib.parse import urljoin
    async def check():
        client, page, route, _ = fake_navigation_session()
        responses, target = [], URL
        for location in locations:
            responses.append(SimpleNamespace(status=302, url=target, headers={'location':location},
                text=AsyncMock(), dispose=AsyncMock()))
            target = urljoin(target, location)
        route.fetch.side_effect = responses
        with pytest.raises(ValueError, match='Vacancy details redirect (loop|limit)'):
            async with passive_details_navigation(client, URL):
                await page.handler(route)
        assert route.fetch.await_count == len(locations)
        route.fulfill.assert_not_awaited()
        route.abort.assert_awaited_once()
        for response in responses:
            response.text.assert_not_awaited()
            response.dispose.assert_awaited_once()
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


@pytest.mark.parametrize('owner', ['page', 'context', 'guard', 'request_context'])
@pytest.mark.parametrize('when', ['redirect_dispose', 'final_fetch'])
def test_details_route_rejects_ownership_change_during_redirect_hop(owner, when):
    from hh.vacancy_details import passive_details_navigation
    async def check():
        client, page, route, response = fake_navigation_session(status=302,
            headers={'location':SPB_URL})
        final = SimpleNamespace(status=200, url=SPB_URL, headers={'content-type':'text/html'},
            text=AsyncMock(return_value=HTML), dispose=AsyncMock())
        def replace():
            if owner == 'request_context':
                client._context.request = object()
            else:
                setattr(client, {'page':'_page', 'context':'_context', 'guard':'_ui_guard'}[owner], object())
        if when == 'redirect_dispose':
            response.dispose.side_effect = replace
            route.fetch.side_effect = [response, final]
        else:
            async def fetch(**kwargs):
                if kwargs['url'] == URL:
                    return response
                replace()
                return final
            route.fetch.side_effect = fetch
        with pytest.raises(HHUnexpectedUI) as stopped:
            async with passive_details_navigation(client, URL):
                await page.handler(route)
        assert stopped.value.hh_stop_reason == 'browser_ownership_changed'
        assert stopped.value.hh_uncertain is True
        route.fulfill.assert_not_awaited()
        route.abort.assert_awaited_once()
        response.dispose.assert_awaited_once()
        final.text.assert_not_awaited()
        if when == 'redirect_dispose':
            assert route.fetch.await_count == 1
            final.dispose.assert_not_awaited()
        else:
            assert route.fetch.await_count == 2
            final.dispose.assert_awaited_once()
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


@pytest.mark.parametrize('goto_error', [RuntimeError, TimeoutError])
@pytest.mark.parametrize('late_uncertainty', ['', 'attempt', 'session'])
def test_apply_regional_forbidden_navigation_keeps_cause_before_chrome_error_guard(tmp_path, monkeypatch, late_uncertainty, goto_error):
    """302 -> regional 403 -> goto error must stop before inspecting chrome-error UI."""
    from hh import apply as hh_apply
    from hh import recovery, verification
    import hh_client

    async def check():
        client, page, route, redirect = fake_navigation_session(status=302,
            headers={'location': SPB_URL})
        client._cookie_paths = SimpleNamespace(cookies_file=tmp_path / 'cookies.json')
        client._save_debug_snapshot = AsyncMock()
        page.url = URL
        page.wait_for_timeout = AsyncMock()
        final = SimpleNamespace(status=403, url=SPB_URL,
            headers={'content-type': 'text/html; charset=UTF-8'},
            text=AsyncMock(return_value='<html>ForbiddenPage</html>'), dispose=AsyncMock())
        route.fetch.side_effect = [redirect, final]

        async def goto(*args, **kwargs):
            await page.handler(route)
            page.url = 'chrome-error://chromewebdata/'
            raise goto_error('Page.goto: net::ERR_FAILED; synthetic-private-query')
        page.goto = AsyncMock(side_effect=goto)
        async def ensure(stage, **kwargs):
            if stage == 'vacancy_open':
                error = HHUnexpectedUI(stage, 'synthetic')
                error.hh_stop_reason = 'auth_origin_unproven'
                raise error
        client._ensure_expected_ui.side_effect = ensure
        abandon = AsyncMock(return_value=SimpleNamespace(recovered=False,
            reason='auth_origin_unproven', uncertain=False))
        monkeypatch.setattr(recovery, 'abandon_page', abandon)
        async def reconcile(session, attempt, *args):
            if late_uncertainty == 'attempt':
                attempt.uncertain = True
            elif late_uncertainty == 'session':
                session._hh_recovery_uncertain = True
        monkeypatch.setattr(verification, 'reconcile_post_submit', AsyncMock(side_effect=reconcile))
        result = await hh_apply.apply_to_vacancy(client, URL, preferred_resume_id='synthetic-exact',
            absolute_hh_url=lambda value: value, anti_bot_message=lambda *args: '', logger=hh_client.log)
        assert result['hh_recovery_reason'] == 'vacancy_navigation_failed'
        failure = result['hh_navigation_failure']
        assert failure == {
            'stage': 'VACANCY_NAVIGATION', 'reason_code': 'vacancy_navigation_failed',
            'requested_url': URL, 'response_url': 'https://spb.hh.ru/vacancy/123',
            'response_origin': 'https://spb.hh.ru', 'status': 403, 'content_type': 'text/html',
            'redirect_chain': [{'url': URL, 'status': 302},
                               {'url': 'https://spb.hh.ru/vacancy/123', 'status': 403}],
            'error_type': 'ValueError', 'error_code': 'unexpected_response', 'method': 'GET',
        }
        assert result['ok'] is False and result['hh_hard_stop'] is True
        assert bool(result.get('uncertain')) is bool(late_uncertainty)
        from state_store.native_apply import NativeApplyRepository
        receipt = NativeApplyRepository(client._cookie_paths.cookies_file, 'hh').get(URL)
        assert receipt['status'] == ('uncertain' if late_uncertainty else 'failed')
        assert [call.args[0] for call in client._ensure_expected_ui.await_args_list] == (
            ['apply_before_navigation', 'vacancy_open'] if goto_error is TimeoutError else ['apply_before_navigation'])
        abandon.assert_not_awaited()
        assert page.wait_for_timeout.await_count == (1 if goto_error is TimeoutError else 0)
        final.text.assert_not_awaited()
        route.fulfill.assert_not_awaited()
        route.abort.assert_awaited_once()
        assert [call.kwargs['url'] for call in route.fetch.await_args_list] == [URL, SPB_URL]
        assert route.request.method == 'GET'
        assert 'synthetic-private-query' not in repr(result)
    asyncio.run(check())
