"""Native submit admission for the observed React response-form structure."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from playwright.async_api import async_playwright
from playwright.async_api import Route

from browser_action_boundary import dispatch_approved
from hh.recovery import monitor_page
from hh.submit_boundary import arm_submit_boundary, bind_submit_control
from state_store.native_apply import NativeApplyRepository, run_native_attempt


FIXTURE = (Path(__file__).parent / "fixtures" / "hh_react_response_form.html").read_text()
TARGET_RESUME = "fixture-resume"
COVER_LETTER = "Approved cover letter"
URL = "https://hh.ru/applicant/vacancy_response?vacancyId=2"
POPUP_URL = "https://hh.ru/applicant/vacancy_response/popup"


async def _open_fixture(fixture=FIXTURE):
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True, args=["--disable-background-networking"])
    context = await browser.new_context(service_workers="block")
    async def offline(route):
        if route.request.is_navigation_request() and route.request.url == URL:
            await route.fulfill(status=200, content_type="text/html", body=fixture)
        else:
            await route.abort()
    await context.route("**/*", offline)
    page = await context.new_page()
    await page.goto(URL)
    session = SimpleNamespace(
        _page=page,
        _approved_hh_payload={"resume_id": TARGET_RESUME, "cover_letter": COVER_LETTER, "answers": []},
        _external_attempt=None,
        _submit_boundary_id=None,
    )
    return pw, browser, page, session


def test_observed_react_portal_resume_form_arms_and_binds_without_dispatch():
    async def run():
        pw, browser, page, session = await _open_fixture()
        try:
            # The exact resume is selected in the portal, outside the form;
            # only _xsrf is a successful named control and the letter textarea
            # is intentionally unnamed, matching the observed React DOM.
            assert await page.evaluate("""() => {
              const form=document.querySelector('form[name="vacancy_response"]');
              return form && JSON.stringify([...form.elements].map(e=>e.name).filter(Boolean))==='[\"_xsrf\"]' &&
                document.querySelector('[role="listbox"] input[type="radio"]:checked')?.value==='fixture-resume' &&
                !document.querySelector('[data-qa="vacancy-response-popup-form-letter-input"]').name;
            }""") is True

            armed = await arm_submit_boundary(session)
            diagnostic = await page.evaluate("""() => {
              const form=document.querySelector('form[name="vacancy_response"]');
              const entries=window.__jhActionBoundary.formData(form);
              const ids=[...entries.entries()].filter(([name])=>['resume_id','resumeId','resumeHash','resume'].includes(name));
              const radio=document.querySelector('[role="listbox"] input[type="radio"]:checked');
              const letter=document.querySelector('[data-qa="vacancy-response-popup-form-letter-input"]');
              return {successful_names:[...entries.keys()],successful_resume_count:ids.length,
                checked_portal_radio:!!radio,portal_radio_value_matches_target:radio?.value==='fixture-resume',
                letter_control_present:!!letter,letter_value_matches_approval:letter?.value==='Approved cover letter'};
            }""")
            assert armed is True, f"exact observed React form rejected: {diagnostic}"
            control = await page.query_selector('[data-qa="vacancy-response-submit-popup"]')
            assert control is not None
            assert await bind_submit_control(session, control) is True
            assert await page.evaluate("""() => window.__jhActionBoundary &&
                document.querySelector('form[name="vacancy_response"]') &&
                document.querySelector('[role="listbox"] input[type="radio"]:checked')?.value==='fixture-resume'""") is True
        finally:
            await browser.close()
            await pw.stop()
    asyncio.run(run())


@pytest.mark.parametrize("variant", ["wrong_resume", "wrong_letter", "extra_resume_field", "extra_field", "profile", "captcha"])
def test_observed_react_form_rejects_mismatched_or_unknown_state(variant):
    async def run():
        pw, browser, page, session = await _open_fixture()
        try:
            if variant == "wrong_resume":
                await page.evaluate("""() => {
                  const radio=document.querySelector('[role="listbox"] input[type="radio"]');
                  radio.value='other-resume';
                  radio.closest('[data-magritte-select-option]').setAttribute('data-magritte-select-option','other-resume');
                }""")
            elif variant == "wrong_letter":
                await page.locator('[data-qa="vacancy-response-popup-form-letter-input"]').fill("Different letter")
            elif variant == "extra_resume_field":
                await page.evaluate("""() => {
                  const input=document.createElement('input');input.type='hidden';input.name='resume_id';input.value='other-resume';
                  document.querySelector('form[name="vacancy_response"]').append(input);
                }""")
            elif variant == "extra_field":
                await page.evaluate("""() => {
                  const input=document.createElement('input');input.type='hidden';input.name='unexpected_field';input.value='fixture';
                  document.querySelector('form[name="vacancy_response"]').append(input);
                }""")
            elif variant == "profile":
                await page.evaluate("""() => document.body.insertAdjacentHTML('beforeend',
                  '<div role="dialog" data-qa="applicant-profile-completion-modal"><h2>Complete profile</h2><input></div>')""")
            elif variant == "captcha":
                await page.evaluate("""() => document.body.insertAdjacentHTML('beforeend',
                  '<div data-qa="captcha">Verification</div>')""")
            assert await arm_submit_boundary(session) is False
        finally:
            await browser.close()
            await pw.stop()
    asyncio.run(run())


@pytest.mark.parametrize("foreign", [False, True])
def test_react_boundary_rejects_stale_form_or_foreign_submit_control(foreign):
    async def run():
        pw, browser, page, session = await _open_fixture()
        try:
            assert await arm_submit_boundary(session) is True
            if foreign:
                await page.evaluate("""() => document.body.insertAdjacentHTML('beforeend',
                  '<button id="foreign-submit" type="submit">Other</button>')""")
                control = await page.query_selector('#foreign-submit')
            else:
                await page.evaluate("""() => {
                  const form=document.querySelector('form[name="vacancy_response"]');
                  form.replaceWith(form.cloneNode(true));
                }""")
                control = await page.query_selector('[data-qa="vacancy-response-submit-popup"]')
            assert control is not None
            assert await bind_submit_control(session, control) is False
        finally:
            await browser.close()
            await pw.stop()
    asyncio.run(run())


@pytest.mark.parametrize("mutation", ["valid", "wrong_wire", "late_ui", "second_post"])
@pytest.mark.parametrize("dispatch_mode", ["click", "submit"])
def test_owned_native_attempt_gates_synthetic_popup_before_local_forward(tmp_path, monkeypatch, mutation, dispatch_mode):
    """Exercise real boundary/attempt/monitor ordering without external requests."""
    async def run():
        pw, browser, page, session = await _open_fixture()
        local_forwarded = []
        async def fulfill_popup(route, *args, **kwargs):
            if route.request.url == POPUP_URL:
                local_forwarded.append(route.request)
                await route.fulfill(status=200, content_type="application/json", body='{"ok":true}')
                return
            await route.abort()

        monkeypatch.setattr(Route, "continue_", fulfill_popup)
        try:
            # Keep production NativeAttempt, monitor, submit boundary, and
            # dispatch path. Only the transport after the exact gate is local.
            from hh_client import HHClient
            client = HHClient()
            client._page, client._context = page, page.context
            client._approved_hh_payload = {
                "resume_id": TARGET_RESUME,
                "cover_letter": COVER_LETTER,
                "answers": [],
            }
            monitor_page(client, page)
            repository = NativeApplyRepository(tmp_path / "synthetic-cookies.json", "hh")

            await page.evaluate("""(variant) => {
              const form=document.querySelector('form[name="vacancy_response"]');
              form.addEventListener('submit', event => {
                event.preventDefault();
                if (variant === 'late_ui') document.body.insertAdjacentHTML('beforeend', '<div role="dialog"><h2>Unknown UI</h2></div>');
                const data=new FormData(form);
                const fields={vacancy_id:'2',resume_hash:variant === 'wrong_wire' ? 'other-resume' : 'fixture-resume',
                  ignore_postponed:'true',incomplete:'false',mark_applicant_visible_in_vacancy_country:'false',
                  country_ids:'[]',letter:'Approved cover letter',lux:'true',withoutTest:'no',
                  hhtmFromLabel:'',hhtmSourceLabel:''};
                Object.entries(fields).forEach(([name,value])=>data.append(name,value));
                const send=()=>fetch('/applicant/vacancy_response/popup',{method:'POST',body:data});
                window.popupResponses=[send()];
                if (variant === 'second_post') window.popupResponses[0].then(() => window.popupResponses.push(send()));
              });
            }""", mutation)

            async def operation():
                attempt = client._external_attempt
                attempt.begin()
                assert await arm_submit_boundary(client) is True
                control = await page.query_selector('[data-qa="vacancy-response-submit-popup"]')
                assert control is not None
                assert await bind_submit_control(client, control) is True
                assert await dispatch_approved(page, control, client._submit_boundary_id, mode=dispatch_mode) is True
                await page.wait_for_function("() => window.popupResponses && window.popupResponses.length > 0")
                if mutation == "second_post":
                    await page.wait_for_function("() => window.popupResponses.length === 2")
                await page.evaluate("() => Promise.allSettled(window.popupResponses)")
                await page.wait_for_timeout(20)
                return {"ok": True}

            result = await run_native_attempt(client, repository, "https://hh.ru/vacancy/2", operation)
            accepted = len(local_forwarded)
            watch = page.context._hh_action_watch
            monitor = page._hh_action_monitor
            assert accepted == (1 if mutation in {"valid", "second_post"} else 0)
            if mutation == "valid":
                assert result["ok"] is True
                assert monitor.unknown is False and watch.unknown is False
                assert repository.get("https://hh.ru/vacancy/2")["status"] == "completed"
                assert len(local_forwarded) == 1
            else:
                assert monitor.unknown is True or watch.unknown is True
                assert repository.get("https://hh.ru/vacancy/2")["status"] == "uncertain"
                if mutation == "second_post":
                    assert len(local_forwarded) == 1  # the second request was denied
        finally:
            await browser.close()
            await pw.stop()
    asyncio.run(run())


def test_owned_popup_success_redirect_does_not_race_post_click_ui_scan(tmp_path, monkeypatch):
    """A local HH-like 200 schedules navigation across the post-click scan."""
    async def run():
        from unittest.mock import AsyncMock

        from hh.recovery import monitor_page
        from hh.ui import HHUIGuard
        from hh_client import HHClient

        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True, args=["--disable-background-networking"])
        context = await browser.new_context(service_workers="block")
        post_requests = []
        nav_started = asyncio.Event()

        async def offline(route):
            request = route.request
            if request.is_navigation_request() and request.url == URL:
                await route.fulfill(status=200, content_type="text/html", body=FIXTURE)
            elif request.is_navigation_request() and request.url == "https://hh.ru/vacancy/2":
                nav_started.set()
                await route.fulfill(status=200, content_type="text/html",
                                    body="<!doctype html><main data-qa='vacancy-description'>Applied</main>")
            else:
                await route.abort()

        await context.route("**/*", offline)
        page = await context.new_page()
        await page.goto(URL)
        client = HHClient()
        client._page, client._context = page, context
        client._ui_home = str(tmp_path)
        client._ui_guard = HHUIGuard(tmp_path, notify=AsyncMock(return_value=False))
        client._approved_hh_payload = {
            "resume_id": TARGET_RESUME, "cover_letter": COVER_LETTER, "answers": []
        }
        monitor_page(client, page)

        original_evaluate = page.evaluate
        scan_errors = []

        async def observe_post_action_inspection(expression, *args):
            if (isinstance(expression, str) and "codex:hh-ui-inspect" in expression
                    and post_requests):
                # Preserve the actual INSPECT_SCRIPT; delay its renderer result
                # so the scheduled native location change can commit in flight.
                slow = expression.replace(
                    "() => { const inspect = (", "async () => { const inspect = (", 1
                ).replace(
                    "return inspect().map(item => item.descriptor);",
                    "await new Promise(resolve => setTimeout(resolve, 200)); "
                    "return inspect().map(item => item.descriptor);",
                    1,
                )
                try:
                    return await original_evaluate(slow, *args)
                except Exception as exc:
                    scan_errors.append({"type": type(exc).__name__, "message": str(exc)[:300]})
                    raise
            return await original_evaluate(expression, *args)

        page.evaluate = observe_post_action_inspection

        async def local_popup(route, *args, **kwargs):
            if route.request.method == "POST" and route.request.url == POPUP_URL:
                post_requests.append(route.request)
                await route.fulfill(status=200, content_type="application/json", body='{"ok":true}')
            else:
                await route.abort()

        monkeypatch.setattr(Route, "continue_", local_popup)
        await page.evaluate("""() => {
          const form=document.querySelector('form[name="vacancy_response"]');
          form.addEventListener('submit', event => {
            event.preventDefault();
            const data=new FormData(form);
            const fields={vacancy_id:'2',resume_hash:'fixture-resume',ignore_postponed:'true',
              incomplete:'false',mark_applicant_visible_in_vacancy_country:'false',country_ids:'[]',
              letter:'Approved cover letter',lux:'true',withoutTest:'no',hhtmFromLabel:'',hhtmSourceLabel:''};
            Object.entries(fields).forEach(([name,value])=>data.append(name,value));
            fetch('/applicant/vacancy_response/popup',{method:'POST',body:data})
              .then(response=>response.json())
              .then(()=>setTimeout(()=>location.assign('/vacancy/2'),1100));
          });
        }""")

        repository = NativeApplyRepository(tmp_path / "receipt.json", "hh")

        async def operation():
            control = await page.query_selector('[data-qa="vacancy-response-submit-popup"]')
            assert control is not None

            async def unchanged_approval():
                return await page.evaluate("""() =>
                  document.querySelector('[role="listbox"] input[type="radio"]:checked')?.value==='fixture-resume' &&
                  document.querySelector('[data-qa="vacancy-response-popup-form-letter-input"]')?.value==='Approved cover letter'
                """)

            clicked = await client._click_with_fallbacks(
                control, "submit_button", before_click=unchanged_approval
            )
            return {"ok": clicked}

        try:
            result = await run_native_attempt(client, repository, "https://hh.ru/vacancy/2", operation)
            assert len(post_requests) == 1
            assert nav_started.is_set() and page.url == "https://hh.ru/vacancy/2"
            assert scan_errors == [], f"post-click UI scan crossed document navigation: {scan_errors}"
            assert result["ok"] is True and not result.get("uncertain")
            assert client._ui_guard.blocked is None
            assert repository.get("https://hh.ru/vacancy/2")["status"] == "completed"
        finally:
            await browser.close()
            await pw.stop()

    asyncio.run(run())
