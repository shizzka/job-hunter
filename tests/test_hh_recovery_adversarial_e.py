"""Independent safety regressions; synthetic state and intercepted Chromium only."""
import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from hh.recovery import AUTH_URL, abandon_page, monitor_page
from hh import apply as hh_apply
from hh.ui import HHUIGuard, HHUnexpectedUI
from hh_client import HHClient
from state_store.native_apply import NativeApplyRepository, run_native_attempt
from tests.test_hh_page_recovery import FORM, OLD_URL, VACANCY, apply as fixture_apply, with_browser
from tests.test_hh_ui_isolation import CLOSE, OPTIONAL, browser_case


class Emitter:
    def __init__(self):
        self.events = {}

    def on(self, name, callback):
        self.events.setdefault(name, []).append(callback)

    def emit(self, name, argument):
        for callback in self.events.get(name, ()):
            callback(argument)


def test_unrelated_post_cannot_be_erased_by_confirmed_apply_owner(tmp_path):
    async def scenario(client, old, requests):
        repository = NativeApplyRepository(client._cookie_paths.cookies_file, 'hh')

        async def operation():
            client._external_attempt.begin()
            await old.evaluate("""Promise.all([
                fetch('/synthetic-approved-vacancy-1', {
                    method:'POST', body:'resume_id=exact-target'
                }).catch(() => {}),
                fetch('/synthetic-unrelated-vacancy-999', {
                    method:'POST', body:'resume_id=wrong-resume'
                }).catch(() => {})
            ])""")
            return {'ok': True, 'selected_resume_id': 'exact-target'}

        await run_native_attempt(client, repository, VACANCY, operation)
        assert len([request for request in requests if request[0] == 'POST']) == 2
        recovered = await abandon_page(client)
        assert not recovered.recovered, (
            'A confirmed owner must not erase an unrelated possible external action'
        )

    asyncio.run(with_browser(tmp_path, scenario))


def test_unowned_post_halts_next_details_before_navigation(tmp_path):
    async def scenario(client, old, requests):
        await old.evaluate("fetch('/synthetic-unowned-action', {method:'POST', body:'unapproved'}).catch(() => {})")
        assert old._hh_action_monitor.unknown
        old.wait_for_timeout = AsyncMock()
        with pytest.raises(HHUnexpectedUI) as stopped:
            await client.get_vacancy_details('https://hh.ru/vacancy/2')
        assert stopped.value.hh_uncertain is True
        assert stopped.value.hh_recovered is False
        assert not any(url == 'https://hh.ru/vacancy/2' for _, url in requests)

    asyncio.run(with_browser(tmp_path, scenario,
                             html='<div data-qa="vacancy-description">Synthetic ordinary page</div>'))


def test_unknown_post_during_optional_close_stops_before_next_optional_close(tmp_path):
    html = '''<meta charset="utf-8"><div id="first" role="dialog" data-qa="whats-new-modal">
<h2>Резюме стали компактнее</h2><button type="button" data-qa="modal-close">Close</button></div>
<script>
localStorage.setItem('eSecondClose', '0');
document.querySelector('#first button').onclick = () => {
    fetch('/synthetic-unknown-close-action', {method:'POST', body:'unapproved'}).catch(() => {});
    document.querySelector('#first').remove();
    const second = document.createElement('div');
    second.setAttribute('role', 'dialog');
    second.setAttribute('data-qa', 'whats-new-modal');
    second.innerHTML = '<h2>Резюме стали компактнее</h2><button type="button" data-qa="modal-close">Close</button>';
    second.querySelector('button').onclick = () => {
        localStorage.setItem('eSecondClose', '1');
        second.remove();
    };
    document.body.appendChild(second);
};
</script>'''

    async def scenario(client, old, requests):
        assert (await client._ui_guard._scan(old, ()))[0]['kind'] == 'optional'
        stopped = None
        try:
            await client._ensure_expected_ui('details_before_navigation')
        except HHUnexpectedUI as exc:
            stopped = exc
        assert old._hh_action_monitor.unknown
        assert await old.evaluate("localStorage.getItem('eSecondClose')") == '0'
        assert stopped is not None
        assert stopped.hh_uncertain is True
        assert stopped.hh_recovered is False
        assert len([method for method, _ in requests if method == 'POST']) == 1

    asyncio.run(with_browser(tmp_path, scenario, html=html))


@pytest.mark.parametrize('entry', ['details', 'search', 'apply'])
@pytest.mark.parametrize('challenge', [
    '<div role="dialog"><div data-qa="captcha">Challenge</div></div>',
    '<div data-qa="captcha">Challenge</div>',
    '<p>DDOS-GUARD: verify you are human</p>',
])
def test_preexisting_challenge_stops_automatic_entry_before_navigation(tmp_path, entry, challenge):
    html = '<meta charset="utf-8">' + challenge + '''<script>
localStorage.setItem('eChallengeEvents', '[]');
for (const name of ['beforeunload','pagehide','unload','click','submit','formdata']) {
    window.addEventListener(name, () => {
        const events = JSON.parse(localStorage.getItem('eChallengeEvents'));
        localStorage.setItem('eChallengeEvents', JSON.stringify([...events, name]));
    }, true);
}
</script>'''

    async def scenario(client, old, requests):
        old.wait_for_timeout = AsyncMock()
        client._handle_anti_bot_with_solver = AsyncMock()
        stopped = None
        try:
            if entry == 'details':
                await client.get_vacancy_details('https://hh.ru/vacancy/2')
            elif entry == 'search':
                await client._search_vacancies('synthetic', page=0,
                                              url='https://hh.ru/search/vacancy?text=synthetic')
            else:
                await fixture_apply(client)
        except HHUnexpectedUI as exc:
            stopped = exc
        # A failed attempted navigation can replace the original origin with
        # an error page. Reject the extra GET before reading that origin's state.
        assert len(requests) == 1
        events = await old.evaluate("localStorage.getItem('eChallengeEvents')")
        assert events == '[]'
        assert stopped is not None
        client._handle_anti_bot_with_solver.assert_not_awaited()

    asyncio.run(with_browser(tmp_path, scenario, html=html))


@pytest.mark.parametrize('late_surface', ['captcha', 'unknown_ui', 'unknown_post'])
def test_response_fallback_rechecks_late_stop_before_navigation(tmp_path, late_surface):
    async def scenario(client, old, requests):
        async def vacancy_route(route):
            requests.append((route.request.method, route.request.url))
            await route.fulfill(content_type='text/html', body='''<meta charset="utf-8">
<div data-qa="vacancy-description">Synthetic ordinary vacancy</div><script>
localStorage.setItem('eFallbackEvents', '[]');
for (const name of ['beforeunload','pagehide','unload']) window.addEventListener(name, () => {
    const events = JSON.parse(localStorage.getItem('eFallbackEvents'));
    localStorage.setItem('eFallbackEvents', JSON.stringify([...events, name]));
});
</script>''')

        await client._context.route(VACANCY, vacancy_route)
        old.wait_for_timeout = AsyncMock()
        client._save_debug_snapshot = AsyncMock()

        async def surface_appears_during_existing_response_check():
            if late_surface == 'unknown_post':
                await old.evaluate("fetch('/synthetic-late-unowned', {method:'POST',body:'unapproved'}).catch(() => {})")
            else:
                markup = ('<div data-qa="captcha">Challenge</div>' if late_surface == 'captcha' else
                          '<div role="dialog" data-qa="unknown"><h2>Unapproved consent</h2></div>')
                await old.evaluate('html => document.body.insertAdjacentHTML("beforeend", html)', markup)
            return False

        client._has_existing_response_ui = surface_appears_during_existing_response_check
        stopped = None
        result = None
        try:
            result = await fixture_apply(client)
        except HHUnexpectedUI as exc:
            stopped = exc
        gets = [url for method, url in requests if method == 'GET']
        assert gets.count(OLD_URL) == 1
        assert gets.count(VACANCY) == 1
        assert all(url in {OLD_URL, VACANCY, AUTH_URL} for url in gets)
        reader = old
        if old.is_closed():
            assert client._page is not old and client._page.url == 'about:blank'
            readback_url = 'https://hh.ru/synthetic-e-readback'
            async def readback(route):
                assert route.request.method == 'GET'
                requests.append(('GET', route.request.url))
                await route.fulfill(content_type='text/html', body='<body>Safe synthetic storage readback</body>')
            await client._context.route(readback_url, readback)
            await client._page.goto(readback_url)
            reader = client._page
        assert await reader.evaluate("localStorage.getItem('eFallbackEvents')") == '[]'
        assert stopped is not None or (result and result.get('hh_hard_stop'))
        if late_surface == 'unknown_post':
            assert (stopped and stopped.hh_uncertain) or (result and result.get('uncertain'))

    asyncio.run(with_browser(tmp_path, scenario))


def test_transient_frame_cannot_restore_proven_single_renderer_ownership(tmp_path):
    async def scenario(client, old, requests):
        await old.evaluate('''() => {
            const frame = document.createElement('iframe');
            document.body.appendChild(frame);
            frame.remove();
        }''')
        await old.wait_for_timeout(50)
        assert len(old.frames) == 1
        recovered = await abandon_page(client)
        assert recovered.recovered is False
        assert recovered.uncertain is True
        assert not old.is_closed()
        assert len(requests) == 1

    asyncio.run(with_browser(tmp_path, scenario))


@pytest.mark.parametrize('constructor', ['Worker', 'SharedWorker'])
def test_captured_worker_policy_blocks_creation_on_old_and_fresh_documents(tmp_path, constructor):
    async def constructor_is_refused(page):
        return await page.evaluate('''name => {
            const descriptor = Object.getOwnPropertyDescriptor(window, name);
            // Site code cannot replace the trusted constructor refusal.
            try { window[name] = function() { return {}; }; } catch (_) {}
            const source = new Blob(["fetch('/synthetic-worker-action',{method:'POST',body:'unapproved'})"],
                                    {type:'application/javascript'});
            const url = URL.createObjectURL(source);
            try {
                new window[name](url);
                return false;
            } catch (_) {
                return descriptor.writable === false && descriptor.configurable === false;
            } finally { URL.revokeObjectURL(url); }
        }''', constructor)

    async def scenario(client, old, requests):
        assert await constructor_is_refused(old)
        cdp = await client._context.new_cdp_session(old)
        try:
            targets = (await cdp.send('Target.getTargets'))['targetInfos']
            assert not any(target['type'] in {'worker', 'shared_worker', 'service_worker'} for target in targets)
        finally:
            await cdp.detach()
        assert not old._hh_action_monitor.unknown
        recovered = await abandon_page(client)
        assert recovered.recovered is True
        assert client._page.url == 'about:blank'
        assert await constructor_is_refused(client._page)
        assert all(method == 'GET' for method, _ in requests)

    asyncio.run(with_browser(tmp_path, scenario))


@pytest.mark.parametrize('entry', ['details', 'search', 'apply'])
@pytest.mark.parametrize('signal', ['captcha_url', 'login_url', 'anonymous_state'])
def test_old_challenge_or_lost_auth_signal_stops_before_automatic_navigation(tmp_path, entry, signal):
    async def scenario(client, old, requests):
        if signal != 'anonymous_state':
            path = '/captcha' if signal == 'captcha_url' else '/account/login'
            await old.evaluate('path => history.replaceState(null, "", path)', path)
        old.wait_for_timeout = AsyncMock()
        client._handle_anti_bot_with_solver = AsyncMock()
        stopped = None
        try:
            if entry == 'details':
                await client.get_vacancy_details('https://hh.ru/vacancy/2')
            elif entry == 'search':
                await client._search_vacancies('synthetic', page=0,
                                              url='https://hh.ru/search/vacancy?text=synthetic')
            else:
                await fixture_apply(client)
        except HHUnexpectedUI as exc:
            stopped = exc
        assert len(requests) == 1
        assert stopped is not None
        client._handle_anti_bot_with_solver.assert_not_awaited()

    html = '<div data-qa="vacancy-description">Synthetic ordinary page</div>'
    if signal == 'anonymous_state':
        html += '<script>window.syntheticState = {"userType":"anonymous"};</script>'
    asyncio.run(with_browser(tmp_path, scenario, html=html))


def test_late_websocket_action_upgrades_failed_receipt_and_blocks_retry(tmp_path):
    context, page = Emitter(), Emitter()
    page.context = context
    client = SimpleNamespace(_page=page, _context=context)
    monitor_page(client, page)
    repository = NativeApplyRepository(tmp_path / 'cookies.json', 'hh')

    async def operation():
        return {'ok': False, 'message': 'Synthetic preliminary refusal'}

    asyncio.run(run_native_attempt(client, repository, VACANCY, operation))
    socket = Emitter()
    page.emit('websocket', socket)
    socket.emit('framesent', '{"apply":true}')
    assert repository.get(VACANCY)['status'] == 'uncertain', (
        'A late WebSocket action must persist uncertainty for the last owned receipt'
    )
    assert not repository.claim(VACANCY, '', '')


def test_late_worker_post_upgrades_last_failed_receipt(tmp_path):
    context, page = Emitter(), Emitter()
    page.context = context
    client = SimpleNamespace(_page=page, _context=context)
    monitor_page(client, page)
    repository = NativeApplyRepository(tmp_path / 'cookies.json', 'hh')

    async def operation():
        return {'ok': False}

    asyncio.run(run_native_attempt(client, repository, VACANCY, operation))

    class WorkerRequest:
        method = 'POST'
        url = 'https://hh.ru/synthetic-worker-submit'

        @property
        def frame(self):
            raise RuntimeError('Synthetic worker request has no frame')

    context.emit('request', WorkerRequest())
    assert repository.get(VACANCY)['status'] == 'uncertain'
    assert not repository.claim(VACANCY, '', '')


def test_late_old_page_post_does_not_lose_old_receipt_to_new_page_owner(tmp_path):
    context, old, fresh = Emitter(), Emitter(), Emitter()
    old.context = fresh.context = context
    client = SimpleNamespace(_page=old, _context=context)
    monitor_page(client, old)
    monitor_page(client, fresh)
    repository = NativeApplyRepository(tmp_path / 'cookies.json', 'hh')
    next_vacancy = 'https://hh.ru/vacancy/2'

    async def first_operation():
        return {'ok': False}

    async def next_operation():
        request = SimpleNamespace(method='POST', url='https://hh.ru/synthetic-late-old-submit',
                                  frame=SimpleNamespace(page=old))
        context.emit('request', request)
        return {'ok': False}

    asyncio.run(run_native_attempt(client, repository, VACANCY, first_operation))
    client._page = fresh
    asyncio.run(run_native_attempt(client, repository, next_vacancy, next_operation))
    assert repository.get(VACANCY)['status'] == 'uncertain'
    assert not repository.claim(VACANCY, '', '')


def test_preaction_exception_cannot_leave_live_response_lifecycle_for_next_vacancy(tmp_path):
    html = FORM + """<script>
        localStorage.setItem('eLifecycle', '[]');
        for(const name of ['beforeunload', 'pagehide', 'visibilitychange', 'unload']) {
            const target = name === 'visibilitychange' ? document : window;
            target.addEventListener(name, () => {
                const prior = JSON.parse(localStorage.getItem('eLifecycle'));
                localStorage.setItem('eLifecycle', JSON.stringify([...prior, name]));
                fetch('/synthetic-lifecycle-submit', {
                    method:'POST', body:name, keepalive:true
                }).catch(() => {});
            });
        }
    </script>"""

    async def scenario(client, old, requests):
        with patch.object(hh_apply, '_apply_to_vacancy', AsyncMock(
                side_effect=RuntimeError('Synthetic preparation timeout'))):
            try:
                await fixture_apply(client)
            except RuntimeError:
                pass
        if client._page is not None and not client._page.is_closed():
            client._page.wait_for_timeout = AsyncMock()
            try:
                await client.get_vacancy_details('https://hh.ru/vacancy/2')
            except HHUnexpectedUI:
                pass
        # Keepalive request events can arrive after the next navigation returns.
        await asyncio.sleep(0.3)
        assert all(method == 'GET' for method, _ in requests), (
            'A preparatory exception must abandon or stop before old lifecycle code runs'
        )
        if client._page is not None and not client._page.is_closed():
            assert await client._page.evaluate("localStorage.getItem('eLifecycle')") == '[]'

    asyncio.run(with_browser(tmp_path, scenario, html=html))


def test_late_replacement_frame_is_not_published_after_auth_inspection(tmp_path):
    async def scenario(client, old, requests):
        get = client._context.request.get

        async def auth_response_with_late_frame(*args, **kwargs):
            response = await get(*args, **kwargs)
            dispose = response.dispose

            async def auth_dispose_then_frame():
                await dispose()
                fresh = client._context.pages[-1]
                await fresh.evaluate(
                    "document.body.appendChild(document.createElement('iframe'))"
                )

            response.dispose = auth_dispose_then_frame
            return response

        client._context.request.get = auth_response_with_late_frame
        recovered = await abandon_page(client)
        assert not recovered.recovered, (
            'An embedded frame appearing after auth inspection invalidates publication'
        )
        assert client._page is old
        assert all(method == 'GET' for method, _ in requests)

    asyncio.run(with_browser(tmp_path, scenario))


@pytest.mark.parametrize('kind', ['captcha', 'ddos_guard', 'browser_check'])
def test_automatic_apply_stops_detected_antibot_before_response_retry_or_solver(kind):
    page = SimpleNamespace(url=VACANCY, goto=AsyncMock(), wait_for_timeout=AsyncMock())
    session = SimpleNamespace(_page=page, _save_debug_snapshot=AsyncMock(),
                              _ensure_expected_ui=AsyncMock(), _detect_response_controls=AsyncMock(),
                              _detect_anti_bot_kind=AsyncMock(return_value=kind),
                              _handle_anti_bot_with_solver=AsyncMock(return_value=kind),
                              _remember_antibot_signal=Mock())
    result = asyncio.run(hh_apply._apply_to_vacancy(
        session, VACANCY, response_url='https://hh.ru/applicant/vacancy_response?vacancyId=1',
        preferred_resume_id='exact-target', absolute_hh_url=lambda value: value,
        anti_bot_message=lambda detected, where='': detected,
        logger=logging.getLogger('e-antibot-characterization'),
    ))
    assert not result['ok'] and result['anti_bot_kind'] == kind
    session._handle_anti_bot_with_solver.assert_not_awaited()
    assert page.goto.await_count == 1
    assert page.goto.await_args.args[0] == VACANCY
    session._detect_response_controls.assert_not_awaited()


@pytest.mark.parametrize('kind', ['captcha', 'ddos_guard', 'browser_check'])
def test_automatic_search_stops_detected_antibot_without_solver(kind):
    page = SimpleNamespace(url='https://hh.ru/search/vacancy', goto=AsyncMock(),
                           wait_for_timeout=AsyncMock(), query_selector_all=AsyncMock(return_value=[]))
    session = SimpleNamespace(_page=page, _ensure_expected_ui=AsyncMock(),
                              _save_debug_snapshot=AsyncMock(),
                              _detect_anti_bot_kind=AsyncMock(return_value=kind),
                              _handle_anti_bot_with_solver=AsyncMock(return_value=kind),
                              _remember_antibot_signal=Mock())
    result = asyncio.run(HHClient._search_vacancies(
        session, 'synthetic', page=0, url='https://hh.ru/search/vacancy?text=synthetic'))
    assert result == []
    session._handle_anti_bot_with_solver.assert_not_awaited()
    page.query_selector_all.assert_not_awaited()
    assert page.goto.await_count == 1
    assert session._remember_antibot_signal.call_args.args[0] == kind


@pytest.mark.parametrize('control', [
    '<input type="checkbox" name="profile_consent">',
    '<textarea name="profile_answer"></textarea>',
    '<select name="profile_city"><option>Unapproved</option></select>',
    '<div contenteditable="true">profile answer</div>',
])
def test_known_optional_title_does_not_authorize_profile_controls(tmp_path, control):
    async def check(page):
        guard = HHUIGuard(tmp_path, notify=AsyncMock(return_value=False))
        with pytest.raises(HHUnexpectedUI):
            await guard.ensure(page, 'mixed_optional')
        assert await page.evaluate('window.interactions') == []

    asyncio.run(browser_case(OPTIONAL.replace(CLOSE, control + CLOSE), check))


def test_closed_foreign_popup_keeps_uncertain_action_ownership(tmp_path):
    async def scenario(client, old, requests):
        close = AsyncMock(wraps=old.close)
        old.close = close
        opened = await old.evaluate('''() => {
            const foreign = window.open('about:blank', '_synthetic_e_closed_popup');
            if (!foreign) return false;
            foreign.close();
            return true;
        }''')
        assert opened is True
        await old.wait_for_timeout(50)
        assert len(client._context.pages) == 1 and len(old.frames) == 1
        recovered = await abandon_page(client)
        assert recovered.recovered is False and recovered.uncertain is True
        assert old._hh_action_monitor.unknown is True
        close.assert_not_awaited()
        assert len(requests) == 1

    asyncio.run(with_browser(tmp_path, scenario))
