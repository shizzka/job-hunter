"""A verified owned application outranks its inherited frame-race hard stop."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import Route, async_playwright

import agent
import apply_orchestrator
import config
import hh.apply as hh_apply
import seen
from hh.recovery import monitor_page
from hh.ui import HHUIGuard
from state_store.native_apply import NativeApplyRepository, NativeAttempt
from state_store.private_journal import read_json_records
from hh_client import HHClient
from tests.test_hh_post_submit_verification import APPLIED_HTML, RESPONSE_URL, VACANCY_URL, POPUP_URL
from tests.test_hh_react_submit_boundary import COVER_LETTER, FIXTURE, TARGET_RESUME
from tests.test_observability import configure_synthetic_search

REAL_DISPATCH = apply_orchestrator.dispatch_apply


@pytest.mark.parametrize("variant, consumer", [
    ("valid", "result"),
    ("valid", "pipeline"),
    ("late_second_post", "result"),
    ("new_attempt_registered", "result"),
    ("post_success_unrelated_put", "result"),
    ("completion_write_error", "result"),
])
def test_owned_confirmation_overrides_only_its_inherited_stop(tmp_path, monkeypatch, variant, consumer):
    for key, name in {
        "JOB_HUNTER_HOME": "", "SEEN_VACANCIES_FILE": "seen.json",
        "RUN_HISTORY_FILE": "runs.jsonl", "RUNTIME_STATUS_FILE": "runtime.json",
        "ANALYTICS_EVENTS_FILE": "events.jsonl", "ANALYTICS_STATE_FILE": "analytics.json",
        "HH_STATE_DIR": "hh", "HH_GUARD_STATE_FILE": "guard.json",
        "HH_COOKIES_FILE": "native-cookies.json", "HH_RESUME_PIPELINE_FILE": "pipeline.json",
    }.items():
        monkeypatch.setattr(config, key, str(tmp_path / name))
    monkeypatch.setattr(config, "ANALYTICS_ENABLED", True)
    monkeypatch.setattr(config, "HH_PRIMARY_RESUME_ID", TARGET_RESUME)
    if consumer == "pipeline":
        configure_synthetic_search(monkeypatch, tmp_path)
    # The harness substitutes search inputs; application and result normalization
    # remain real. Every browser request is intercepted below.
    monkeypatch.setattr(apply_orchestrator, "dispatch_apply", REAL_DISPATCH)

    async def run():
        pw = await async_playwright().start()
        browser = await pw.chromium.launch(headless=True, args=["--disable-background-networking"])
        context = await browser.new_context(service_workers="block")
        await context.add_cookies([{
            "name": "hhtoken", "value": "synthetic-session-cookie",
            "domain": "hh.ru", "path": "/", "httpOnly": True, "secure": True,
        }])
        unrelated_mutations = []
        context.on("request", lambda request: unrelated_mutations.append(request)
                   if request.method == "PUT" else None)
        popup_posts = []
        popup_responses = []
        attached_frames = []
        fresh_get_calls = []
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
            status = 200
            url = VACANCY_URL
            headers = {"content-type": "text/html; charset=utf-8"}

            async def text(self):
                return APPLIED_HTML

            async def dispose(self):
                pass

        async def readonly_get(url, **kwargs):
            fresh_get_calls.append({"url": url, "kwargs": kwargs})
            if variant == "late_second_post":
                await page.evaluate("""() => fetch('/applicant/vacancy_response/popup', {
                  method: 'POST', body: new URLSearchParams({vacancy_id: '2'})
                }).catch(() => null)""")
            elif variant == "new_attempt_registered":
                other = NativeAttempt(repository, "https://hh.ru/vacancy/3", "fixture-new-owner", client=client)
                page._hh_action_monitor.register(other)
            return PassiveResponse()

        monkeypatch.setattr(context.request, "get", readonly_get)

        async def local_popup(route, *args, **kwargs):
            request = route.request
            if request.method == "POST" and request.url == POPUP_URL:
                popup_posts.append(request)
                await route.fulfill(status=200, content_type="application/json", body='{"ok":true}')
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

        repository = NativeApplyRepository(tmp_path / "native-cookies.json", "hh")
        if variant == "completion_write_error":
            original_transition = NativeApplyRepository.transition
            def transition(repo, url, owner, status, **kwargs):
                if status == "completed":
                    raise OSError("Synthetic terminal receipt write failure")
                return original_transition(repo, url, owner, status, **kwargs)
            monkeypatch.setattr(NativeApplyRepository, "transition", transition)
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
            session._external_attempt.uncertain = True
            session._hh_recovery_uncertain = True
            session._hh_recovery_stop_reason = "native_action_or_uncertainty"
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
                vacancy, COVER_LETTER, hh_client=client,
                preferred_resume_id=TARGET_RESUME,
                preferred_resume_title="Fixture QA resume",
            )
            attempt = next(iter(monitor.attempts.values()))
            assert len(popup_posts) == 1
            assert popup_responses and popup_responses[0].status == 200
            assert any(frame.url == "about:srcdoc" for frame in attached_frames)
            # A passive hidden frame is a warning; only the unowned POST
            # raises the independent sticky unknown-action latch.
            assert monitor.unknown is (variant == "late_second_post")
            assert context._hh_action_watch.unknown is (variant == "late_second_post")
            assert repository.get(VACANCY_URL)["status"] in {"completed", "uncertain"}
            assert await context.cookies([RESPONSE_URL]) == cookie_before
            assert len(fresh_get_calls) == 1
            assert fresh_get_calls[0]["url"] == VACANCY_URL
            assert fresh_get_calls[0]["kwargs"].get("max_redirects") == 0
            assert attempt._hh_application_posts[0]["owned"] is True
            assert attempt._hh_application_posts[0]["status"] == 200

            if variant in {"late_second_post", "new_attempt_registered", "completion_write_error"}:
                assert result.get("submission_status") != "success_confirmed"
                assert result.get("uncertain") is True
                if variant != "completion_write_error":
                    assert result.get("hh_hard_stop") is True
                assert repository.claim(VACANCY_URL, "", "") is None
                if variant == "late_second_post":
                    assert context._hh_action_watch.application_post_sequence == 2
                    assert attempt._hh_application_posts[1]["owned"] is False
                    assert repository.get(VACANCY_URL)["status"] == "uncertain"
                elif variant == "new_attempt_registered":
                    assert context._hh_action_watch.last_attempt is not attempt
                else:
                    assert repository.get(VACANCY_URL)["status"] == "uncertain"
                return

            assert result["submission_status"] == "success_confirmed"
            assert result["ok"] is True
            assert result["uncertain"] is False
            assert result["native_receipt_status"] == "completed"
            assert attempt.acting is True
            assert attempt.uncertain is False
            assert not client._hh_recovery_uncertain
            assert not client._hh_recovery_stop_reason
            assert result["cover_letter_status"] == "submitted_with_application"
            assert repository.claim(VACANCY_URL, "", "") is None
            if variant == "post_success_unrelated_put":
                assert not result.get("hh_hard_stop")
                # Independent mutation does not add an application POST, change
                # its owned receipt, or invalidate the fresh GET body itself.
                await page.evaluate("""() => fetch('/applicant/synthetic-preference', {
                  method: 'PUT', body: 'synthetic-only'
                }).catch(() => null)""")
                result = apply_orchestrator.apply_result_with_current_uncertainty(
                    vacancy, client, result, owned_attempt=attempt
                )
                assert len(unrelated_mutations) == 1
                assert monitor.unknown is True
                assert context._hh_action_watch.unknown is True
                assert context._hh_action_watch.application_post_sequence == 1
                assert len(attempt._hh_application_posts) == 1
                assert attempt._hh_application_posts[0]["owned"] is True
                assert attempt._hh_application_posts[0]["status"] == 200
                assert len(apply_core_calls) == len(popup_posts) == len(fresh_get_calls) == 1
                assert result.get("ok") is False
                assert result.get("uncertain") is True
                assert result.get("submission_status") == "uncertain"
                assert client._hh_recovery_stop_reason == "possible_external_action"
                assert repository.get(VACANCY_URL)["status"] == "uncertain"
                assert repository.claim(VACANCY_URL, "", "") is None
                return
            if consumer == "result":
                assert not result.get("hh_hard_stop"), result
                assert not result.get("hh_recovery_reason"), result
                return

            # Feed the actual typed verified result into the real agent consumer.
            # No forged success dictionary or second native application is used.
            client.start, client.stop = AsyncMock(), AsyncMock()
            client.is_logged_in = AsyncMock(return_value=True)
            client.get_negotiation_statuses = AsyncMock(return_value=[])
            monkeypatch.setattr(agent, "HHClient", lambda: client)
            monkeypatch.setattr(agent.search_pipeline, "collect_all", AsyncMock(return_value=[vacancy]))
            monkeypatch.setattr(agent.search_pipeline.filters, "check_vacancy", lambda vacancy: None)
            monkeypatch.setattr(agent, "evaluate_vacancy", AsyncMock(return_value={
                "score": 90, "reason": "Synthetic QA match", "should_apply": True, "red_flags": [],
            }))
            dispatch = AsyncMock(return_value=result)
            monkeypatch.setattr(apply_orchestrator, "dispatch_apply", dispatch)
            search_result = await agent.do_search()
            dispatch.assert_awaited_once()
            assert search_result["applied"] == 1, search_result
            assert search_result["source_stats"]["hh"]["applied"] == 1
            assert search_result["source_stats"]["hh"].get("guard_stop", 0) == 0
            assert search_result["reason_breakdown"].get("apply_uncertain", 0) == 0
            assert seen.all_entries()["2"]["action"] == "applied"
            decisions = [event["decision"] for event in read_json_records(config.ANALYTICS_EVENTS_FILE)
                         if event.get("event") == "decision"]
            assert "applied_auto" in decisions
            assert not {"guard_stop", "apply_uncertain"}.intersection(decisions)
            assert len(apply_core_calls) == len(popup_posts) == len(fresh_get_calls) == 1
            assert repository.claim(VACANCY_URL, "", "") is None
        finally:
            await browser.close()
            await pw.stop()

    asyncio.run(run())
