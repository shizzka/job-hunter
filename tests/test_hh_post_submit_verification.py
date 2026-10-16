"""A clean HH POST plus persisted applied GET can verify the result after a frame race."""
import asyncio
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import Route, async_playwright

import apply_orchestrator
import config
import hh.apply as hh_apply
from hh.recovery import monitor_page
from hh.ui import HHUIGuard
from hh_client import HHClient
from state_store.native_apply import NativeApplyRepository
from tests.test_hh_react_submit_boundary import COVER_LETTER, FIXTURE, TARGET_RESUME


RESPONSE_URL = "https://hh.ru/applicant/vacancy_response?vacancyId=2"
VACANCY_URL = "https://hh.ru/vacancy/2"
POPUP_URL = "https://hh.ru/applicant/vacancy_response/popup"
APPLIED_HTML = """<!doctype html><html><body>
<main><h1 data-qa="vacancy-title">Synthetic QA vacancy</h1>
<button data-qa="vacancy-response-link-top-again">Отклик другим резюме</button>
<div data-qa="vacancy-response-success-standard-notification">Резюме доставлено</div></main>
<iframe hidden srcdoc="&lt;!doctype html&gt;&lt;body&gt;inert fixture&lt;/body&gt;"></iframe>
</body></html>"""


@pytest.mark.parametrize("variant, expected_success", [
    ("valid", True),
    ("mixed_unsent_cta", False),
    ("foreign_vacancy", False),
    ("redirect", False),
    ("dispose_error", False),
    ("cookie_changed_before_get", False),
    ("cookie_changed_during_get", False),
    ("late_second_post", False),
    ("late_second_get", False),
    ("new_attempt_registered", False),
    ("context_swapped", False),
    ("inert_markers", False),
    ("post_success_late_post", False),
    ("post_success_new_attempt", False),
    ("post_success_context_swap", False),
    ("post_success_dispatch_replay", False),
    ("post_success_claim_denied", False),
    ("post_success_generic_replay", False),
    ("popup_http_error", False),
])
def test_owned_popup_frame_race_requires_unambiguous_fresh_applied_get(
    tmp_path, monkeypatch, variant, expected_success
):
    async def run():
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True, args=["--disable-background-networking"])
        context = await browser.new_context(service_workers="block")
        await context.add_cookies([{
            "name": "hhtoken", "value": "synthetic-session-cookie",
            "domain": "hh.ru", "path": "/", "httpOnly": True, "secure": True,
        }])
        browser_requests = []
        popup_posts = []
        popup_responses = []
        attached_frames = []
        fresh_get_calls = []
        context.on("request", lambda request: browser_requests.append(request))
        context.on("response", lambda response: popup_responses.append(response)
                   if response.url == POPUP_URL else None)

        async def offline(route):
            request = route.request
            if request.is_navigation_request() and request.url == RESPONSE_URL:
                await route.fulfill(status=200, content_type="text/html", body=FIXTURE)
            elif request.is_navigation_request() and request.url == VACANCY_URL:
                await route.fulfill(status=200, content_type="text/html", body=APPLIED_HTML)
            else:
                await route.abort()

        await context.route("**/*", offline)
        page = await context.new_page()
        await page.goto(RESPONSE_URL)

        client = HHClient()
        client._page, client._context = page, context
        client._ui_home = str(tmp_path / "ui")
        client._ui_guard = HHUIGuard(tmp_path / "ui", notify=AsyncMock(return_value=False))
        client.hard_stop_browser = AsyncMock(return_value=True)
        # The fixture models the real session-owned binding. No cookie writes
        # are performed by this test.
        client._cookie_binding = SimpleNamespace(context=context, closing=False, revoked=False)
        monitor = monitor_page(client, page)
        page.on("frameattached", lambda frame: attached_frames.append(frame))

        class PassiveResponse:
            status = 302 if variant == "redirect" else 200
            url = "https://hh.ru/vacancy/3" if variant == "foreign_vacancy" else VACANCY_URL
            headers = {"content-type": "text/html; charset=utf-8"}

            async def text(self):
                if variant == "mixed_unsent_cta":
                    return APPLIED_HTML.replace(
                        "<button data-qa=\"vacancy-response-link-top-again\">",
                        '<a data-qa="vacancy-response-link-top">Откликнуться</a>'
                        '<button data-qa="vacancy-response-link-top-again">',
                    )
                if variant == "inert_markers":
                    return '<script>' + APPLIED_HTML + '</script>'
                return APPLIED_HTML

            async def dispose(self):
                if variant == "dispose_error":
                    raise RuntimeError("synthetic response disposal failure")

        async def readonly_get(url, **kwargs):
            fresh_get_calls.append({"url": url, "kwargs": kwargs})
            if variant == "cookie_changed_during_get":
                await context.add_cookies([{
                    "name": "hhtoken", "value": "changed-session-cookie",
                    "domain": "hh.ru", "path": "/", "httpOnly": True, "secure": True,
                }])
            elif variant == "late_second_post":
                # A real second same-origin POST reaches the installed monitor
                # while the verifier is awaiting the fresh response.
                await page.evaluate("""() => fetch('/applicant/vacancy_response/popup', {
                  method: 'POST', body: new URLSearchParams({vacancy_id: '2'})
                }).catch(() => null)""")
            elif variant == "late_second_get":
                await page.evaluate("""() => fetch('/applicant/vacancy_response?resume_id=other')
                  .catch(() => null)""")
            elif variant == "new_attempt_registered":
                from state_store.native_apply import NativeAttempt
                other = NativeAttempt(repository, "https://hh.ru/vacancy/3", "fixture-new-owner", client=client)
                page._hh_action_monitor.register(other)
            elif variant == "context_swapped":
                client._context = SimpleNamespace()
            return PassiveResponse()

        monkeypatch.setattr(context.request, "get", readonly_get)

        async def local_popup(route, *args, **kwargs):
            request = route.request
            if request.method == "POST" and request.url == POPUP_URL:
                popup_posts.append(request)
                if variant == "cookie_changed_before_get":
                    await context.add_cookies([{
                        "name": "hhtoken", "value": "changed-session-cookie",
                        "domain": "hh.ru", "path": "/", "httpOnly": True, "secure": True,
                    }])
                status = 500 if variant == "popup_http_error" else 200
                await route.fulfill(status=status, content_type="application/json", body='{"ok":true}')
                return
            await route.abort()

        # The real boundary gate invokes Route.continue_ only after native
        # proof. Stub that terminal transport locally, after the gate.
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
              .then(()=>setTimeout(()=>location.assign('/vacancy/2'),100));
          });
        }""")

        # Inspect the same isolated receipt file captured by HHClient.
        repository = NativeApplyRepository(config.HH_COOKIES_FILE, "hh")
        apply_core_calls = []
        cookie_before = await context.cookies([RESPONSE_URL])
        vacancy = {
            "id": "2", "source": "hh", "title": "Synthetic QA vacancy",
            "company": "Synthetic employer", "url": VACANCY_URL,
            "_analytics_apply_mode": "auto",
        }

        async def apply_core(session, vacancy_url, cover_letter, *_args, **_kwargs):
            apply_core_calls.append(vacancy_url)
            control = await session._page.query_selector('[data-qa="vacancy-response-submit-popup"]')
            assert control is not None

            async def fresh_identity_and_letter():
                return await session._page.evaluate("""() =>
                  document.querySelector('[role="listbox"] input[type="radio"]:checked')?.value==='fixture-resume' &&
                  document.querySelector('[data-qa="vacancy-response-popup-form-letter-input"]')?.value==='Approved cover letter'
                """)

            clicked = await session._click_with_fallbacks(
                control, "submit_button", before_click=fresh_identity_and_letter
            )
            assert clicked is True
            # The browser-side receipt cannot classify the terminal result yet.
            # Only the independent fresh GET may close this owned POST.
            return {"ok": False, "hh_hard_stop": True,
                    "hh_recovery_reason": "native_action_or_uncertainty",
                    "cover_letter_status": "unconfirmed"}

        monkeypatch.setattr(hh_apply, "_apply_to_vacancy", apply_core)
        monkeypatch.setattr(apply_orchestrator, "create_hh_apply_trace", lambda *args, **kwargs: None)
        monkeypatch.setattr(apply_orchestrator.company_blacklist, "is_blocked", lambda company: False)
        try:
            result = await apply_orchestrator.dispatch_apply(
                vacancy,
                COVER_LETTER,
                hh_client=client,
                preferred_resume_id=TARGET_RESUME,
                preferred_resume_title="Fixture QA resume",
            )
            if variant == "post_success_late_post":
                await page.evaluate("""() => fetch('/applicant/vacancy_response/popup', {
                  method: 'POST', body: new URLSearchParams({vacancy_id: '2'})
                }).catch(() => null)""")
                result = apply_orchestrator.apply_result_with_current_uncertainty(
                    vacancy, client, result, owned_attempt=next(iter(monitor.attempts.values()))
                )
            elif variant == "post_success_new_attempt":
                from state_store.native_apply import NativeAttempt
                newer = NativeAttempt(repository, "https://hh.ru/vacancy/3", "fixture-later-owner", client=client)
                monitor.register(newer)
                result = apply_orchestrator.apply_result_with_current_uncertainty(
                    vacancy, client, result, owned_attempt=next(iter(monitor.attempts.values()))
                )
            elif variant == "post_success_context_swap":
                client._context = SimpleNamespace()
                result = apply_orchestrator.apply_result_with_current_uncertainty(
                    vacancy, client, result, owned_attempt=next(iter(monitor.attempts.values()))
                )
                client._context = context
            elif variant == "post_success_dispatch_replay":
                result = await apply_orchestrator.dispatch_apply(
                    vacancy,
                    COVER_LETTER,
                    hh_client=client,
                    preferred_resume_id=TARGET_RESUME,
                    preferred_resume_title="Fixture QA resume",
                )
            elif variant == "post_success_claim_denied":
                # Clear only this test's sticky UI flags to reach the real
                # repository claim path; the terminal uncertain receipt stays.
                monitor.unknown = False
                context._hh_action_watch.unknown = False
                client._hh_recovery_stop_reason = ""
                client._hh_recovery_uncertain = False
                result = await client.apply_to_vacancy(
                    VACANCY_URL, COVER_LETTER, response_url=RESPONSE_URL,
                    preferred_resume_title="Fixture QA resume", preferred_resume_id=TARGET_RESUME,
                )
                assert len(apply_core_calls) == 1
                assert result.get("reason") == "native_attempt_not_retryable"
            elif variant == "post_success_generic_replay":
                result = apply_orchestrator.apply_result_with_current_uncertainty(
                    vacancy, client, {"ok": True}, owned_attempt=next(iter(monitor.attempts.values()))
                )
            if variant == "context_swapped":
                client._context = context

            # Prove the regression setup reached the real event/receipt race,
            # rather than passing through a clean monitor or a fabricated result.
            srcdoc_frames = [frame for frame in attached_frames if frame.url == "about:srcdoc"]
            assert len(popup_posts) == 1
            expected_popup_status = 500 if variant == "popup_http_error" else 200
            assert popup_responses and popup_responses[0].status == expected_popup_status
            assert srcdoc_frames, [frame.url for frame in attached_frames]
            if variant == "post_success_claim_denied":
                assert monitor.unknown is False
                assert context._hh_action_watch.unknown is False
            else:
                mutated = variant in {"late_second_post", "late_second_get", "post_success_late_post"}
                assert monitor.unknown is mutated
                assert context._hh_action_watch.unknown is mutated
            assert repository.get(VACANCY_URL)["status"] in {"completed", "uncertain"}
            if variant in {"cookie_changed_before_get", "cookie_changed_during_get"}:
                assert await context.cookies([RESPONSE_URL]) != cookie_before
            else:
                assert await context.cookies([RESPONSE_URL]) == cookie_before

            if variant in {"popup_http_error", "cookie_changed_before_get"}:
                assert fresh_get_calls == []
            else:
                # Only an unconfirmed HTML receipt gets a second vacancy GET;
                # a separate auth GET cannot serve as delivery confirmation.
                vacancy_gets = [call for call in fresh_get_calls if call['url'] == VACANCY_URL]
                assert len(vacancy_gets) == (2 if variant in {'mixed_unsent_cta', 'inert_markers'} else 1)
                assert fresh_get_calls[0]["url"] == VACANCY_URL
                assert fresh_get_calls[0]["kwargs"].get("max_redirects") == 0
            if variant == "late_second_post":
                assert context._hh_action_watch.application_post_sequence == 2
                registered_attempt = next(iter(monitor.attempts.values()))
                assert len(registered_attempt._hh_application_posts) == 2
                assert registered_attempt._hh_application_posts[0]["status"] == 200
                assert registered_attempt._hh_application_posts[1]["owned"] is False
            if variant == "late_second_get":
                assert context._hh_action_watch.application_post_sequence == 2
                registered_attempt = next(iter(monitor.attempts.values()))
                assert len(registered_attempt._hh_application_posts) == 1
                assert registered_attempt._hh_reconciliation_diagnostic['step'] == 'post_sequence_changed'
            if variant == "new_attempt_registered":
                assert context._hh_action_watch.last_attempt is not next(iter(monitor.attempts.values()))
            if variant == "post_success_late_post":
                assert context._hh_action_watch.application_post_sequence == 2
                assert len(next(iter(monitor.attempts.values()))._hh_application_posts) == 2
                assert len(popup_posts) == 1
            if variant in {"post_success_dispatch_replay", "post_success_claim_denied",
                           "post_success_generic_replay"}:
                assert len(popup_posts) == 1
                assert len(fresh_get_calls) == 1
            assert (result.get("submission_status") == "success_confirmed") is expected_success
            if expected_success:
                assert result.get("ok") is True
                assert result.get("uncertain") is not True
                assert result.get("native_receipt_status") == "completed"
                assert not result.get("hh_hard_stop")
                assert result.get("cover_letter_status") == "submitted_with_application"
                assert len(popup_posts) == 1
                assert repository.claim(VACANCY_URL, "", "") is None
            else:
                assert result.get("uncertain") is True
                assert repository.get(VACANCY_URL)["status"] in {"completed", "uncertain"}
                assert repository.claim(VACANCY_URL, "", "") is None
        finally:
            await browser.close()
            await pw.stop()

    asyncio.run(run())


@pytest.mark.parametrize("client", [None, SimpleNamespace()])
@pytest.mark.parametrize("ok", [True, False])
def test_forged_success_marker_without_typed_verification_is_uncertain(client, ok):
    result = apply_orchestrator.apply_result_with_current_uncertainty(
        {"id": "2", "source": "hh", "url": VACANCY_URL},
        client,
        {"ok": ok, "submission_status": "success_confirmed"},
    )

    assert result["ok"] is False
    assert result["uncertain"] is True
    assert result["submission_status"] == "uncertain"
