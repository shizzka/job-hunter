"""Pure HH React-state and popup multipart helpers against observed structure."""
import asyncio
from types import SimpleNamespace

import pytest
from playwright.async_api import async_playwright

from hh.submit_boundary import REACT_RESPONSE_STATE_SCRIPT, popup_request_matches
from tests.test_hh_react_submit_boundary import FIXTURE, URL


async def _offline_page():
    pw = await async_playwright().start()
    browser = await pw.chromium.launch(headless=True, args=["--disable-background-networking"])
    context = await browser.new_context(service_workers="block")

    async def route_handler(route):
        if route.request.is_navigation_request() and route.request.url == URL:
            await route.fulfill(status=200, content_type="text/html", body=FIXTURE)
        else:
            await route.abort()

    await context.route("**/*", route_handler)
    page = await context.new_page()
    await page.goto(URL)
    return pw, browser, page


def test_react_state_helper_reads_exact_configured_resume_and_current_root():
    async def run():
        pw, browser, page = await _offline_page()
        try:
            proof = await page.evaluate(REACT_RESPONSE_STATE_SCRIPT)
            assert proof == {
                "hash": "fixture-resume",
                "incomplete": False,
                "letter": "Approved cover letter",
                "visibility": False,
                "countries": [],
                "vacancy": "2",
            }
        finally:
            await browser.close()
            await pw.stop()

    asyncio.run(run())


@pytest.mark.parametrize(
    "mutation",
    [
        "window.__fixtureReduxState.applicantVacancyResponseStatuses['2'].resumes['fixture-resume'].isIncomplete='false';",
        "window.__fixtureReduxState.countriesProfileVisibilityAgreement.confirmed='true';",
        "window.__fixtureReduxState.countriesProfileVisibilityAgreement.confirmedCountryIds='1';",
        "window.__fixtureReduxState.router={location:{query:{vacancyId:'2',unexpected:'x'}}};",
        "document.querySelector('form[name=vacancy_response]').__reactFiber$fixture.return.memoizedProps.vacancySource='vacancy';",
    ],
)
def test_react_state_helper_refuses_unverified_or_mismatched_metadata(mutation):
    async def run():
        pw, browser, page = await _offline_page()
        try:
            await page.evaluate(mutation)
            assert await page.evaluate(REACT_RESPONSE_STATE_SCRIPT) is None
        finally:
            await browser.close()
            await pw.stop()

    asyncio.run(run())


def test_react_state_helper_preserves_true_dynamic_flags_without_acceptance():
    async def run():
        pw, browser, page = await _offline_page()
        try:
            await page.evaluate("""() => {
              window.__fixtureReduxState.applicantVacancyResponseStatuses['2'].resumes['fixture-resume'].isIncomplete=true;
              const a=window.__fixtureReduxState.countriesProfileVisibilityAgreement;
              a.confirmed=true; a.confirmedCountryIds=[643];
            }""")
            proof = await page.evaluate(REACT_RESPONSE_STATE_SCRIPT)
            assert proof == {
                "hash": "fixture-resume",
                "incomplete": True,
                "letter": "Approved cover letter",
                "visibility": True,
                "countries": [643],
                "vacancy": "2",
            }
            assert "approved" not in proof
        finally:
            await browser.close()
            await pw.stop()

    asyncio.run(run())


def test_react_state_helper_rejects_form_fiber_absent_from_current_root():
    async def run():
        pw, browser, page = await _offline_page()
        try:
            await page.evaluate("""() => {
              const form=document.querySelector('form[name="vacancy_response"]');
              let host=form[Object.keys(form).find(k=>k.startsWith('__reactFiber$'))];
              while(host.return) host=host.return;
              const fiberRoot=host.stateNode;
              fiberRoot.current={stateNode:fiberRoot,return:null,child:null,sibling:null,memoizedProps:{}};
            }""")
            assert await page.evaluate(REACT_RESPONSE_STATE_SCRIPT) is None
        finally:
            await browser.close()
            await pw.stop()

    asyncio.run(run())


def test_react_state_helper_uses_current_root_over_stale_dom_attached_fiber():
    async def run():
        pw, browser, page = await _offline_page()
        try:
            await page.evaluate("""() => {
              const form=document.querySelector('form[name="vacancy_response"]');
              let oldHost=form[Object.keys(form).find(k=>k.startsWith('__reactFiber$'))];
              while(oldHost.return) oldHost=oldHost.return;
              const fiberRoot=oldHost.stateNode;
              const state=window.__fixtureReduxState;
              const resume=state.applicantVacancyResponseStatuses['2'].resumes['fixture-resume'];
              const hostForm={stateNode:form,return:null,child:null,sibling:null,memoizedProps:{}};
              const type=function CurrentResponseForm(){};
              type.displayName='components/VacancyResponseForm/index/VacancyResponseForm';
              const component={stateNode:null,type,return:null,child:hostForm,sibling:null,
                memoizedProps:{vacancyId:'2',vacancySource:undefined,selectedResume:resume,letterValue:'Approved cover letter'}};
              const store={getState:()=>state};
              const provider={stateNode:null,type:{displayName:'Provider'},return:null,child:component,sibling:null,
                memoizedProps:{value:{store}}};
              const current={stateNode:fiberRoot,type:null,return:null,child:provider,sibling:null,memoizedProps:{}};
              const currentHost={stateNode:fiberRoot,type:null,return:null,child:null,sibling:null,memoizedProps:{}};
              current.return=currentHost;provider.return=current;component.return=provider;hostForm.return=component;
              fiberRoot.current=current;
              let oldComponent=form[Object.keys(form).find(k=>k.startsWith('__reactFiber$'))].return;
              while(oldComponent && oldComponent.type?.displayName !== 'components/VacancyResponseForm/index/VacancyResponseForm') oldComponent=oldComponent.return;
              oldComponent.memoizedProps.selectedResume={hash:'stale-wrong-resume',isIncomplete:false};
            }""")
            assert await page.evaluate(REACT_RESPONSE_STATE_SCRIPT) == {
                "hash": "fixture-resume", "incomplete": False,
                "letter": "Approved cover letter", "visibility": False,
                "countries": [], "vacancy": "2",
            }
        finally:
            await browser.close()
            await pw.stop()
    asyncio.run(run())


POPUP_URL = "https://hh.ru/applicant/vacancy_response/popup"
POPUP_FIELDS = {
    "_xsrf": "fixture-xsrf",
    "vacancy_id": "2",
    "resume_hash": "fixture-resume",
    "ignore_postponed": "true",
    "incomplete": "false",
    "mark_applicant_visible_in_vacancy_country": "false",
    "country_ids": "[]",
    "letter": "Approved cover letter",
    "lux": "true",
    "withoutTest": "no",
    "hhtmFromLabel": "",
    "hhtmSourceLabel": "",
}


def _request(fields=POPUP_FIELDS, *, duplicate=None, filename=None, url=POPUP_URL, content_type=None):
    boundary = "fixture-boundary"
    chunks = []
    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n")
    if duplicate:
        name, value = duplicate
        chunks.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{name}\"\r\n\r\n{value}\r\n")
    if filename:
        chunks.append(f"--{boundary}\r\nContent-Disposition: form-data; name=\"{filename[0]}\"; filename=\"fixture.txt\"\r\nContent-Type: text/plain\r\n\r\n{filename[1]}\r\n")
    chunks.append(f"--{boundary}--\r\n")
    return SimpleNamespace(
        url=url,
        method="POST",
        headers={"content-type": content_type or f"multipart/form-data; boundary={boundary}"},
        post_data_buffer="".join(chunks).encode("utf-8"),
    )


def test_popup_multipart_helper_requires_exact_observed_field_set_and_values():
    proof = {"url": POPUP_URL, "fields": POPUP_FIELDS}
    assert popup_request_matches(proof, _request()) is True

    altered_resume = {**POPUP_FIELDS, "resume_hash": "other-resume"}
    altered_letter = {**POPUP_FIELDS, "letter": "Different letter"}
    unknown_field = {**POPUP_FIELDS, "unexpected": "value"}
    assert popup_request_matches(proof, _request(duplicate=("resume_hash", "fixture-resume"))) is False
    assert popup_request_matches(proof, _request(filename=("extra", "file"))) is False
    assert popup_request_matches(proof, _request(fields=altered_resume)) is False
    assert popup_request_matches(proof, _request(fields=altered_letter)) is False
    assert popup_request_matches(proof, _request(fields=unknown_field)) is False
    assert popup_request_matches(proof, _request(url=POPUP_URL + "?unexpected=1")) is False
    assert popup_request_matches(proof, _request(content_type="application/json")) is False


def test_popup_multipart_helper_rejects_forged_proof_with_unknown_field():
    forged = {"url": POPUP_URL, "fields": {**POPUP_FIELDS, "unexpected": "value"}}
    assert popup_request_matches(forged, _request()) is False


@pytest.mark.parametrize("content_type", [
    "multipart/form-data; boundary=fixture-boundary; boundary=other",
    "multipart/form-data; boundary=fixture-boundary; charset=utf-8",
    "multipart/form-data; boundary=",
    'multipart/form-data; boundary="fixture-boundary"',
    "multipart/form-data; boundary=fixture-boundary\r\nX-Injected: yes",
])
def test_popup_multipart_helper_rejects_noncanonical_outer_content_type(content_type):
    assert popup_request_matches(
        {"url": POPUP_URL, "fields": POPUP_FIELDS}, _request(content_type=content_type)
    ) is False


@pytest.mark.parametrize("part_header", [
    'Content-Disposition: form-data; name="_xsrf"; name="other"',
    'Content-Disposition: form-data; name*="utf-8\'\'_xsrf"',
    'Content-Disposition: form-data; name="_xsrf"\r\nContent-Disposition: form-data; name="_xsrf"',
    'Content-Disposition: form-data; name="_xsrf"\r\nContent-Transfer-Encoding: base64',
    'Content-Disposition: form-data; name="_xsrf"\r\nContent-Transfer-Encoding: quoted-printable',
])
def test_popup_multipart_helper_rejects_noncanonical_part_headers(part_header):
    boundary = "fixture-boundary"
    chunks = []
    for name, value in POPUP_FIELDS.items():
        header = part_header if name == "_xsrf" else f'Content-Disposition: form-data; name="{name}"'
        chunks.append(f"--{boundary}\r\n{header}\r\n\r\n{value}\r\n")
    chunks.append(f"--{boundary}--\r\n")
    request = SimpleNamespace(
        url=POPUP_URL,
        method="POST",
        headers={"content-type": f"multipart/form-data; boundary={boundary}"},
        post_data_buffer="".join(chunks).encode("utf-8"),
    )
    assert popup_request_matches({"url": POPUP_URL, "fields": POPUP_FIELDS}, request) is False
