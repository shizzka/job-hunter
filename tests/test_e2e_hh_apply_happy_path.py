"""Offline Chromium gate: ordinary HH page traffic must not own an application."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import urlsplit

from playwright.async_api import Route, async_playwright

import agent
import apply_orchestrator
import config
import seen
from hh.recovery import AUTH_URL, BLOCK_WORKER_CONSTRUCTORS, monitor_page
from hh.ui import HHUIGuard
from hh_client import HHClient
from state_store.private_journal import read_json_records
from tests.test_hh_post_submit_verification import APPLIED_HTML, POPUP_URL, RESPONSE_URL, VACANCY_URL
from tests.test_hh_react_submit_boundary import COVER_LETTER, FIXTURE, TARGET_RESUME
from tests.test_observability import configure_synthetic_search

REAL_DISPATCH = apply_orchestrator.dispatch_apply
REAL_DETAILS = apply_orchestrator.fetch_vacancy_details
VENDOR_URL = "https://vendor.example.invalid/pixel.html"
TELEMETRY_URL = "https://mc.yandex.ru/watch/123456"
ANALYTICS_URL = "https://hh.ru/analytics/pageview"
NOISE_URLS = {TELEMETRY_URL, ANALYTICS_URL}
VACANCY_HTML = """<!doctype html><html><body><main>
<h1 data-qa="vacancy-title">Synthetic QA vacancy</h1>
<div data-qa="vacancy-description">Test Python services and browser workflows.</div>
<a data-qa="vacancy-response-link-top" href="/applicant/vacancy_response?vacancyId=2">Откликнуться</a>
</main></body></html>"""
NOISE_SCRIPT = """<script>
function pageTelemetry() {
  return Promise.all([
    fetch('https://mc.yandex.ru/watch/123456', {
      method:'POST', mode:'no-cors', body:'pageview=vacancy'
    }),
    fetch('/analytics/pageview', {method:'POST', body:'pageview=vacancy'})
  ]).then(() => { window.telemetryFinished=true; });
}
</script>"""
SUBMIT_SCRIPT = """<script>
document.querySelector('form[name="vacancy_response"]').addEventListener('submit', event => {
  event.preventDefault();
  const data=new FormData(event.target);
  const fields={vacancy_id:'2',resume_hash:'fixture-resume',ignore_postponed:'true',
    incomplete:'false',mark_applicant_visible_in_vacancy_country:'false',country_ids:'[]',
    letter:'Approved cover letter',lux:'true',withoutTest:'no',hhtmFromLabel:'',hhtmSourceLabel:''};
  Object.entries(fields).forEach(([name,value])=>data.append(name,value));
  fetch('/applicant/vacancy_response/popup',{method:'POST',body:data})
    .then(response=>response.json()).then(()=>location.assign('/vacancy/2'));
});
</script>"""


async def run_hh_gate(tmp_path, monkeypatch, *, continuity=False, noise=True):
    """Reuse the confirmed-success harness; replace transport and search inputs only."""
    for key, name in {
        "JOB_HUNTER_HOME": "", "SEEN_VACANCIES_FILE": "seen.json",
        "RUN_HISTORY_FILE": "runs.jsonl", "RUNTIME_STATUS_FILE": "runtime.json",
        "ANALYTICS_EVENTS_FILE": "events.jsonl", "ANALYTICS_STATE_FILE": "analytics.json",
        "HH_STATE_DIR": "hh", "HH_GUARD_STATE_FILE": "guard.json",
        "HH_COOKIES_FILE": "native-cookies.json", "HH_RESUME_PIPELINE_FILE": "pipeline.json",
    }.items():
        monkeypatch.setattr(config, key, str(tmp_path / name))
    configure_synthetic_search(monkeypatch, tmp_path)
    monkeypatch.setattr(config, "ANALYTICS_ENABLED", True)
    monkeypatch.setattr(config, "HH_BASE_URL", "https://hh.ru")
    monkeypatch.setattr(config, "HH_PRIMARY_RESUME_ID", TARGET_RESUME)
    monkeypatch.setattr(config, "HH_PRIMARY_RESUME_TITLE", "Configured resume")
    monkeypatch.setattr(agent.search_pipeline.filters, "check_vacancy", lambda vacancy: None)
    # The happy gate uses the description supplied by synthetic search, as in
    # configure_synthetic_search. The continuity gate exercises real GET handling.
    if continuity:
        monkeypatch.setattr(apply_orchestrator, "fetch_vacancy_details", REAL_DETAILS)
    monkeypatch.setattr(apply_orchestrator, "create_hh_apply_trace", lambda *args, **kwargs: None)
    dispatch_results = []

    async def observed_dispatch(*args, **kwargs):
        outcome = await REAL_DISPATCH(*args, **kwargs)
        dispatch_results.append(outcome)
        return outcome

    dispatch = AsyncMock(wraps=observed_dispatch)
    monkeypatch.setattr(apply_orchestrator, "dispatch_apply", dispatch)
    ids = ("1", "2", "3") if continuity else ("2",)
    vacancies = [{
        "id": vid, "source": "hh", "source_label": "hh.ru",
        "title": "Synthetic QA vacancy", "company": f"Synthetic employer {vid}",
        "url": f"https://hh.ru/vacancy/{vid}",
    } for vid in ids]
    monkeypatch.setattr(agent.search_pipeline, "collect_all", AsyncMock(return_value=vacancies))
    matched = []

    async def evaluate(vacancy, details):
        matched.append(vacancy["id"])
        # Continuity ends at a real matcher rejection: no second application
        # or post-submit frame lifecycle is needed to prove the run continued.
        return {"score": 10 if continuity else 90, "reason": "Synthetic QA match",
                "should_apply": not continuity, "red_flags": []}

    monkeypatch.setattr(agent, "evaluate_vacancy", evaluate)
    monkeypatch.setattr(agent, "generate_cover_letter", AsyncMock(return_value=COVER_LETTER))

    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True, args=[
            "--disable-background-networking",
            "--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost",
        ])
        try:
            context = await browser.new_context(service_workers="block")
            await context.add_init_script(script=BLOCK_WORKER_CONSTRUCTORS)
            await context.add_cookies([{
                "name": "hhtoken", "value": "synthetic-session-cookie",
                "domain": "hh.ru", "path": "/", "httpOnly": True, "secure": True,
            }])
            requests, answered, documents, fresh_gets = [], [], [], []
            noise_answered = asyncio.Event()
            context.on("request", lambda request: requests.append((request.method, request.url)))
            submitted = set()

            class PassiveResponse:
                headers = {"content-type": "text/html; charset=utf-8"}

                def __init__(self, url, body, status=200):
                    self.url, self.body, self.status = url, body, status

                async def text(self):
                    return self.body

                async def dispose(self):
                    pass

            def document(url):
                if url == AUTH_URL:
                    return PassiveResponse(url, '<a data-qa="resume-card-link-fixture" '
                                           'href="/resume/fixture-resume">Configured resume</a>')
                if url == RESPONSE_URL:
                    return PassiveResponse(url, FIXTURE + SUBMIT_SCRIPT)
                if urlsplit(url).path.startswith("/vacancy/"):
                    vid = url.rsplit("/", 1)[-1]
                    status = 403 if continuity and vid == "1" else 200
                    body = "<main>Вакансия недоступна</main>" if status == 403 else (
                        APPLIED_HTML if vid in submitted else VACANCY_HTML)
                    return PassiveResponse(url, body, status)
                raise AssertionError(f"Unexpected offline document: {url}")

            async def fetch_document(route, **kwargs):
                # Route.fetch bypasses context.route; stub this transport too.
                url = kwargs.get("url", route.request.url)
                response = document(url)
                documents.append((route.request.method, url, response.status))
                return response

            async def readonly_get(url, **kwargs):
                fresh_gets.append({"url": url, **kwargs})
                return document(url)

            monkeypatch.setattr(Route, "fetch", fetch_document)
            monkeypatch.setattr(context.request, "get", readonly_get)

            initial_url = "https://hh.ru/search/vacancy" if continuity else VACANCY_URL
            initial = "<!doctype html><main>HH search results</main>" if continuity else VACANCY_HTML
            if noise and not continuity:
                initial += (f'<iframe hidden src="{VENDOR_URL}"></iframe>'
                            + NOISE_SCRIPT + "<script>pageTelemetry();</script>")
            if noise and continuity:
                # Real beacons from the previous live document can race the
                # passive 403 shell. No monitor flags/exceptions are fabricated.
                initial += """<script>window.addEventListener('pagehide', () => {
                  navigator.sendBeacon('https://mc.yandex.ru/watch/123456','pagehide=search');
                  navigator.sendBeacon('/analytics/pageview','pagehide=search');
                });</script>"""

            initial_served = False

            async def offline(route, *args, **kwargs):
                nonlocal initial_served
                request = route.request
                if request.method == "POST" and request.url == POPUP_URL:
                    # Only reached after the real submit boundary admits the POST.
                    submitted.add("2")
                    await route.fulfill(status=200, content_type="application/json", body='{"ok":true}')
                elif request.url in NOISE_URLS:
                    await route.fulfill(status=200, content_type="text/plain", body="ok")
                elif request.url == VENDOR_URL:
                    await route.fulfill(status=200, content_type="text/html", body="<!doctype html><body>vendor pixel</body>")
                elif request.is_navigation_request():
                    if request.url == initial_url and not initial_served:
                        initial_served = True
                        response = PassiveResponse(request.url, initial)
                    else:
                        response = document(request.url)
                    await route.fulfill(status=response.status, content_type="text/html", body=response.body)
                else:
                    await route.abort()
                answered.append((request.method, request.url))
                if all(("POST", url) in answered for url in NOISE_URLS):
                    noise_answered.set()

            await context.route("**/*", offline)
            # The real native gate calls continue_ only after payload approval.
            # Keep that final hop offline, exactly as in the existing harness.
            monkeypatch.setattr(Route, "continue_", offline)
            page = await context.new_page()
            client = HHClient()
            client._page, client._context = page, context
            client._ui_home = str(tmp_path / "ui")
            client._ui_guard = HHUIGuard(tmp_path / "ui", notify=AsyncMock(return_value=False))
            client._cookie_binding = SimpleNamespace(context=context, closing=False, revoked=False)
            client._hh_blocked_worker_context = context
            context._hh_workers_owner = client
            monitor_page(client, page)
            client.start, client.stop = AsyncMock(), AsyncMock()
            client.is_logged_in = AsyncMock(return_value=True)
            client.get_negotiation_statuses = AsyncMock(return_value=[])
            client.hard_stop_browser = AsyncMock(return_value=True)
            monkeypatch.setattr(agent, "HHClient", lambda: client)
            await page.goto(initial_url)
            if noise and not continuity:
                await page.wait_for_function("window.telemetryFinished === true")
            result = await agent.do_search()
            if noise:
                # sendBeacon does not await its response. Drain both offline
                # handlers before taking evidence or closing Chromium.
                await asyncio.wait_for(noise_answered.wait(), timeout=5)
            decisions = [event for event in read_json_records(config.ANALYTICS_EVENTS_FILE)
                         if event.get("event") == "decision"]
            application_posts = [(method, url) for method, url in requests
                                 if method == "POST" and urlsplit(url).path.startswith("/applicant/vacancy_response")]
            evidence = {
                "decisions": [{key: event.get(key) for key in ("vacancy_id", "decision", "reason_code")}
                              for event in decisions],
                "stop_reason": getattr(client, "_hh_recovery_stop_reason", ""),
                "uncertain": bool(getattr(client, "_hh_recovery_uncertain", False)),
                "breaker": result["hh_recovery"], "matched": matched,
                "dispatched": [call.args[0]["id"] for call in dispatch.await_args_list],
                "apply_results": [{key: outcome.get(key) for key in ("ok", "uncertain", "reason")}
                                  for outcome in dispatch_results],
                "application_posts": application_posts, "documents": documents,
                "noise_posts": [(method, url) for method, url in requests if url in NOISE_URLS],
                "not_processed": result["funnel"]["not_processed"],
            }
            print("HH E2E evidence: " + json.dumps(evidence, ensure_ascii=False, sort_keys=True))
            return SimpleNamespace(result=result, evidence=evidence, decisions=decisions,
                                   entries=seen.all_entries(), requests=requests, answered=answered,
                                   fresh_gets=fresh_gets, application_posts=application_posts,
                                   dispatch_results=dispatch_results)
        finally:
            await browser.close()


def test_hh_apply_happy_path_through_real_page_noise(tmp_path, monkeypatch):
    gate = asyncio.run(run_hh_gate(tmp_path, monkeypatch))
    # Setup evidence must pass before the product assertions: these are actual
    # Chromium POSTs, each fulfilled locally, and the real dispatcher was entered.
    assert set(gate.evidence["noise_posts"]) == {("POST", url) for url in NOISE_URLS}
    assert all(("POST", url) in gate.answered for url in NOISE_URLS)
    assert ("GET", VENDOR_URL) in gate.answered
    assert gate.evidence["matched"] == gate.evidence["dispatched"] == ["2"]
    assert gate.entries["2"]["action"] == "applied", gate.evidence
    assert gate.result["applied"] == 1, gate.evidence
    assert len(gate.application_posts) == 1, gate.evidence
    assert gate.dispatch_results[0]["uncertain"] is False, gate.evidence
    assert gate.evidence["uncertain"] is False, gate.evidence
    assert gate.evidence["stop_reason"] == "", gate.evidence
    assert not any(event["decision"] == "guard_stop" for event in gate.decisions), gate.evidence
    assert gate.result["source_stats"]["hh"].get("guard_stop", 0) == 0, gate.evidence
    assert gate.result["hh_recovery"]["stopped"] is False, gate.evidence
    confirmation = [call for call in gate.fresh_gets if call["url"] == VACANCY_URL]
    assert len(confirmation) == 1 and confirmation[0]["max_redirects"] == 0, gate.evidence
