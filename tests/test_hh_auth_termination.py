"""Synthetic server authentication and real Chromium process-death checks."""
import asyncio
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest
from playwright.async_api import async_playwright

from hh.recovery import RecoveryBlocked, _authenticated_server_html
from hh.termination import terminate_browser


def test_unsupported_termination_capability_prevents_browser_start(monkeypatch):
    from hh.browser import start_browser
    import hh.termination as termination
    factory = Mock(side_effect=AssertionError('No browser may be created'))
    def unavailable():
        raise RuntimeError('pidfd unsupported')
    monkeypatch.setattr(termination, 'require_termination_capability', unavailable)
    session = SimpleNamespace(_browser=None, _context=None, _pw=None, _page=None)
    with pytest.raises(RuntimeError, match='pidfd unsupported'):
        asyncio.run(start_browser(session, playwright_factory=factory))
    factory.assert_not_called()
    from hh_client import HHClient
    assert asyncio.run(HHClient.hard_stop_browser(session)) is True


def test_foreign_browser_is_not_terminated_or_persisted(monkeypatch):
    import hh.termination as termination
    from hh.browser import stop_browser
    from hh_client import HHClient
    crash = AsyncMock(return_value=True)
    monkeypatch.setattr(termination, 'terminate_browser', crash)
    browser = SimpleNamespace(_hh_owner_session=object(), contexts=[])
    context = SimpleNamespace(browser=browser, cookies=AsyncMock(return_value=[]))
    browser.contexts = [context]
    session = SimpleNamespace(_browser=browser, _context=context, _pw=None, _page=None)
    assert asyncio.run(HHClient.hard_stop_browser(session)) is False
    with pytest.raises(RuntimeError, match='ownership is unproven'):
        asyncio.run(stop_browser(session))
    crash.assert_not_awaited()
    context.cookies.assert_not_awaited()


@pytest.mark.parametrize('body', [
    '<a data-qa="resume-card-link-exact">Resume</a>',
    '<nav class="profile-nav"><a href="/applicant/profile">Profile</a></nav><div data-qa="resume">Resume</div>',
])
def test_server_resume_authentication_accepts_explicit_account_markup(body):
    _authenticated_server_html(body)


@pytest.mark.parametrize('body', [
    '<p>No authenticated marker</p>',
    '<script>"<a data-qa=resume>"</script>',
    '<template><a data-qa="resume">Inert</a></template>',
    '<noscript><a data-qa="resume">Inert</a></noscript>',
    '<div data-qa="resume"></div><script>{"userType":"anonymous"}</script>',
    '<div data-qa="resume"></div><div role="dialog">Unknown</div>',
    '<div data-qa="resume"></div><div data-qa="profile-completion">Profile</div>',
    '<div data-qa="resume"></div><div data-qa="onboarding">Profile</div>',
    '<div data-qa="resume"></div><iframe src="/unknown"></iframe>',
    '<div data-qa="resume"></div><div data-qa="captcha">Challenge</div>',
    '<div data-qa="resume"></div><p>Verify you are human</p>',
])
def test_server_authentication_rejects_unproven_or_unsafe_markup(body):
    with pytest.raises(RecoveryBlocked):
        _authenticated_server_html(body)


def test_context_request_shares_cookie_jar_without_page_navigation():
    observed = []
    class Server(BaseHTTPRequestHandler):
        def do_GET(self):
            observed.append(self.headers.get('Cookie', ''))
            self.send_response(200)
            self.send_header('Content-Type', 'text/html')
            self.send_header('Set-Cookie', 'synthetic_server_cookie=shared; Path=/')
            self.end_headers()
            self.wfile.write(b'<a data-qa="resume-card-link-synthetic">Resume</a>')
        def log_message(self, *args):
            pass
    server = ThreadingHTTPServer(('127.0.0.1', 0), Server)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    async def scenario():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                context = await browser.new_context()
                await context.add_cookies([{'name': 'hhtoken', 'value': 'synthetic-only', 'domain': '127.0.0.1', 'path': '/'}])
                page = await context.new_page()
                response = await context.request.get(f'http://127.0.0.1:{server.server_port}/applicant/resumes', max_redirects=0)
                try:
                    assert response.status == 200
                    _authenticated_server_html(await response.text())
                finally:
                    await response.dispose()
                assert page.url == 'about:blank'
                values = {item['name']: item['value'] for item in await context.cookies()}
                assert values == {'hhtoken': 'synthetic-only', 'synthetic_server_cookie': 'shared'}
                assert observed == ['hhtoken=synthetic-only']
            finally:
                assert await terminate_browser(browser) is True
    try:
        asyncio.run(scenario())
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.parametrize('event,target', [('beforeunload', 'window'), ('unload', 'window'),
                                         ('pagehide', 'window'), ('visibilitychange', 'document')])
def test_owned_termination_never_executes_forced_oopif_handlers(tmp_path, event, target):
    args = ['--site-per-process', '--isolate-origins=https://other.invalid',
            '--disable-background-networking', '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost']
    def fixture(prefix, handlers, outer):
        result = ('<iframe src="https://other.invalid/child"></iframe>' if outer else '')
        result += '<form method="post" action="/synthetic-submit"><input name="resume" value="synthetic"><button>Submit</button></form>'
        result += '<script>const key=' + json.dumps(prefix) + ''';
if(!localStorage.getItem(key+'Sentinel')){localStorage.setItem(key+'Sentinel','persistent');localStorage.setItem(key+'Events','[]');}
const record=name=>{const values=JSON.parse(localStorage.getItem(key+'Events'));values.push(name);localStorage.setItem(key+'Events',JSON.stringify(values));};
for(const name of ['click','submit','formdata'])window.addEventListener(name,e=>{record(name);if(name==='submit')e.preventDefault()},true);
'''
        if handlers:
            result += target + '.addEventListener(' + json.dumps(event) + ''',()=>{record('lifecycle');const form=document.querySelector('form');form.requestSubmit();HTMLFormElement.prototype.submit.call(form);new FormData(form);navigator.sendBeacon('/synthetic-submit','synthetic');});'''
        return result + '</script>'
    async def scenario():
        async with async_playwright() as pw:
            async def launch(handlers):
                context = await pw.chromium.launch_persistent_context(str(tmp_path / 'synthetic-profile'), headless=True, args=args)
                async def route(item):
                    if item.request.method != 'GET':
                        await item.abort()
                    else:
                        outer = item.request.url.endswith('/outer')
                        await item.fulfill(status=200, content_type='text/html', body=fixture('top' if outer else 'child', handlers, outer))
                await context.route('**/*', route)
                return context
            # Persist positive sentinels before any dangerous handler exists.
            context = await launch(False)
            await context.pages[0].goto('https://hh.ru/outer', wait_until='load')
            await context.browser.close()
            context = await launch(True)
            await context.pages[0].goto('https://hh.ru/outer', wait_until='load')
            cdp = await context.new_cdp_session(context.pages[0])
            targets = (await cdp.send('Target.getTargets'))['targetInfos']
            assert any(item['type'] == 'iframe' and item['url'] == 'https://other.invalid/child' for item in targets)
            browser = context.browser
            first, second = await asyncio.gather(terminate_browser(browser), terminate_browser(browser))
            assert first is second is True
            assert await terminate_browser(browser) is True
            assert not browser.is_connected()
            reader = await launch(False)
            try:
                for prefix, origin in [('top', 'https://hh.ru'), ('child', 'https://other.invalid')]:
                    await reader.pages[0].goto(origin + '/readback')
                    storage = await reader.pages[0].evaluate("prefix=>({sentinel:localStorage.getItem(prefix+'Sentinel'),events:JSON.parse(localStorage.getItem(prefix+'Events'))})", prefix)
                    assert storage == {'sentinel': 'persistent', 'events': []}
            finally:
                assert await terminate_browser(reader.browser) is True
    asyncio.run(scenario())
