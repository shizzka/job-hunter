"""Offline Chromium recovery; all HH URLs are intercepted synthetic fixtures."""
import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

from browser_action_boundary import bootstrap_boundary
from hh import apply as hh_apply
from hh.browser import CookieBinding, CookiePaths
from hh.recovery import AUTH_URL, BLOCK_WORKER_CONSTRUCTORS, abandon_page, monitor_page
from hh.ui import HHUIGuard, HHUnexpectedUI
from hh_client import HHClient
from state_store.hh_cookies import HHCookieRepository
from state_store.native_apply import NativeApplyRepository, run_native_attempt

OLD_URL = 'https://hh.ru/applicant/vacancy_response?vacancyId=1'
VACANCY = 'https://hh.ru/vacancy/1'
FORM = '''<form name="vacancy_response" method="post" action="/synthetic-submit">
<input type="hidden" name="resume_id" value="exact-target"><span data-qa="resume-title">Target</span>
<button type="submit" data-qa="vacancy-response-submit-popup">Submit</button></form>'''
PICKER = '''<div role="dialog" data-qa="drop-base"><div role="listbox" data-qa="magritte-select-option-list">
<label role="option" data-magritte-select-option="exact-target"><input type="radio" name="resume" value="exact-target" checked>
<span data-qa="resume-title">Target</span></label></div></div>'''
LANDING = '<a data-qa="resume-card-link-exact-target">Synthetic authenticated resume</a>'
EFFECTS = {
    'requestSubmit': 'form.requestSubmit(button)',
    'direct_submit': 'HTMLFormElement.prototype.submit.call(form)',
    'formdata': 'new FormData(form)',
    'click': 'button.click()',
    'fetch': "fetch('/synthetic-submit',{method:'POST',body:'fixture',keepalive:true})",
    'beacon': "navigator.sendBeacon('/synthetic-submit','fixture')",
}


def install_ssr_auth(context, requests, body=LANDING):
    """APIRequest bypasses Page routes: replace it so no live HH GET is possible."""
    async def get(url, **options):
        assert url == AUTH_URL and options == {'max_redirects': 0, 'timeout': 20000}
        requests.append(('GET', url))
        return SimpleNamespace(status=200, url=url, headers={'content-type': 'text/html'},
                               text=AsyncMock(return_value=body), dispose=AsyncMock())
    context.request.get = get


async def with_browser(tmp_path, scenario, *, html=None, auth_html=LANDING):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=[
            '--disable-background-networking', '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost'])
        try:
            context = await browser.new_context(service_workers='block')
            await context.add_init_script(script=BLOCK_WORKER_CONSTRUCTORS)
            await context.add_cookies([{'name': 'hhtoken', 'value': 'synthetic-only',
                                       'domain': '.hh.ru', 'path': '/', 'secure': True}])
            requests = []
            install_ssr_auth(context, requests, auth_html)
            async def route(item):
                request = item.request
                requests.append((request.method, request.url))
                if request.method != 'GET':
                    await item.abort()
                elif request.url == OLD_URL:
                    await item.fulfill(status=200, content_type='text/html', body=html or FORM + PICKER)
                elif request.url == AUTH_URL:
                    await item.fulfill(status=200, content_type='text/html', body=auth_html)
                elif request.url == 'https://hh.ru/vacancy/2':
                    await item.fulfill(status=200, content_type='text/html', body='<div data-qa="vacancy-description">next vacancy succeeds</div>')
                else:
                    await item.abort()
            await context.route('**/*', route)
            client = HHClient()
            browser._hh_owner_session = client
            client._hh_blocked_worker_context = context
            context._hh_workers_owner = client
            client._context = context
            client._cookie_paths = CookiePaths(str(tmp_path / 'cookies.json'), str(tmp_path / 'state'))
            client._cookie_binding = CookieBinding(HHCookieRepository(tmp_path / 'cookies.json'), None, True, context)
            client._cookie_write_nonce = object()
            client._ui_home = str(tmp_path)
            client._ui_guard = HHUIGuard(tmp_path, notify=AsyncMock(return_value=False))
            client._page = await context.new_page()
            monitor_page(client, client._page)
            await bootstrap_boundary(client._page)
            await client._page.goto(OLD_URL)
            old = client._page
            await scenario(client, old, requests)
        finally:
            # No live requests are possible: all traffic is routed/aborted,
            # and DNS resolution outside localhost is disabled as well.
            await browser.close()


def apply(client):
    return hh_apply.apply_to_vacancy(client, VACANCY, preferred_resume_id='exact-target',
        absolute_hh_url=lambda value: value, anti_bot_message=lambda kind: 'captcha',
        logger=logging.getLogger('synthetic-recovery'))


def test_known_unsent_picker_does_not_poison_next_vacancy(tmp_path, monkeypatch):
    async def scenario(client, old, requests):
        old_guard, context = client._ui_guard, client._context
        monkeypatch.setattr(hh_apply, '_apply_to_vacancy', AsyncMock(return_value={'ok': False, 'needs_questions': True}))
        result = await apply(client)
        assert result['hh_page_recovered'] and not result.get('uncertain')
        assert old.is_closed() and client._page is not old and client._page.context is context
        assert client._ui_guard is not old_guard and client._page._hh_ui_guard is client._ui_guard
        assert client._ui_guard.blocked is None
        assert client._page.url == 'about:blank'
        assert await client._page.evaluate('() => !!window.__jhActionBoundary')
        assert any(cookie['name'] == 'hhtoken' for cookie in await context.cookies())
        client._page.wait_for_timeout = AsyncMock()
        assert await client.get_vacancy_details('https://hh.ru/vacancy/2') == 'next vacancy succeeds'
        assert all(method == 'GET' for method, url in requests)
    asyncio.run(with_browser(tmp_path, scenario))


@pytest.mark.parametrize('event', ['beforeunload', 'unload', 'pagehide', 'visibilitychange'])
@pytest.mark.parametrize('effect', list(EFFECTS))
def test_recovery_runs_zero_lifecycle_or_form_events(tmp_path, monkeypatch, event, effect):
    handler_target = 'document' if event == 'visibilitychange' else 'window'
    html = FORM + PICKER + '''<script>
localStorage.setItem('events','[]');
const record=name=>{const values=JSON.parse(localStorage.getItem('events'));values.push(name);localStorage.setItem('events',JSON.stringify(values));};
const form=document.querySelector('form'),button=document.querySelector('button');
for(const name of ['click','submit','formdata'])window.addEventListener(name,e=>{record(name);if(name==='submit')e.preventDefault()},true);
form.addEventListener('formdata',()=>fetch('/synthetic-formdata',{method:'POST',body:'fixture'}));
HANDLER
</script>'''.replace('HANDLER', handler_target + '.addEventListener(' + repr(event) + ",()=>{record('lifecycle');" + EFFECTS[effect] + ';});')
    async def scenario(client, old, requests):
        monkeypatch.setattr(hh_apply, '_apply_to_vacancy', AsyncMock(return_value={'ok': False, 'needs_questions': True}))
        result = await apply(client)
        assert result['hh_page_recovered']
        await client._page.goto('https://hh.ru/vacancy/2')
        assert await client._page.evaluate("JSON.parse(localStorage.getItem('events'))") == []
        assert all(method == 'GET' for method, url in requests)
    asyncio.run(with_browser(tmp_path, scenario, html=html))


def test_unexpected_apply_recovers_inside_owned_attempt_and_new_guard(tmp_path, monkeypatch):
    html = FORM + '<div role="dialog" data-qa="unknown"><h2>Unknown</h2><button type="button" onclick="window.unsafe++">Close</button></div><script>localStorage.setItem("unsafe","0")</script>'
    async def scenario(client, old, requests):
        captured = {}
        async def operation(*args, **kwargs):
            captured['attempt'] = client._external_attempt
            await client._ensure_expected_ui('apply_unexpected', allowed=('response',))
        monkeypatch.setattr(hh_apply, '_apply_to_vacancy', operation)
        with pytest.raises(HHUnexpectedUI) as stopped:
            await apply(client)
        assert stopped.value.hh_recovered is True
        assert old.is_closed() and client._page is not old and client._ui_guard.blocked is None
        assert captured['attempt'].abandoned
        assert client._external_attempt is None
        await client._page.goto('https://hh.ru/vacancy/2')
        assert await client._page.evaluate("localStorage.getItem('unsafe')") == '0'
        repo = NativeApplyRepository(client._cookie_paths.cookies_file, 'hh')
        assert repo.get(VACANCY)['status'] == 'failed'
    asyncio.run(with_browser(tmp_path, scenario, html=html))


@pytest.mark.parametrize('kind', ['acting', 'durable_uncertain', 'adapter_uncertain', 'missing_monitor', 'foreign_owner'])
def test_possible_action_or_unknown_ownership_never_recovers(tmp_path, monkeypatch, kind):
    async def scenario(client, old, requests):
        async def operation(*args, **kwargs):
            attempt = client._external_attempt
            if kind == 'acting':
                attempt.begin()
            elif kind == 'durable_uncertain':
                attempt.repository.transition(VACANCY, attempt.owner, 'uncertain')
            elif kind == 'missing_monitor':
                del old._hh_action_monitor
            elif kind == 'foreign_owner':
                attempt.repository.store.update(lambda state: {key: {**item, 'owner': 'f' * 32} for key, item in state.items()})
            return {'ok': False, 'uncertain': kind == 'adapter_uncertain'}
        monkeypatch.setattr(hh_apply, '_apply_to_vacancy', operation)
        result = await apply(client)
        assert result['uncertain'] and result['ok'] is False
        assert result.get('hh_hard_stop')
        assert not old.is_closed() and client._page is old
        assert len(client._context.pages) == 1
        repo = NativeApplyRepository(client._cookie_paths.cookies_file, 'hh')
        assert not repo.claim(VACANCY, '', '')
    asyncio.run(with_browser(tmp_path, scenario))


@pytest.mark.parametrize('auth_html', ['<p>No positive account marker</p>', LANDING + '<div data-qa="captcha">challenge</div>', LANDING + '<div role="dialog">Unexpected</div>'])
def test_unproven_fresh_auth_stops_without_interacting(tmp_path, monkeypatch, auth_html):
    async def scenario(client, old, requests):
        monkeypatch.setattr(hh_apply, '_apply_to_vacancy', AsyncMock(return_value={'ok': False, 'needs_questions': True}))
        result = await apply(client)
        assert result['hh_hard_stop'] and not result['hh_page_recovered']
        assert all(method == 'GET' for method, url in requests)
        assert client._page is old
    asyncio.run(with_browser(tmp_path, scenario, auth_html=auth_html))


@pytest.mark.parametrize('marker', ['<div data-qa="captcha">challenge</div>', '<p>Verify you are human</p>'])
def test_old_captcha_requires_intervention_and_is_not_discarded(tmp_path, monkeypatch, marker):
    async def scenario(client, old, requests):
        monkeypatch.setattr(hh_apply, '_apply_to_vacancy', AsyncMock(return_value={'ok': False, 'anti_bot_kind': 'captcha'}))
        result = await apply(client)
        assert result['hh_hard_stop'] and result['hh_recovery_reason'] == 'captcha_or_antibot'
        assert not old.is_closed() and len(client._context.pages) == 1
    asyncio.run(with_browser(tmp_path, scenario, html=FORM + marker))


def test_recovery_reservation_blocks_live_and_durable_dispatch_before_first_await(tmp_path, monkeypatch):
    async def scenario(client, old, requests):
        original = client._context.new_cdp_session
        inspected = []
        async def first_await(page):
            attempt = client._external_attempt
            assert attempt.abandoned
            state = attempt.repository.get(attempt.url)
            assert state['status'] == 'preparing' and state['recovery_sealed']
            assert not attempt.repository.claim(attempt.url, '', '')
            for status in ('acting', 'completed'):
                with pytest.raises(RuntimeError):
                    attempt.repository.transition(attempt.url, attempt.owner, status)
            with pytest.raises(RuntimeError):
                attempt.begin()
            inspected.append(True)
            return await original(page)
        client._context.new_cdp_session = first_await
        monkeypatch.setattr(hh_apply, '_apply_to_vacancy', AsyncMock(return_value={'ok': False, 'needs_questions': True}))
        result = await apply(client)
        assert result['hh_page_recovered'] and inspected == [True]
    asyncio.run(with_browser(tmp_path, scenario))


def test_unowned_post_before_suspension_is_sticky_uncertain(tmp_path, monkeypatch):
    async def scenario(client, old, requests):
        original = client._context.new_cdp_session
        async def first_await(page):
            await page.evaluate("fetch('/synthetic-submit',{method:'POST',body:'fixture'}).catch(()=>{})")
            return await original(page)
        client._context.new_cdp_session = first_await
        monkeypatch.setattr(hh_apply, '_apply_to_vacancy', AsyncMock(return_value={'ok': False, 'needs_questions': True}))
        result = await apply(client)
        assert result['uncertain'] and result['hh_hard_stop']
        assert not old.is_closed()
        repo = NativeApplyRepository(client._cookie_paths.cookies_file, 'hh')
        assert repo.get(VACANCY)['status'] == 'uncertain' and not repo.claim(VACANCY, '', '')
        assert any(method == 'POST' for method, url in requests)  # route aborted, never sent to HH
    asyncio.run(with_browser(tmp_path, scenario))


def test_confirmed_completed_owner_does_not_poison_later_safe_recovery(tmp_path):
    async def scenario(client, old, requests):
        repo = NativeApplyRepository(client._cookie_paths.cookies_file, 'hh')
        async def operation():
            from hh.submit_boundary import arm_submit_boundary, bind_submit_control
            client._approved_hh_payload = {'resume_id': 'exact-target', 'cover_letter': ''}
            button = await old.query_selector('[data-qa="vacancy-response-submit-popup"]')
            assert await arm_submit_boundary(client)
            assert await bind_submit_control(client, button)
            client._external_attempt.begin()
            await old.evaluate("fetch('/synthetic-submit',{method:'POST',headers:{'Content-Type':'application/x-www-form-urlencoded'},body:'resume_id=exact-target'}).catch(()=>{})")
            from browser_action_boundary import release_boundary
            await release_boundary(old, client._submit_boundary_id)
            return {'ok': True}
        result = await run_native_attempt(client, repo, VACANCY, operation)
        assert result['ok'] and repo.get(VACANCY)['status'] == 'completed'
        recovered = await abandon_page(client)
        assert recovered.recovered
    asyncio.run(with_browser(tmp_path, scenario))


@pytest.mark.parametrize('failure', ['get', 'completion_write', 'foreign_owner'])
def test_receipt_finalization_failure_after_action_is_uncertain(tmp_path, failure):
    client = SimpleNamespace(_page=None, _context=None)
    repo = NativeApplyRepository(tmp_path / 'cookies.json', 'hh')
    original_get, original_transition = repo.get, repo.transition
    async def operation():
        client._external_attempt.begin()
        if failure == 'get':
            repo.get = lambda url: (_ for _ in ()).throw(OSError('synthetic read failure'))
        elif failure == 'completion_write':
            def broken_transition(url, owner, status, **kwargs):
                if status == 'completed':
                    raise OSError('synthetic completion write failure')
                return original_transition(url, owner, status, **kwargs)
            repo.transition = broken_transition
        else:
            repo.store.update(lambda state: {key: {**item, 'owner': 'f' * 32} for key, item in state.items()})
        return {'ok': True}
    result = asyncio.run(run_native_attempt(client, repo, VACANCY, operation))
    assert result['uncertain'] and result['ok'] is False
    assert original_get(VACANCY)['status'] in {'acting', 'uncertain'}
    assert not repo.claim(VACANCY, '', '')


def test_claim_persistence_failure_is_uncertain_without_operation(tmp_path):
    client = SimpleNamespace(_page=None, _context=None)
    repo = NativeApplyRepository(tmp_path / 'cookies.json', 'hh')
    repo.claim = lambda *args: (_ for _ in ()).throw(OSError('synthetic claim persistence failure'))
    operation = AsyncMock(return_value={'ok': True})
    result = asyncio.run(run_native_attempt(client, repo, VACANCY, operation))
    assert result['uncertain'] and result['reason'] == 'native_ownership_unproven'
    operation.assert_not_awaited()


def test_force_oopif_is_abandoned_by_hard_stop_without_page_close(tmp_path, monkeypatch):
    async def scenario():
        callbacks = []
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=[
                '--disable-background-networking', '--site-per-process',
                '--isolate-origins=https://other.invalid',
                '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost'])
            try:
                context = await browser.new_context(service_workers='block')
                await context.add_cookies([{'name': 'hhtoken', 'value': 'synthetic-only', 'domain': '.hh.ru', 'path': '/', 'secure': True}])
                await context.expose_function('recordLeak', lambda name: callbacks.append(name))
                async def route(item):
                    if item.request.method != 'GET':
                        await item.abort()
                    elif item.request.url == OLD_URL:
                        await item.fulfill(status=200, content_type='text/html', body=FORM + '<iframe src="https://other.invalid/inner"></iframe>')
                    elif item.request.url == 'https://other.invalid/inner':
                        await item.fulfill(status=200, content_type='text/html', body='''<form><button>Submit</button></form><script>
window.addEventListener('pagehide',()=>{window.recordLeak('pagehide');new FormData(document.querySelector('form'))});
window.addEventListener('submit',()=>window.recordLeak('submit'),true);</script>''')
                    else:
                        await item.abort()
                await context.route('**/*', route)
                client = HHClient()
                client._browser = browser
                browser._hh_owner_session = client
                client._context = context
                client._cookie_paths = CookiePaths(str(tmp_path / 'cookies.json'), str(tmp_path / 'state'))
                client._cookie_binding = CookieBinding(HHCookieRepository(tmp_path / 'cookies.json'), None, True, context)
                client._cookie_write_nonce = object()
                client._ui_guard = HHUIGuard(tmp_path, notify=AsyncMock(return_value=False))
                old = await context.new_page()
                client._page = old
                monitor_page(client, old)
                await bootstrap_boundary(old)
                await old.goto(OLD_URL)
                assert len(old.frames) == 2
                browser_cdp = await browser.new_browser_cdp_session()
                targets = await browser_cdp.send('Target.getTargets')
                assert any(target['type'] == 'iframe' for target in targets['targetInfos'])
                await browser_cdp.detach()
                old.close = AsyncMock(wraps=old.close)
                context.new_page = AsyncMock(wraps=context.new_page)
                operation = AsyncMock(return_value={'ok': False, 'needs_questions': True})
                monkeypatch.setattr(hh_apply, '_apply_to_vacancy', operation)
                result = await apply(client)
                assert result['hh_hard_stop'] and result['uncertain']
                assert result['reason'] == 'native_client_busy'
                operation.assert_not_awaited()
                assert await client.hard_stop_browser() is True
                old.close.assert_not_awaited()
                context.new_page.assert_not_awaited()
                assert callbacks == []
            finally:
                from hh.termination import terminate_browser
                assert await terminate_browser(browser) is True
            assert callbacks == []
    asyncio.run(scenario())
