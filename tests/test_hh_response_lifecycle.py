"""Response lifecycle regressions: every request is fulfilled locally."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

import config
import hh_client
from hh import apply
from hh.ui import HHUIGuard, HHUnexpectedUI

HTML = (Path(__file__).parent / "fixtures/hh/resume_picker_current.html").read_text()
NEXT = '<div data-qa="vacancy-description">Synthetic next vacancy</div>'


async def manual_exit(tmp_path, monkeypatch, check, *, popup=False):
    monkeypatch.setattr(config, "HH_COOKIES_FILE", str(tmp_path / "cookies.json"))
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            requests, submits = [], []
            async def fixture(route):
                requests.append((route.request.method, route.request.url))
                body = NEXT if route.request.url == "https://hh.ru/vacancy/2" else HTML
                if popup and body == HTML:
                    body = body.replace('<form name="vacancy_response">', '<div role="dialog" data-qa="vacancy-response-popup"><form name="vacancy_response">').replace('</form>', '</form></div>')
                await route.fulfill(status=200, content_type="text/html", body=body)
            await context.route("**/*", fixture)
            await context.expose_binding("recordSubmit", lambda *args: submits.append(True))
            await context.add_init_script("window.addEventListener('submit', () => window.recordSubmit(), true)")
            page = await context.new_page()
            from browser_action_boundary import bootstrap_boundary
            await bootstrap_boundary(page)
            client = hh_client.HHClient()
            client._page = page
            client._ui_home = str(tmp_path)
            client._ui_guard = HHUIGuard(tmp_path, notify=AsyncMock(return_value=False))
            client._save_debug_snapshot = AsyncMock()
            client._detect_anti_bot_kind = AsyncMock(return_value=None)
            client._handle_anti_bot_with_solver = AsyncMock(return_value=None)
            client._page_closed_or_archived = AsyncMock(return_value=False)
            client._has_existing_response_ui = AsyncMock(return_value=False)
            client._apply_success_detected = AsyncMock(return_value=False)
            client._response_requires_questions = AsyncMock(return_value=True)
            client._try_auto_answer_questions = AsyncMock(return_value={"ok": False, "message": "Employer questions need manual handling"})
            client._submit_response_form_via_dom = AsyncMock(side_effect=AssertionError("Lifecycle tests must never submit"))
            page.wait_for_timeout = AsyncMock()
            await check(client, page)
            client._submit_response_form_via_dom.assert_not_awaited()
            assert not submits
            assert all(method == "GET" for method, _ in requests)
        finally:
            await browser.close()


async def apply_manual(client):
    result = await client.apply_to_vacancy("https://hh.ru/vacancy/1", preferred_resume_id="qa-target", preferred_resume_title="Synthetic QA")
    assert not result["ok"]
    assert "manual" in result["message"]
    client._try_auto_answer_questions.assert_awaited_once()
    return result


@pytest.mark.parametrize("popup", [False, True])
def test_manual_question_exit_allows_next_vacancy(tmp_path, monkeypatch, popup):
    async def check(client, page):
        await apply_manual(client)
        assert page.url == "about:blank"
        assert client._ui_guard.blocked is None
        assert await client.get_vacancy_details("https://hh.ru/vacancy/2") == "Synthetic next vacancy"
        assert page.url == "https://hh.ru/vacancy/2"
    asyncio.run(manual_exit(tmp_path, monkeypatch, check, popup=popup))


def test_legacy_without_cleanup_reproduces_stale_picker_failure(tmp_path, monkeypatch):
    monkeypatch.setattr(apply, "leave_known_response_ui", AsyncMock(return_value=False), raising=False)
    async def check(client, page):
        await apply_manual(client)
        assert await page.locator('[data-qa="drop-base"]').count() == 1
        selected = await page.evaluate(apply.SELECTED_RESUME_SCRIPT)
        assert set(selected["ids"]) == {"qa-target"}
        with pytest.raises(HHUnexpectedUI) as raised:
            await client.get_vacancy_details("https://hh.ru/vacancy/2")
        assert raised.value.stage == "details_before_navigation"
        assert page.url.endswith("vacancyId=1")
    asyncio.run(manual_exit(tmp_path, monkeypatch, check))


@pytest.mark.parametrize("mutation", [
    "document.body.insertAdjacentHTML('beforeend', '<div role=dialog><h2>Unknown modal</h2><button type=button aria-label=Закрыть onclick=window.closeCalls++>Close</button></div>')",
    "document.body.insertAdjacentHTML('beforeend', '<div role=dialog data-qa=applicant-profile-onboarding-modal><h2>Расскажите о себе</h2><button type=button aria-label=Закрыть onclick=window.closeCalls++>Close</button></div>')",
    "document.body.insertAdjacentHTML('beforeend', '<div role=dialog data-qa=applicant-profile-completion-modal><h2>Заполните профиль</h2><button type=button aria-label=Закрыть onclick=window.closeCalls++>Close</button></div>')",
    "document.body.insertAdjacentHTML('beforeend', '<div role=dialog><h2>Резюме стали компактнее</h2><button type=button aria-label=Закрыть onclick=window.closeCalls++>Close</button></div>')",
    "document.body.insertAdjacentHTML('beforeend', '<div role=dialog><div data-qa=captcha>Captcha</div></div>')",
    "document.querySelector('form').insertAdjacentHTML('beforeend', '<div data-qa=captcha>Captcha</div>')",
    "document.querySelector('form').insertAdjacentHTML('beforeend', '<iframe src=https://smartcaptcha.example.test/widget></iframe>')",
    "document.querySelector('form').insertAdjacentHTML('beforeend', '<div class=SmartCaptcha>Challenge</div>')",
    "document.querySelector('form').insertAdjacentHTML('beforeend', '<div id=hh-captcha>Challenge</div>')",
    "document.querySelector('form').insertAdjacentHTML('beforeend', '<p>Подтвердите, что вы не робот</p>')",
    "document.querySelector('form').insertAdjacentHTML('beforeend', '<div class=SmartCaptcha style=display:none>Challenge</div>')",
    "document.querySelector('form').insertAdjacentHTML('beforeend', '<div role=dialog><h2>Unknown nested questions</h2></div>')",
    "document.querySelector('[data-qa=drop-base]').dataset.qa='other-modal'",
    "document.querySelector('[data-qa=drop-base]').insertAdjacentHTML('beforeend', '<textarea name=profile-answer></textarea>')",
    "document.querySelector('[data-magritte-select-option=qa-target] input').value='wrong-id'",
    "document.querySelector('form').insertAdjacentElement('afterend', document.querySelector('form').cloneNode(true))",
    "document.querySelector('form').removeAttribute('name')",
    "history.replaceState(null, '', '/applicant/vacancy_response?vacancyId=2')",
    "history.replaceState(null, '', '/applicant/vacancy_response?vacancyId=1&vacancyId=2')",
    "history.replaceState(null, '', '/arbitrary-page?vacancyId=1')",
    "const wrapper=document.createElement('div'); wrapper.setAttribute('role','dialog'); wrapper.dataset.qa='arbitrary-modal'; document.querySelector('form').before(wrapper); wrapper.append(document.querySelector('form'))",
])
def test_manual_cleanup_preserves_unproven_or_mixed_ui(tmp_path, monkeypatch, mutation):
    async def check(client, page):
        async def manual(**kwargs):
            assert set((await page.evaluate(apply.SELECTED_RESUME_SCRIPT))["ids"]) == {"qa-target"}
            await page.evaluate("window.closeCalls = 0")
            await page.evaluate("() => {" + mutation + "}")
            return {"ok": False, "message": "manual review"}
        client._try_auto_answer_questions = AsyncMock(side_effect=manual)
        await apply_manual(client)
        assert page.url != "about:blank"
        assert await page.evaluate("window.closeCalls") == 0
        with pytest.raises(HHUnexpectedUI) as raised:
            await client.get_vacancy_details("https://hh.ru/vacancy/2")
        assert raised.value.stage == "response_cleanup"
        assert client._ui_guard.blocked is raised.value
        assert await page.evaluate("window.closeCalls") == 0
    asyncio.run(manual_exit(tmp_path, monkeypatch, check))


def test_employer_questionnaire_url_is_cleaned_without_answering_or_submitting(tmp_path, monkeypatch):
    async def check(client, page):
        async def manual(**kwargs):
            await page.evaluate("history.replaceState(null, '', '/applicant/vacancy_response_question?vacancyId=1')")
            return {"ok": False, "message": "manual review"}
        client._try_auto_answer_questions = AsyncMock(side_effect=manual)
        await apply_manual(client)
        assert page.url == "about:blank"
        assert await client.get_vacancy_details("https://hh.ru/vacancy/2") == "Synthetic next vacancy"
    asyncio.run(manual_exit(tmp_path, monkeypatch, check))


def test_cleanup_rechecks_dom_at_navigation_commit(tmp_path, monkeypatch):
    async def check(client, page):
        new_cdp = page.context.new_cdp_session
        async def changed_session(target):
            cdp = await new_cdp(target)
            send = cdp.send
            async def changed(method, params=None):
                if method == "Runtime.evaluate" and "codex:hh-response-leave" in params.get("expression", ""):
                    await page.evaluate("document.body.insertAdjacentHTML('beforeend', '<div role=dialog><h2>Late unknown UI</h2></div>')")
                return await send(method, params)
            cdp.send = changed
            return cdp
        monkeypatch.setattr(page.context, "new_cdp_session", changed_session)
        await apply_manual(client)
        assert page.url != "about:blank"
        with pytest.raises(HHUnexpectedUI):
            await client.get_vacancy_details("https://hh.ru/vacancy/2")
    asyncio.run(manual_exit(tmp_path, monkeypatch, check))


@pytest.mark.parametrize("outcome", ["success", "acting", "uncertain", "exception"])
def test_no_cleanup_of_success_dispatched_submit_or_ui_stop(tmp_path, monkeypatch, outcome):
    cleanup = AsyncMock(return_value=True)
    monkeypatch.setattr(apply, "leave_known_response_ui", cleanup)
    async def check(client, page):
        blocked = HHUnexpectedUI("synthetic", "a" * 64)
        async def operation(session, *args, **kwargs):
            await page.goto("https://hh.ru/applicant/vacancy_response?vacancyId=1")
            if outcome == "exception":
                session._ui_guard.blocked = blocked
                raise blocked
            if outcome == "acting":
                session._external_attempt.begin()
            return {"ok": outcome == "success", "uncertain": outcome == "uncertain", "message": "synthetic result"}
        monkeypatch.setattr(apply, "_apply_to_vacancy", operation)
        if outcome == "exception":
            with pytest.raises(HHUnexpectedUI) as raised:
                await client.apply_to_vacancy("https://hh.ru/vacancy/1", preferred_resume_id="qa-target")
            assert raised.value is blocked and client._ui_guard.blocked is blocked
        else:
            result = await client.apply_to_vacancy("https://hh.ru/vacancy/1", preferred_resume_id="qa-target")
            assert result.get("uncertain") if outcome in {"acting", "uncertain"} else result["ok"]
        cleanup.assert_not_awaited()
        assert page.url.endswith("vacancyId=1")
    asyncio.run(manual_exit(tmp_path, monkeypatch, check))


def test_generic_ui_alert_does_not_claim_profile_questions_were_not_filled(monkeypatch):
    import notifier
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(notifier, "_send_to_chats", send)
    target = ("synthetic-token", (1,), "")
    assert asyncio.run(notifier.notify_hh_unexpected_ui(None, "details_before_navigation", "a" * 64, target=target))
    method, build = send.call_args.args
    assert method == "sendMessage"
    text = build(1)["text"]
    assert "Нестандартное поведение HH" in text and "details_before_navigation" in text
    assert "Вопросы профиля не заполнялись" not in text
    assert send.call_args.kwargs["target"] == target


@pytest.mark.parametrize("owned,acting,blocked", [(False, False, False), (True, True, False), (True, False, True)])
def test_cleanup_requires_unsent_ownership_and_preserves_sticky_block(tmp_path, owned, acting, blocked):
    from types import SimpleNamespace
    from hh.ui import leave_known_response_ui
    guard = HHUIGuard(tmp_path, notify=AsyncMock())
    stop = HHUnexpectedUI("synthetic", "a" * 64)
    guard.blocked = stop if blocked else None
    page = SimpleNamespace(evaluate=AsyncMock(), wait_for_url=AsyncMock())
    session = SimpleNamespace(_page=page, _ui_guard=guard,
        _external_attempt=SimpleNamespace(acting=acting) if owned else None)
    if blocked:
        with pytest.raises(HHUnexpectedUI) as raised:
            asyncio.run(leave_known_response_ui(session, "1"))
        assert raised.value is stop and guard.blocked is stop
    else:
        assert asyncio.run(leave_known_response_ui(session, "1")) is False
    page.evaluate.assert_not_awaited()
    page.wait_for_url.assert_not_awaited()


NESTED_WRAPPER = r"""() => {
    const form = document.querySelector('form');
    const unknown = document.createElement('div');
    unknown.setAttribute('role', 'dialog'); unknown.dataset.qa = 'unknown-consent-modal';
    unknown.innerHTML = '<h2>Unknown consent</h2><input type=checkbox name=consent>';
    form.before(unknown); unknown.append(form);
}"""


def test_unknown_dialog_wrapping_form_inside_known_popup_refuses_cleanup(tmp_path, monkeypatch):
    from hh.ui import leave_known_response_ui
    async def check(client, page):
        cleanup_results = []
        cleanup = apply.leave_known_response_ui
        async def record_cleanup(*args):
            result = await cleanup(*args)
            cleanup_results.append(result)
            return result
        monkeypatch.setattr(apply, "leave_known_response_ui", record_cleanup)
        async def manual(**kwargs):
            await page.evaluate(NESTED_WRAPPER)
            await page.evaluate("window.cleanupClicks=0; document.addEventListener('click',()=>window.cleanupClicks++,true)")
            return {"ok": False, "message": "manual review"}
        client._try_auto_answer_questions = AsyncMock(side_effect=manual)
        await apply_manual(client)
        assert cleanup_results == [False]
        assert page.url.endswith("vacancyId=1")
        assert await page.locator('[data-qa=unknown-consent-modal]').count() == 1
        assert await page.evaluate("[window.submitCalls,window.cleanupClicks]") == [0, 0]
        stop = client._ui_guard.blocked
        assert isinstance(stop, HHUnexpectedUI)
        for _ in range(2):
            with pytest.raises(HHUnexpectedUI) as raised:
                await client.get_vacancy_details("https://hh.ru/vacancy/2")
            assert raised.value is stop
        assert await page.evaluate("[window.submitCalls,window.cleanupClicks]") == [0, 0]
    asyncio.run(manual_exit(tmp_path, monkeypatch, check, popup=True))


@pytest.mark.parametrize("event,target", [
    ("beforeunload", "window"), ("unload", "window"), ("pagehide", "window"),
    ("visibilitychange", "document"), ("beforeunload", "document.body"),
])
@pytest.mark.parametrize("effect", ["unknown", "submit", "formdata", "click", "direct_submit", "dispatch_submit", "inline"])
def test_lifecycle_side_effects_refuse_cleanup_without_dispatch(tmp_path, monkeypatch, event, target, effect):
    from state_store.native_apply import NativeApplyRepository
    async def check(client, page):
        observed = []
        await page.context.expose_binding("cleanupEvent", lambda source, event: observed.append(event))
        cleanup_results = []
        cleanup = apply.leave_known_response_ui
        async def record_cleanup(*args):
            result = await cleanup(*args)
            cleanup_results.append(result)
            return result
        monkeypatch.setattr(apply, "leave_known_response_ui", record_cleanup)
        async def manual(**kwargs):
            effects = {
                "unknown": "document.body.insertAdjacentHTML('beforeend','<div role=dialog>Late unknown</div>')",
                "submit": "document.querySelector('form').requestSubmit()",
                "formdata": "new FormData(document.querySelector('form'))",
                "click": "document.querySelector('button[type=submit]').click()",
                "direct_submit": "document.querySelector('form').submit()",
                "dispatch_submit": "document.querySelector('form').dispatchEvent(new Event('submit',{bubbles:true,cancelable:true}))",
                "inline": "document.querySelector('form').requestSubmit()",
            }
            await page.evaluate("""() => {
                for (const name of ['click','submit','formdata'])
                    window.addEventListener(name,()=>window.cleanupEvent(name),true);
            }""")
            handler = "() => {window.cleanupEvent('lifecycle');" + effects[effect] + "}"
            registration = (target + ".on" + event + " = " + handler if effect == "inline" else
                            target + ".addEventListener(" + repr(event) + "," + handler + ")")
            # DOM0 pagehide/visibilitychange on body is not a browser handler;
            # the explicit listener path is used for these synthetic targets.
            if effect == "inline" and target == "document.body":
                registration = target + ".addEventListener(" + repr(event) + "," + handler + ")"
            await page.evaluate("() => {" + registration + ";}")
            return {"ok": False, "message": "manual review"}
        client._try_auto_answer_questions = AsyncMock(side_effect=manual)
        await apply_manual(client)
        assert cleanup_results == [False]
        assert page.url.endswith("vacancyId=1")
        assert observed == []
        assert await page.evaluate("window.submitCalls") == 0
        repo = NativeApplyRepository(client._cookie_paths.cookies_file, "hh")
        assert repo.get("https://hh.ru/vacancy/1")["status"] == "failed"
        with pytest.raises(HHUnexpectedUI):
            await client.get_vacancy_details("https://hh.ru/vacancy/2")
        assert observed == []
    asyncio.run(manual_exit(tmp_path, monkeypatch, check, popup=True))


@pytest.mark.parametrize("moment", ["proof", "commit_receipt", "post_navigation", "restore"])
@pytest.mark.parametrize("signal", ["acting", "uncertain", "durable_uncertain", "request"])
def test_cleanup_action_state_change_preserves_uncertain_and_hard_stops(tmp_path, monkeypatch, moment, signal):
    from state_store.native_apply import NativeApplyRepository
    async def check(client, page):
        new_cdp = page.context.new_cdp_session
        injected = False
        async def session(target):
            cdp = await new_cdp(target)
            send = cdp.send
            async def changed(method, params=None):
                nonlocal injected
                expression = (params or {}).get("expression", "")
                matches = ((moment == "proof" and "codex:hh-response-leave" in expression) or
                           (moment == "commit_receipt" and "codex:hh-response-leave" in expression) or
                           (moment == "post_navigation" and method == "Page.getFrameTree" and page.url == "about:blank") or
                           (moment == "restore" and method == "Emulation.setScriptExecutionDisabled" and not params["value"]))
                receipt = await send(method, params) if matches and moment == "commit_receipt" else None
                if matches and not injected:
                    injected = True
                    attempt = client._external_attempt
                    if signal == "acting":
                        attempt.begin()
                    elif signal == "uncertain":
                        attempt.uncertain = True
                    elif signal == "durable_uncertain":
                        attempt.repository.transition(attempt.url, attempt.owner, "uncertain")
                    else:
                        page._impl_obj.emit("request", object())
                return receipt if receipt is not None else await send(method, params)
            cdp.send = changed
            return cdp
        monkeypatch.setattr(page.context, "new_cdp_session", session)
        cleanup_results = []
        cleanup = apply.leave_known_response_ui
        async def record_cleanup(*args):
            result = await cleanup(*args)
            cleanup_results.append(result)
            return result
        monkeypatch.setattr(apply, "leave_known_response_ui", record_cleanup)
        result = await apply_manual(client)
        assert injected and cleanup_results == [False]
        assert result["uncertain"] is True
        repository = NativeApplyRepository(client._cookie_paths.cookies_file, "hh")
        assert repository.get("https://hh.ru/vacancy/1")["status"] == "uncertain"
        assert repository.claim("https://hh.ru/vacancy/1", "", "") is None
        stop = client._ui_guard.blocked
        with pytest.raises(HHUnexpectedUI) as raised:
            await client.get_vacancy_details("https://hh.ru/vacancy/2")
        assert raised.value is stop
    asyncio.run(manual_exit(tmp_path, monkeypatch, check))


@pytest.mark.parametrize("popup", [False, True])
@pytest.mark.parametrize("main_world_hooks", [False, True])
def test_known_picker_cleanup_receipt_has_zero_click_submit_formdata(tmp_path, monkeypatch, popup, main_world_hooks):
    async def check(client, page):
        events = []
        await page.context.expose_binding("cleanupEvent", lambda source, event: events.append(event))
        async def manual(**kwargs):
            await page.evaluate("""() => {
                for (const name of ['click','submit','formdata'])
                    window.addEventListener(name,()=>window.cleanupEvent(name),true);
            }""")
            return {"ok": False, "message": "manual review"}
        if main_world_hooks:
            ordinary_manual = manual
            async def manual(**kwargs):
                result = await ordinary_manual(**kwargs)
                await page.evaluate("""() => {
                    Document.prototype.querySelectorAll = () => {window.cleanupEvent('page_dom_hook');throw new Error('Page-owned DOM hook')};
                    Element.prototype.querySelector = () => {window.cleanupEvent('page_dom_hook');throw new Error('Page-owned DOM hook')};
                }""")
                return result
        client._try_auto_answer_questions = AsyncMock(side_effect=manual)
        cleanup_results = []
        cleanup = apply.leave_known_response_ui
        async def record_cleanup(*args):
            result = await cleanup(*args)
            cleanup_results.append(result)
            return result
        monkeypatch.setattr(apply, "leave_known_response_ui", record_cleanup)
        await apply_manual(client)
        assert cleanup_results == [True] and page.url == "about:blank"
        assert events == []
    asyncio.run(manual_exit(tmp_path, monkeypatch, check, popup=popup))


@pytest.mark.parametrize("extra", [
    '<div role=dialog><h2>Unknown wrapper</h2></div>',
    '<div role=dialog data-qa=vacancy-response-popup>Additional dialog</div>',
    '<div data-qa=modal-overlay>Additional overlay</div>',
    '<h2>Unknown consent</h2><input type=checkbox name=consent>',
    '<div data-qa=applicant-profile-onboarding-modal>Profile</div>',
])
def test_known_response_marker_does_not_authorize_extra_modal_surface(tmp_path, monkeypatch, extra):
    async def check(client, page):
        async def manual(**kwargs):
            await page.evaluate("html => document.querySelector('[data-qa=vacancy-response-popup]').insertAdjacentHTML('beforeend',html)", extra)
            return {"ok": False, "message": "manual review"}
        client._try_auto_answer_questions = AsyncMock(side_effect=manual)
        await apply_manual(client)
        assert page.url.endswith("vacancyId=1")
        assert await page.evaluate("window.submitCalls") == 0
        assert isinstance(client._ui_guard.blocked, HHUnexpectedUI)
    asyncio.run(manual_exit(tmp_path, monkeypatch, check, popup=True))



def test_ordinary_failed_vacancy_without_response_ui_keeps_existing_flow(tmp_path, monkeypatch):
    async def check(client, page):
        async def manual(**kwargs):
            await page.evaluate("""() => {
                history.replaceState(null, '', '/vacancy/1');
                document.body.innerHTML='<div data-qa="vacancy-description">Known vacancy page</div>';
            }""")
            return {"ok": False, "message": "manual review"}
        client._try_auto_answer_questions = AsyncMock(side_effect=manual)
        cleanup_results = []
        cleanup = apply.leave_known_response_ui
        async def record_cleanup(*args):
            result = await cleanup(*args)
            cleanup_results.append(result)
            return result
        monkeypatch.setattr(apply, "leave_known_response_ui", record_cleanup)
        await apply_manual(client)
        assert cleanup_results == [False]
        assert page.url == "https://hh.ru/vacancy/1"
        assert client._ui_guard.blocked is None
        assert await page.evaluate("window.submitCalls") == 0
        assert await client.get_vacancy_details("https://hh.ru/vacancy/2") == "Synthetic next vacancy"
    asyncio.run(manual_exit(tmp_path, monkeypatch, check))
