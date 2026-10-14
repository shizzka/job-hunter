"""Response initialization is passive except for the exact evidenced tracker frames."""
import asyncio
import html
import logging
import os
import re
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import Route, async_playwright

from browser_action_boundary import bootstrap_boundary
from hh import apply
from hh.recovery import monitor_page
from hh.response_navigation import RESPONSE_CSP, response_navigation
from hh.ui import HHUIGuard, HHUnexpectedUI
from hh.vacancy_details import _MTS_SRCDOC
from hh_client import HHClient

URL = 'https://hh.ru/applicant/vacancy_response?vacancyId=123'
SPB_URL = 'https://spb.hh.ru/applicant/vacancy_response?vacancyId=123'
FORM_HTML = (Path(__file__).parent / 'fixtures' / 'hh_response_navigation.html').read_text()
VENDOR_URL = 'https://i.hh.ru/build/vendors.df512a23f097c519.js'
VENDOR_REPLAY_JS = (Path(__file__).parent / 'fixtures' / 'hh_response_navigation_vendor_replay.js').read_text()
DETAILS_HTML = (Path(__file__).parent / 'fixtures' / 'hh_vacancy_details_background.html').read_text()
MTS_SRCDOC = html.unescape(re.search(
    r'<iframe id="mts-pixel"[^>]*srcdoc="(.*?)"></iframe>', DETAILS_HTML, re.S
).group(1))
assert _MTS_SRCDOC.fullmatch(MTS_SRCDOC)


@pytest.mark.parametrize('host', ['hh.ru', 'spb.hh.ru'])
def test_response_csp_allows_observed_popup_endpoint_only(host):
    async def check():
        csp = RESPONSE_CSP
        if os.getenv('HH_RESPONSE_POPUP_FIX_DISABLED') == '1':
            for origin in ('hh.ru', 'spb.hh.ru'):
                csp = csp.replace(f'https://{origin}/applicant/vacancy_response/popup', '')
        url = f'https://{host}/applicant/vacancy_response?vacancyId=123'
        routed = []
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=[
                '--disable-background-networking',
                '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost'])
            try:
                context = await browser.new_context(service_workers='block')
                async def intercept(route):
                    req = route.request
                    if req.is_navigation_request() and req.url == url:
                        await route.fulfill(headers={'Content-Type': 'text/html',
                            'Content-Security-Policy': csp}, body='<body>Offline response</body>')
                    elif req.method == 'POST' and req.url == f'https://{host}/applicant/vacancy_response/popup':
                        routed.append(req.url)
                        await route.fulfill(content_type='application/json', body='{}')
                    else:
                        routed.append(req.url)
                        await route.abort()
                await context.route('**/*', intercept)
                page = await context.new_page()
                await page.goto(url)
                results = await page.evaluate('''async () => {
                    const results = [];
                    for (const path of ['/applicant/vacancy_response/popup',
                        '/applicant/vacancy_response/popup/other', '/applicant/profile']) {
                        try { const response = await fetch(path, {method:'POST', body:'offline'});
                            results.push(response.status); }
                        catch (_) { results.push('blocked'); }
                    }
                    return results;
                }''')
                assert results == [200, 'blocked', 'blocked']
                assert routed == [f'https://{host}/applicant/vacancy_response/popup']
            finally:
                await browser.close()
    asyncio.run(check())


async def browser_case(tmp_path, monkeypatch, check, *, response_html=FORM_HTML, resources=None):
    resources = resources or {}
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=[
            '--disable-background-networking',
            '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost'])
        try:
            context = await browser.new_context(service_workers='block')
            blocked = []

            async def network_gate(route):
                req = route.request
                if req.method == 'GET' and req.url in resources:
                    await route.fulfill(status=200, content_type='application/javascript', body=resources[req.url])
                    return
                if os.getenv('HH_RESPONSE_FIX_DISABLED') == '1' and req.url == URL and req.method == 'GET':
                    await route.fulfill(status=200, content_type='text/html', body=FORM_HTML)
                    return
                if req.method.upper() not in {'GET', 'HEAD'}:
                    blocked.append((req.method, req.url))
                    await route.abort()
                    return
                if req.url not in {URL, SPB_URL}:
                    await route.abort()
                    return
                await route.fallback()
            await context.route('**/*', network_gate)
            page = await context.new_page()

            async def fetch(route, **kwargs):
                target = kwargs.get('url') or route.request.url
                if target == URL:
                    return SimpleNamespace(status=302, url=URL,
                        headers={'location': SPB_URL},
                        text=AsyncMock(return_value=''), dispose=AsyncMock())
                if target == SPB_URL:
                    return SimpleNamespace(status=200, url=SPB_URL,
                        headers={'content-type': 'text/html'},
                        text=AsyncMock(return_value=response_html), dispose=AsyncMock())
                raise AssertionError('response helper fetched outside trusted same-path chain')
            monkeypatch.setattr(Route, 'fetch', fetch)

            client = HHClient()
            client._page, client._context = page, context
            client._ui_home = str(tmp_path)
            client._ui_guard = HHUIGuard(tmp_path, notify=AsyncMock(return_value=False))
            client._response_requires_questions = lambda current='': apply.response_requires_questions(
                client, current, logger=logging.getLogger('test.response_navigation'))
            monitor_page(client, page)
            await bootstrap_boundary(page)
            await check(client, page, context, blocked)
        finally:
            await browser.close()


@asynccontextmanager
async def configured_response_navigation(client):
    """The env switch changes only test setup; safe outcome assertions stay identical."""
    if os.getenv('HH_RESPONSE_FIX_DISABLED') == '1':
        yield
    else:
        async with response_navigation(client, URL):
            yield


def test_without_response_initializer_passive_frame_is_observed(tmp_path, monkeypatch):
    """Test-only disable switch keeps the production helper untouched."""
    async def check(client, page, context, blocked):
        assert os.getenv('HH_RESPONSE_FIX_DISABLED') == '1'

        async def direct(route):
            if route.request.url == URL and route.request.method == 'GET':
                await route.fulfill(status=200, content_type='text/html', body=FORM_HTML)
            else:
                blocked.append((route.request.method, route.request.url))
                await route.abort()
        await page.route('**/*', direct)
        await page.goto(URL, wait_until='domcontentloaded')
        await append_observed_frames(page, include=('mts',))
        await page.wait_for_timeout(100)
        assert client._page._hh_action_monitor.embedded_history
        assert not client._page._hh_action_monitor.unknown
        assert not context._hh_action_watch.unknown
        client._raise_stopped_ui('resume_preflight')
        assert context._hh_action_watch.application_post_sequence == 0
        assert blocked and all(method == 'GET' for method, _ in blocked)
        assert any('sm.rtb.mts.ru/s' in url for _, url in blocked)
    monkeypatch.setenv('HH_RESPONSE_FIX_DISABLED', '1')
    asyncio.run(browser_case(tmp_path, monkeypatch, check))


async def append_observed_frames(page, *, include=('mts', 'websocket', 'uxfeedback'), variant=None):
    """Create only frame owner shapes observed in the saved HH DOM trace."""
    cases = []
    for kind in include:
        cases.append({'kind': kind, 'variant': variant if kind == (variant or {}).get('kind') else None,
                      'srcdoc': MTS_SRCDOC})
    return await page.evaluate("""cases => {
      const rows=[];
      for(const c of cases){
        let node, parent;
        if(c.kind==='mts'){
          parent=c.variant?.parent==='body'?document.body:document.createElement('div');
          if(parent!==document.body) document.body.appendChild(parent);
          node=document.createElement('iframe'); node.id='mts-pixel'; node.setAttribute('style','display:none;'); node.setAttribute('srcdoc',c.srcdoc);
          if(c.variant?.style) node.setAttribute('style',c.variant.style);
          if(c.variant?.extra) node.setAttribute(c.variant.extra,'x');
          if(c.variant?.grammar) node.setAttribute('srcdoc',c.variant.grammar);
        } else if(c.kind==='websocket') {
          parent=c.variant?.parent==='div'?document.body.appendChild(document.createElement('div')):document.body;
          node=document.createElement('iframe'); node.id='websocket-iframe'; node.setAttribute('src','https://websocket.hh.ru/proxy-webapp/index.html'); node.setAttribute('style','display:none');
          if(c.variant?.src) node.setAttribute('src',c.variant.src);
          if(c.variant?.style) node.setAttribute('style',c.variant.style);
          if(c.variant?.extra) node.setAttribute(c.variant.extra,'x');
        } else {
          parent=c.variant?.parent==='div'?document.body.appendChild(document.createElement('div')):document.body;
          node=document.createElement('iframe'); node.setAttribute('class','uxfeedback-widget'); node.setAttribute('sandbox','allow-scripts allow-same-origin allow-forms allow-popups'); node.setAttribute('src','https://hhcdn.ru/uxfeedback/widget.html');
          if(c.variant?.class) node.setAttribute('class',c.variant.class);
          if(c.variant?.sandbox) node.setAttribute('sandbox',c.variant.sandbox);
          if(c.variant?.src) node.setAttribute('src',c.variant.src);
          if(c.variant?.extra) node.setAttribute(c.variant.extra,'x');
        }
        parent.appendChild(node);
        rows.push({kind:c.kind, attached:parent.contains(node), attrs:[...node.attributes].map(a=>a.name).sort()});
      }
      return rows;
    }""", cases)


def test_green_initializer_allows_readonly_controls_and_prevents_exact_frames(tmp_path, monkeypatch):
    async def check(client, page, context, blocked):
        async with configured_response_navigation(client):
            response = await page.goto(URL, wait_until='domcontentloaded')
            frames = await append_observed_frames(page)
            await page.wait_for_timeout(100)
            assert not client._page._hh_action_monitor.unknown, 'observed frame attachment set possible_external_action'
            assert not context._hh_action_watch.unknown
            assert [x['attached'] for x in frames] == [False, False, False]
            headers = await response.all_headers()
            csp = headers.get('content-security-policy', '')
            assert "script-src https://i.hh.ru/build/ 'unsafe-inline'" in csp
            assert "connect-src https://hh.ru/applicant/vacancy_response https://spb.hh.ru/applicant/vacancy_response" in csp
            assert "frame-src 'none'" in csp and "worker-src 'none'" in csp
            assert "form-action 'self'" in csp and "base-uri 'none'" in csp

            current, header, questions, resume, letter, submit = await apply.detect_response_controls(client)
            assert current == URL and not questions
            assert resume is not None and letter is not None and submit is not None
            assert await apply.selected_resume_matches(page, 'qa-target', '')
            assert await letter.get_attribute('data-qa') == 'vacancy-response-popup-form-letter-input'
            assert await submit.get_attribute('data-qa') == 'vacancy-response-submit-popup'
            assert await page.locator('form[name="vacancy_response"]').get_attribute('method') == 'post'
            assert await page.locator('form[name="vacancy_response"]').get_attribute('action') == '/applicant/vacancy_response?vacancyId=123'
            assert await page.evaluate('window.__jhActionBoundary != null')
            # These are inspections only: no resume click, letter fill, or submit.
        assert blocked == []
        assert not client._page._hh_action_monitor.unknown
    asyncio.run(browser_case(tmp_path, monkeypatch, check))


def test_vendor_replay_timer_iframe_preserves_safe_response_controls(tmp_path, monkeypatch):
    """The captured HH replay timer's transient hidden iframe must not poison this response flow."""
    monkeypatch.delenv('HH_RESPONSE_FIX_DISABLED', raising=False)
    response_html = FORM_HTML.replace(
        '</body>', f'<script src="{VENDOR_URL}"></script></body>'
    )

    async def check(client, page, context, blocked):
        guard_error = None
        try:
            async with response_navigation(client, URL):
                response = await page.goto(URL, wait_until='domcontentloaded')
                assert response is not None
                await page.wait_for_timeout(100)
                assert await page.evaluate('window.__replayTimerCallbackRan') is True

                current, header, questions, resume, letter, submit = await apply.detect_response_controls(client)
                assert current == URL and not questions
                assert resume is not None and letter is not None and submit is not None
                assert await apply.selected_resume_matches(page, 'qa-target', '')
                assert await page.locator('form[name="vacancy_response"]').get_attribute('method') == 'post'
                assert await page.locator('form[name="vacancy_response"]').get_attribute('action') == '/applicant/vacancy_response?vacancyId=123'
        except HHUnexpectedUI as exc:
            guard_error = exc

        assert not client._page._hh_action_monitor.unknown, 'captured Wu timer iframe set possible_external_action'
        assert not context._hh_action_watch.unknown
        assert guard_error is None, f'response initializer stopped unexpectedly: {guard_error}'
        assert blocked == []

    asyncio.run(browser_case(
        tmp_path, monkeypatch, check,
        response_html=response_html,
        resources={VENDOR_URL: VENDOR_REPLAY_JS},
    ))


@pytest.mark.parametrize('variant', ['other_bundle', 'extra_attr', 'src_attr', 'body_parent'])
def test_replay_timer_nonmatching_passive_frames_are_observed(tmp_path, monkeypatch, variant):
    """Only the observed source and exact hidden head iframe are recognized."""
    monkeypatch.delenv('HH_RESPONSE_FIX_DISABLED', raising=False)
    script_url = VENDOR_URL
    script = VENDOR_REPLAY_JS
    if variant == 'other_bundle':
        script_url = VENDOR_URL.replace('vendors.df512a23f097c519.js', 'other-vendors.js')
    elif variant == 'extra_attr':
        script = script.replace(
            'const t = n.createElement("iframe");',
            'const t = n.createElement("iframe"); t.setAttribute("data-extra", "x");',
        )
    elif variant == 'src_attr':
        script = script.replace(
            'const t = n.createElement("iframe");',
            'const t = n.createElement("iframe"); t.setAttribute("src", "about:blank");',
        )
    elif variant == 'body_parent':
        script = script.replace('n.head.appendChild(t);', 'n.body.appendChild(t);')
        script = script.replace('n.head.removeChild(t);', 'n.body.removeChild(t);')
    else:
        raise AssertionError(f'unhandled replay timer variant: {variant}')

    response_html = FORM_HTML.replace(
        '</body>', f'<script src="{script_url}"></script></body>'
    )

    async def check(client, page, context, blocked):
        guard_error = None
        try:
            async with response_navigation(client, URL):
                await page.goto(URL, wait_until='domcontentloaded')
                await page.wait_for_timeout(100)
                assert await page.evaluate('window.__replayTimerCallbackRan') is True
        except HHUnexpectedUI as exc:
            guard_error = exc

        assert client._page._hh_action_monitor.embedded_history, variant
        assert not client._page._hh_action_monitor.unknown, variant
        assert not context._hh_action_watch.unknown, variant
        assert guard_error is None, variant
        assert context._hh_action_watch.application_post_sequence == 0
        assert blocked == []

    asyncio.run(browser_case(
        tmp_path, monkeypatch, check,
        response_html=response_html,
        resources={script_url: script},
    ))


def test_unrecognized_frames_remain_unmodified_and_passive(tmp_path, monkeypatch):
    variants = [
        ('mts', {'style': 'display:block'}), ('mts', {'extra': 'title'}),
        ('mts', {'parent': 'body'}), ('mts', {'grammar': 'not-observed'}),
        ('websocket', {'src': 'https://websocket.hh.ru/proxy-webapp/other.html'}),
        ('websocket', {'style': 'display:block'}), ('websocket', {'extra': 'title'}),
        ('websocket', {'parent': 'div'}),
        ('uxfeedback', {'class': 'uxfeedback-other'}),
        ('uxfeedback', {'sandbox': 'allow-scripts allow-forms'}),
        ('uxfeedback', {'src': 'https://hhcdn.ru/uxfeedback/other.html'}),
        ('uxfeedback', {'extra': 'title'}), ('uxfeedback', {'parent': 'div'}),
    ]
    async def check(client, page, context, blocked):
        async with response_navigation(client, URL):
            await page.goto(URL, wait_until='domcontentloaded')
            for kind, variant in variants:
                rows = await append_observed_frames(page, include=(kind,), variant={'kind':kind, **variant})
                assert rows[0]['attached'] is True, (kind, variant)
            await page.wait_for_timeout(100)
            assert client._page._hh_action_monitor.embedded_history
            assert not client._page._hh_action_monitor.unknown
            assert not context._hh_action_watch.unknown
            assert blocked == []
        assert context._hh_action_watch.application_post_sequence == 0
    asyncio.run(browser_case(tmp_path, monkeypatch, check))

def fake_navigation_session():
    from hh_client import HHClient
    page = SimpleNamespace(main_frame=object())
    async def install(pattern, handler): page.handler = handler
    page.route, page.unroute = AsyncMock(side_effect=install), AsyncMock()
    context = SimpleNamespace(request=object(), add_init_script=AsyncMock())
    client = HHClient()
    client._page, client._context, client._ui_guard = page, context, object()
    return client, page


def test_existing_unknown_monitor_stops_before_route_registration():
    async def check():
        client, page = fake_navigation_session()
        page._hh_action_monitor = SimpleNamespace(unknown=True)
        client._hh_recovery_stop_reason = 'possible_external_action'
        client._hh_recovery_uncertain = True
        with pytest.raises(HHUnexpectedUI) as stopped:
            async with response_navigation(client, URL):
                pytest.fail('navigation body must not run with a sticky unknown monitor')
        assert stopped.value.hh_uncertain
        page.route.assert_not_awaited()
        page.unroute.assert_not_awaited()
    asyncio.run(check())


def fake_route(client, responses):
    class FakeResponse:
        def __init__(self, url, status, headers):
            self.url, self.status, self.headers = url, status, headers
            self.text, self.dispose = AsyncMock(return_value=FORM_HTML), AsyncMock()
    request = SimpleNamespace(method='GET', url=URL, frame=client._page.main_frame,
        is_navigation_request=lambda: True)
    route = SimpleNamespace(request=request, fetch=AsyncMock(side_effect=responses),
        fulfill=AsyncMock(), abort=AsyncMock(), fallback=AsyncMock())
    return route, FakeResponse


def test_untrusted_redirect_aborts_and_cleans_up():
    async def check():
        client, page = fake_navigation_session()
        response = SimpleNamespace(url=URL, status=302, headers={'location':'https://evil.invalid/applicant/vacancy_response?vacancyId=123'},
            text=AsyncMock(), dispose=AsyncMock())
        route, _ = fake_route(client, [response])
        with pytest.raises(ValueError, match='Unknown response redirect'):
            async with response_navigation(client, URL): await page.handler(route)
        route.abort.assert_awaited_once(); route.fulfill.assert_not_awaited()
        response.dispose.assert_awaited_once(); page.unroute.assert_awaited_once()
        route.fetch.assert_awaited_once()
    asyncio.run(check())


def test_redirect_limit_aborts_after_four_same_path_get_hops():
    async def check():
        client, page = fake_navigation_session()
        responses=[]
        for i in range(4):
            here = URL if i % 2 == 0 else SPB_URL
            there = SPB_URL if i % 2 == 0 else URL
            responses.append(SimpleNamespace(url=here,status=302,headers={'location':there},text=AsyncMock(),dispose=AsyncMock()))
        route, _ = fake_route(client, responses)
        with pytest.raises(ValueError, match='Response redirect limit exceeded'):
            async with response_navigation(client, URL): await page.handler(route)
        assert route.fetch.await_count == 4
        assert all(item.dispose.await_count == 1 for item in responses)
        route.abort.assert_awaited_once(); route.fulfill.assert_not_awaited()
        page.unroute.assert_awaited_once()
    asyncio.run(check())


def test_ownership_change_after_fetch_aborts_and_unroutes():
    async def check():
        client, page = fake_navigation_session()
        response=SimpleNamespace(url=URL,status=200,headers={'content-type':'text/html'},text=AsyncMock(return_value=FORM_HTML),dispose=AsyncMock())
        route,_=fake_route(client,[response])
        async def fetch(**kwargs):
            client._page=object()
            return response
        route.fetch.side_effect=fetch
        with pytest.raises(HHUnexpectedUI) as stopped:
            async with response_navigation(client, URL): await page.handler(route)
        assert stopped.value.hh_stop_reason == 'browser_ownership_changed'
        assert stopped.value.hh_uncertain
        route.abort.assert_awaited_once(); route.fulfill.assert_not_awaited()
        response.text.assert_not_awaited(); response.dispose.assert_awaited_once()
        page.unroute.assert_awaited_once()
    asyncio.run(check())


def test_cancelled_pending_fetch_is_aborted_and_drained():
    async def check():
        client, page = fake_navigation_session()
        started, drained = asyncio.Event(), asyncio.Event()
        route,_=fake_route(client,[])
        async def blocked_fetch(**kwargs):
            started.set()
            try: await asyncio.Future()
            finally: drained.set()
        route.fetch.side_effect=blocked_fetch
        with pytest.raises(asyncio.CancelledError):
            async with response_navigation(client, URL):
                task=asyncio.create_task(page.handler(route))
                await asyncio.wait_for(started.wait(),1)
                task.cancel()
                await task
        assert drained.is_set()
        route.abort.assert_awaited_once(); route.fulfill.assert_not_awaited()
        assert route.fetch.await_count == 1
        page.unroute.assert_awaited_once()
    asyncio.run(check())
