"""An HH access denial precedes interception abort; entirely offline evidence."""
import asyncio
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Thread
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

from hh.vacancy_details import HHVacancyNavigationError, passive_details_navigation
from tests.test_hh_vacancy_details import fake_navigation_session, URL, SPB_URL

DENIAL = ('Вам недоступна эта вакансия. Войдите как пользователь, у которого есть доступ '
          'на просмотр, либо как работодатель, создавший эту вакансию.')
BODY = '<html><head><title>HeadHunter</title></head><body><main>' + DENIAL + '</main></body></html>'


def test_native_navigation_keeps_server_403_without_interception():
    """An ordinary browser GET receives 403 as a response, not ERR_FAILED."""
    received = []
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received.append((self.command, self.path))
            if self.path == '/vacancy/123':
                self.send_response(302)
                self.send_header('Location', '/regional/vacancy/123')
                self.end_headers()
            else:
                self.send_response(403)
                self.send_header('Content-Type', 'text/html; charset=utf-8')
                self.end_headers()
                self.wfile.write(BODY.encode())
        def do_POST(self):
            received.append((self.command, self.path))
            self.send_error(405)
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    worker = Thread(target=server.serve_forever, daemon=True)
    worker.start()
    async def check():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=[
                '--disable-background-networking', '--no-proxy-server',
                '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE 127.0.0.1'])
            try:
                context = await browser.new_context(java_script_enabled=False, service_workers='block')
                page = await context.new_page()
                chain = []
                page.on('response', lambda r: chain.append(r.status) if r.request.is_navigation_request() else None)
                response = await page.goto(f'http://127.0.0.1:{server.server_port}/vacancy/123')
                assert chain == [302, 403]
                assert response.status == 403
                assert await page.title() == 'HeadHunter'
                assert await page.locator('main').inner_text() == DENIAL
                assert page.url.endswith('/regional/vacancy/123')
            finally:
                await browser.close()
    try:
        asyncio.run(check())
        assert [r for r in received if r[1] != '/favicon.ico'] == [('GET', '/vacancy/123'), ('GET', '/regional/vacancy/123')]
        assert all(method == 'GET' for method, _ in received)
    finally:
        server.shutdown()
        worker.join()
        server.server_close()


def test_passive_apply_route_aborts_only_after_receiving_forbidden_response():
    async def check():
        client, page, route, redirect = fake_navigation_session(status=302, headers={'location': SPB_URL})
        final = SimpleNamespace(status=403, url=SPB_URL, headers={'content-type': 'text/html'},
                                text=AsyncMock(return_value=BODY), dispose=AsyncMock())
        timeline = []
        async def fetch(**kwargs):
            response = redirect if not timeline else final
            timeline.append(('server_response', response.status))
            return response
        async def abort():
            timeline.append(('route_abort', None))
        route.fetch.side_effect = fetch
        route.abort.side_effect = abort
        with pytest.raises(HHVacancyNavigationError) as raised:
            async with passive_details_navigation(client, URL, stage="vacancy_open"):
                await page.handler(route)
                raise RuntimeError('Simulated ERR_FAILED after abort')
        assert timeline == [('server_response', 302), ('server_response', 403), ('route_abort', None)]
        assert raised.value.metadata['status'] == 403
        assert raised.value.metadata['response_origin'] == 'https://spb.hh.ru'
        route.fulfill.assert_not_awaited()
        final.text.assert_not_awaited()
        assert route.request.method == 'GET'
    asyncio.run(check())


def test_browser_inert_vacancy_403_continues_to_next_document_offline(monkeypatch):
    """Real Chromium rendering + production preflight, no external network or submit."""
    from playwright.async_api import Route
    from hh_client import HHClient
    from hh.recovery import PageActions, ContextActions
    from hh import resume_refresh
    from hh.vacancy_details import classify_vacancy_forbidden

    fetched, unexpected = [], []
    async def fake_fetch(route, **kwargs):
        fetched.append((route.request.method, kwargs['url']))
        forbidden = kwargs['url'] == URL
        return SimpleNamespace(url=kwargs['url'], status=403 if forbidden else 200,
            headers=({'content-type': 'text/html', 'refresh': '0;url=https://untrusted.invalid/'}
                     if forbidden else {'content-type': 'text/html'}),
            text=AsyncMock(return_value='<html><head><script>fetch("https://untrusted.invalid/")</script></head>'
                           '<body><div data-qa="vacancy-description">Next safe vacancy</div></body></html>'),
            dispose=AsyncMock())
    monkeypatch.setattr(Route, 'fetch', fake_fetch)
    health = AsyncMock(return_value=[])
    monkeypatch.setattr(resume_refresh, 'get_resume_ids_readonly', health)
    async def check():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=[
                '--disable-background-networking', '--no-proxy-server',
                '--host-resolver-rules=MAP * ~NOTFOUND'])
            try:
                context = await browser.new_context(service_workers='block')
                await context.add_cookies([{'name':'hhtoken', 'value':'offline-synthetic-only',
                    'domain':'.hh.ru', 'path':'/', 'secure':True, 'httpOnly':True}])
                async def reject(route):
                    unexpected.append(route.request.url)
                    await route.abort()
                await context.route('**/*', reject)
                page = await context.new_page()
                client = HHClient()
                client._page, client._context, client._ui_guard = page, context, object()
                client._ensure_expected_ui = AsyncMock()
                monitor = PageActions(client, page)
                page._hh_action_monitor = monitor
                context._hh_action_watch = ContextActions(client, context, pages={page:monitor})
                context._hh_workers_owner = client
                client._hh_blocked_worker_context = context
                with pytest.raises(HHVacancyNavigationError) as caught:
                    await client.get_vacancy_details(URL)
                error = caught.value
                assert page.url == URL
                assert await page.title() == 'HeadHunter: vacancy unavailable'
                assert await page.locator('meta[data-jh-vacancy-forbidden]').get_attribute('content') == error._passive_document_token
                assert await classify_vacancy_forbidden(client, error, URL)
                assert client._hh_forbidden_document_proof is not None
                assert await client.get_vacancy_details('https://hh.ru/vacancy/456') == 'Next safe vacancy'
                assert client._hh_forbidden_document_proof is None
                assert fetched == [('GET', URL), ('GET', 'https://hh.ru/vacancy/456')]
                assert not unexpected
                health.assert_awaited_once_with(client)
            finally:
                await browser.close()
    asyncio.run(check())


def test_cancelled_details_after_inert_403_preserves_cancellation():
    async def check():
        client, page, route, response = fake_navigation_session(status=403)
        with pytest.raises(asyncio.CancelledError):
            async with passive_details_navigation(client, URL):
                await page.handler(route)
                raise asyncio.CancelledError()
        route.fulfill.assert_awaited_once()
        route.abort.assert_not_awaited()
        page.unroute.assert_awaited_once()
    asyncio.run(check())
