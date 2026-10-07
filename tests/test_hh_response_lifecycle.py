"""Real exact-resume/manual-question lifecycle; requests are synthetic only."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

import config
import hh_client
from hh import apply
from hh.browser import CookieBinding
from hh.recovery import AUTH_URL, BLOCK_WORKER_CONSTRUCTORS, RecoveryResult, monitor_page
from hh.ui import HHUIGuard, HHUnexpectedUI
from state_store.hh_cookies import HHCookieRepository
from state_store.native_apply import NativeApplyRepository
from tests.test_hh_page_recovery import install_ssr_auth

HTML = (Path(__file__).parent / 'fixtures/hh/resume_picker_current.html').read_text()
NEXT = '<div data-qa="vacancy-description">Synthetic next vacancy</div>'
AUTH = '<a data-qa="resume-card-link-qa-target">Synthetic authenticated resume</a>'
VACANCY = 'https://hh.ru/vacancy/1'


async def manual_exit(tmp_path, monkeypatch, check, *, popup=False):
    monkeypatch.setattr(config, 'HH_COOKIES_FILE', str(tmp_path / 'cookies.json'))
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=[
            '--disable-background-networking', '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost'])
        try:
            context = await browser.new_context(service_workers='block')
            await context.add_init_script(script=BLOCK_WORKER_CONSTRUCTORS)
            await context.add_cookies([{'name': 'hhtoken', 'value': 'synthetic-only', 'domain': '.hh.ru', 'path': '/', 'secure': True}])
            requests, submits = [], []
            install_ssr_auth(context, requests, AUTH)
            async def fixture(route):
                requests.append((route.request.method, route.request.url))
                if route.request.method != 'GET':
                    await route.abort()
                    return
                body = AUTH if route.request.url == AUTH_URL else NEXT if route.request.url == 'https://hh.ru/vacancy/2' else HTML
                if popup and body == HTML:
                    body = body.replace('<form name="vacancy_response">', '<div role="dialog" data-qa="vacancy-response-popup"><form name="vacancy_response">').replace('</form>', '</form></div>')
                await route.fulfill(status=200, content_type='text/html', body=body)
            await context.route('**/*', fixture)
            await context.expose_binding('recordSubmit', lambda *args: submits.append(True))
            await context.add_init_script("window.addEventListener('submit', () => window.recordSubmit(), true)")
            page = await context.new_page()
            client = hh_client.HHClient()
            browser._hh_owner_session = client
            client._hh_blocked_worker_context = context
            context._hh_workers_owner = client
            client._page, client._context = page, context
            client._cookie_binding = CookieBinding(HHCookieRepository(tmp_path / 'cookies.json'), None, True, context)
            client._cookie_write_nonce = object()
            monitor_page(client, page)
            from browser_action_boundary import bootstrap_boundary
            await bootstrap_boundary(page)
            client._ui_home = str(tmp_path)
            client._ui_guard = HHUIGuard(tmp_path, notify=AsyncMock(return_value=False))
            client._save_debug_snapshot = AsyncMock()
            client._detect_anti_bot_kind = AsyncMock(return_value=None)
            client._handle_anti_bot_with_solver = AsyncMock(return_value=None)
            client._page_closed_or_archived = AsyncMock(return_value=False)
            client._has_existing_response_ui = AsyncMock(return_value=False)
            client._apply_success_detected = AsyncMock(return_value=False)
            client._response_requires_questions = AsyncMock(return_value=True)
            async def manual(**kwargs):
                selected = await page.evaluate(apply.SELECTED_RESUME_SCRIPT)
                assert set(selected['ids']) == {'qa-target'}
                return {'ok': False, 'message': 'Employer questions need manual handling'}
            client._try_auto_answer_questions = AsyncMock(side_effect=manual)
            client._submit_response_form_via_dom = AsyncMock(side_effect=AssertionError('Lifecycle tests must never submit'))
            page.wait_for_timeout = AsyncMock()
            await check(client, page)
            client._submit_response_form_via_dom.assert_not_awaited()
            assert not submits
            assert all(method == 'GET' for method, _ in requests)
        finally:
            await browser.close()


async def apply_manual(client):
    result = await client.apply_to_vacancy(VACANCY, preferred_resume_id='qa-target', preferred_resume_title='Synthetic QA')
    assert not result['ok']
    assert 'manual' in result['message']
    client._try_auto_answer_questions.assert_awaited_once()
    return result


@pytest.mark.parametrize('popup', [False, True])
def test_real_exact_picker_manual_questions_then_fresh_page_and_next_vacancy(tmp_path, monkeypatch, popup):
    async def check(client, old):
        original_guard = client._ui_guard
        result = await apply_manual(client)
        assert result['hh_page_recovered'] and not result.get('uncertain')
        assert old.is_closed() and client._page is not old
        assert client._page.context is old.context
        assert client._ui_guard is not original_guard and client._ui_guard.blocked is None
        client._page.wait_for_timeout = AsyncMock()
        assert await client.get_vacancy_details('https://hh.ru/vacancy/2') == 'Synthetic next vacancy'
        assert client._page.url == 'https://hh.ru/vacancy/2'
    asyncio.run(manual_exit(tmp_path, monkeypatch, check, popup=popup))


def test_legacy_no_abandonment_reproduces_stale_picker_failure(tmp_path, monkeypatch):
    import hh.recovery as recovery
    monkeypatch.setattr(recovery, 'abandon_page', AsyncMock(return_value=RecoveryResult(False, 'disabled_for_characterization')))
    async def check(client, old):
        await apply_manual(client)
        assert await old.locator('[data-qa="drop-base"]').count() == 1
        selected = await old.evaluate(apply.SELECTED_RESUME_SCRIPT)
        assert set(selected['ids']) == {'qa-target'}
        with pytest.raises(HHUnexpectedUI) as stopped:
            await client.get_vacancy_details('https://hh.ru/vacancy/2')
        assert stopped.value.stage == 'details_before_navigation'
        assert old.url.endswith('vacancyId=1')
    asyncio.run(manual_exit(tmp_path, monkeypatch, check))


@pytest.mark.parametrize('mutation,captcha', [
    ("document.body.insertAdjacentHTML('beforeend','<div role=dialog><h2>Unknown modal</h2><button type=button aria-label=Закрыть onclick=localStorage.setItem(\"closeCalls\",\"1\")>Close</button></div>')", False),
    ("document.body.insertAdjacentHTML('beforeend','<div role=dialog data-qa=applicant-profile-onboarding-modal><h2>Расскажите о себе</h2><button type=button aria-label=Закрыть onclick=localStorage.setItem(\"closeCalls\",\"1\")>Close</button></div>')", False),
    ("document.body.insertAdjacentHTML('beforeend','<div role=dialog data-qa=applicant-profile-completion-modal><h2>Заполните профиль</h2><button type=button aria-label=Закрыть onclick=localStorage.setItem(\"closeCalls\",\"1\")>Close</button></div>')", False),
    ("document.body.insertAdjacentHTML('beforeend','<div role=dialog><h2>Резюме стали компактнее</h2><button type=button aria-label=Закрыть onclick=localStorage.setItem(\"closeCalls\",\"1\")>Close</button></div>')", False),
    ("document.body.insertAdjacentHTML('beforeend','<div role=dialog><div data-qa=captcha>Captcha</div></div>')", True),
    ("document.querySelector('form').insertAdjacentHTML('beforeend','<div data-qa=captcha>Captcha</div>')", True),
    ("document.querySelector('form').insertAdjacentHTML('beforeend','<iframe src=https://smartcaptcha.example.test/widget></iframe>')", True),
    ("document.querySelector('form').insertAdjacentHTML('beforeend','<div class=SmartCaptcha>Challenge</div>')", True),
    ("document.querySelector('form').insertAdjacentHTML('beforeend','<div id=hh-captcha>Challenge</div>')", True),
    ("document.querySelector('form').insertAdjacentHTML('beforeend','<p>Подтвердите, что вы не робот</p>')", True),
    ("document.querySelector('form').insertAdjacentHTML('beforeend','<div class=SmartCaptcha style=display:none>Challenge</div>')", True),
    ("document.querySelector('form').insertAdjacentHTML('beforeend','<div role=dialog><h2>Unknown nested questions</h2></div>')", False),
    ("document.querySelector('[data-qa=drop-base]').dataset.qa='other-modal'", False),
    ("document.querySelector('[data-qa=drop-base]').insertAdjacentHTML('beforeend','<textarea name=profile-answer></textarea>')", False),
    ("document.querySelector('[data-magritte-select-option=qa-target] input').value='wrong-id'", False),
    ("document.querySelector('form').insertAdjacentElement('afterend',document.querySelector('form').cloneNode(true))", False),
    ("document.querySelector('form').removeAttribute('name')", False),
    ("history.replaceState(null,'','/applicant/vacancy_response?vacancyId=2')", False),
    ("history.replaceState(null,'','/applicant/vacancy_response?vacancyId=1&vacancyId=2')", False),
    ("history.replaceState(null,'','/arbitrary-page?vacancyId=1')", False),
    ("const wrapper=document.createElement('div');wrapper.setAttribute('role','dialog');wrapper.dataset.qa='unknown-consent';document.querySelector('form').before(wrapper);wrapper.append(document.querySelector('form'))", False),
])
def test_recovery_never_interacts_with_mixed_profile_or_unknown_ui(tmp_path, monkeypatch, mutation, captcha):
    async def check(client, old):
        async def manual(**kwargs):
            assert set((await old.evaluate(apply.SELECTED_RESUME_SCRIPT))['ids']) == {'qa-target'}
            await old.evaluate("localStorage.setItem('closeCalls','0')")
            await old.evaluate('() => {' + mutation + '}')
            return {'ok': False, 'message': 'manual review'}
        client._try_auto_answer_questions = AsyncMock(side_effect=manual)
        result = await apply_manual(client)
        if captcha:
            assert result['hh_hard_stop'] and not result.get('hh_page_recovered', False)
            if 'iframe' in mutation:
                assert result['uncertain'] is True
            assert not old.is_closed()
        else:
            assert result['hh_page_recovered'] and old.is_closed()
            await client._page.goto('https://hh.ru/vacancy/2')
            assert await client._page.evaluate("localStorage.getItem('closeCalls')") == '0'
    asyncio.run(manual_exit(tmp_path, monkeypatch, check))


@pytest.mark.parametrize('popup', [False, True])
@pytest.mark.parametrize('main_world_hooks', [False, True])
def test_response_abandonment_uses_no_page_dom_hooks_or_form_events(tmp_path, monkeypatch, popup, main_world_hooks):
    async def check(client, old):
        events = []
        await old.context.expose_binding('cleanupEvent', lambda source, event: events.append(event))
        async def manual(**kwargs):
            await old.evaluate("""() => {
                for(const name of ['click','submit','formdata'])window.addEventListener(name,()=>window.cleanupEvent(name),true);
            }""")
            if main_world_hooks:
                await old.evaluate("""() => {
                    Document.prototype.querySelectorAll=()=>{window.cleanupEvent('page_dom_hook');throw new Error('Page-owned hook')};
                    Element.prototype.querySelector=()=>{window.cleanupEvent('page_dom_hook');throw new Error('Page-owned hook')};
                }""")
            return {'ok':False,'message':'manual review'}
        client._try_auto_answer_questions = AsyncMock(side_effect=manual)
        result = await apply_manual(client)
        assert result['hh_page_recovered'] and old.is_closed()
        assert events == []
    asyncio.run(manual_exit(tmp_path, monkeypatch, check, popup=popup))


@pytest.mark.parametrize('moment', ['suspend', 'passive_read', 'close', 'fresh_auth'])
@pytest.mark.parametrize('signal', ['begin', 'uncertain', 'durable_uncertain', 'request'])
def test_recovery_state_is_rechecked_across_every_action_boundary(tmp_path, monkeypatch, moment, signal):
    async def check(client, old):
        injected = False
        def inject():
            nonlocal injected
            if injected:
                return
            injected = True
            attempt = client._external_attempt
            if signal == 'begin':
                with pytest.raises(RuntimeError):
                    attempt.begin()
                with pytest.raises(RuntimeError):
                    attempt.repository.transition(attempt.url, attempt.owner, 'acting')
            elif signal == 'uncertain':
                attempt.uncertain = True
            elif signal == 'durable_uncertain':
                attempt.repository.transition(attempt.url, attempt.owner, 'uncertain')
            else:
                old.context._impl_obj.emit('request', object())  # malformed evidence fails closed; no request is sent
        new_cdp = old.context.new_cdp_session
        async def captured_cdp(page):
            cdp = await new_cdp(page)
            send = cdp.send
            async def observed(method, params=None):
                if page is old and ((moment == 'suspend' and method == 'Emulation.setScriptExecutionDisabled') or
                        (moment == 'passive_read' and method == 'Runtime.evaluate')):
                    inject()
                return await send(method, params)
            cdp.send = observed
            return cdp
        monkeypatch.setattr(old.context, 'new_cdp_session', captured_cdp)
        close = old.close
        async def observed_close(**kwargs):
            if moment == 'close':
                inject()
            return await close(**kwargs)
        monkeypatch.setattr(old, 'close', observed_close)
        get = old.context.request.get
        async def observed_get(*args, **kwargs):
            if moment == 'fresh_auth':
                inject()
            return await get(*args, **kwargs)
        monkeypatch.setattr(old.context.request, 'get', observed_get)
        result = await apply_manual(client)
        assert injected
        state = NativeApplyRepository(client._cookie_paths.cookies_file, 'hh').get(VACANCY)
        if signal == 'begin':
            assert result['hh_page_recovered'] and state['status'] == 'failed'
        else:
            assert result['uncertain'] and result['hh_hard_stop'] and state['status'] == 'uncertain'
            assert not NativeApplyRepository(client._cookie_paths.cookies_file, 'hh').claim(VACANCY, '', '')
    asyncio.run(manual_exit(tmp_path, monkeypatch, check))


def test_generic_ui_alert_does_not_claim_profile_questions_were_not_filled(monkeypatch):
    import notifier
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(notifier, '_send_to_chats', send)
    target = ('synthetic-token', (1,), '')
    assert asyncio.run(notifier.notify_hh_unexpected_ui(None, 'details_before_navigation', 'a' * 64, target=target))
    method, build = send.call_args.args
    assert method == 'sendMessage'
    text = build(1)['text']
    assert 'Нестандартное поведение HH' in text and 'details_before_navigation' in text
    assert 'Вопросы профиля не заполнялись' not in text
    assert send.call_args.kwargs['target'] == target
