import asyncio
from types import SimpleNamespace

import pytest

from commands import resume
from hh_client import HHClient
from hh.resume_refresh import get_resume_ids_readonly, download_resume_readonly


CATALOG = '''<script>fetch('/unexpected', {method: 'POST'});</script>
<a data-qa="resume-card-link-other" href="/resume/other">Same title</a>
<a data-qa="resume-card-link-exact" href="/resume/exact-id">Same title</a>'''
DOCUMENT = '''<script>document.forms[0].requestSubmit()</script>
<h1 data-qa="resume-block-title-position">Fresh title &amp; QA</h1>
<section data-qa="resume-list-card-experience"><h2>Experience</h2><p>Real work</p></section>
<div data-qa="skills-card">Python <span>SQL</span></div>'''


class Response:
    def __init__(self, url, body, status=200, **headers):
        self.url, self.body, self.status = url, body, status
        self.headers = {"content-type": "text/html; charset=utf-8", **headers}
        self.disposed = False

    async def text(self):
        return self.body

    async def dispose(self):
        self.disposed = True


def session(responses):
    calls = []
    async def get(url, **kwargs):
        calls.append((url, kwargs))
        response = responses[len(calls) - 1]
        if isinstance(response, BaseException):
            raise response
        return response
    client = HHClient()
    client._context = SimpleNamespace(request=SimpleNamespace(get=get))
    client._page = SimpleNamespace()  # No goto/evaluate/click APIs available.
    return client, calls


def test_readonly_refresh_never_runs_page_js_and_downloads_exact_id(tmp_path, monkeypatch):
    path = tmp_path / "resume.md"
    path.write_text("old resume")
    monkeypatch.setattr(resume.config, "RESUME_FILE", str(path))
    monkeypatch.setattr(resume.config, "HH_PRIMARY_RESUME_ID", "exact-id")
    monkeypatch.setattr(resume.config, "HH_PRIMARY_RESUME_TITLE", "Configured title")
    monkeypatch.setattr(resume.config, "HH_BASE_URL", "https://hh.ru")
    responses = [Response("https://hh.ru/applicant/resumes", CATALOG),
                 Response("https://hh.ru/resume/exact-id", DOCUMENT)]
    client, calls = session(responses)
    async def start():
        pass
    async def stop(**kwargs):
        assert kwargs == {"persist_cookies": False}
        assert path.read_text() == "old resume"
    client.start, client.stop = start, stop
    monkeypatch.setattr(resume, "HHClient", lambda: client)
    assert asyncio.run(resume.do_hh_resume_refresh()) is True
    assert [call[0] for call in calls] == ["https://hh.ru/applicant/resumes", "https://hh.ru/resume/exact-id"]
    assert all(call[1] == {"max_redirects": 0, "timeout": 30000} for call in calls)
    assert all(r.disposed for r in responses)
    assert path.read_text().startswith("# Fresh title & QA\n")
    assert "Real work" in path.read_text() and "Python SQL" in path.read_text()
    assert resume.config.HH_PRIMARY_RESUME_TITLE == "Configured title"


def test_catalog_ignores_inert_and_foreign_links(monkeypatch):
    monkeypatch.setattr(resume.config, "HH_BASE_URL", "https://hh.ru")
    body = CATALOG + '''<template><a data-qa="resume-card-link-hidden" href="/resume/hidden">Hidden</a></template>
    <a href="https://foreign.test/resume/foreign">Foreign</a><a href="/resume/exact-id?duplicate=1">Duplicate</a>'''
    client, calls = session([Response("https://hh.ru/applicant/resumes", body)])
    result = asyncio.run(get_resume_ids_readonly(client))
    assert [item["id"] for item in result] == ["other", "exact-id"]


@pytest.mark.parametrize("body,status,headers", [
    ('<script>{"userType":"anonymous"}</script>' + CATALOG, 200, {}),
    ('<div data-qa="captcha">Challenge</div>' + CATALOG, 200, {}),
    ('<p>Verify you are human</p>' + CATALOG, 200, {}),
    ("Login form", 200, {}),
    (CATALOG, 401, {}),
    (CATALOG, 200, {"content-type": "application/json"}),
    ("", 302, {"location": "/account/login"}),
    ("", 302, {"location": "https://evil.test/applicant/resumes"}),
])
def test_catalog_fails_closed_for_auth_challenge_redirect_or_unknown_format(monkeypatch, body, status, headers):
    monkeypatch.setattr(resume.config, "HH_BASE_URL", "https://hh.ru")
    response = Response("https://hh.ru/applicant/resumes", body, status, **headers)
    client, calls = session([response])
    with pytest.raises(ValueError):
        asyncio.run(get_resume_ids_readonly(client))
    assert response.disposed and len(calls) == 1


@pytest.mark.parametrize("body,url", [
    ("", "https://hh.ru/resume/exact-id"),
    ('<h1 data-qa="resume-block-title-position">Title only</h1>', "https://hh.ru/resume/exact-id"),
    (DOCUMENT, "https://hh.ru/resume/other"),
    (DOCUMENT, "https://evil.test/resume/exact-id"),
])
def test_readonly_download_rejects_wrong_or_empty_document(monkeypatch, body, url):
    monkeypatch.setattr(resume.config, "HH_BASE_URL", "https://hh.ru")
    response = Response(url, body)
    client, calls = session([response])
    with pytest.raises(ValueError):
        asyncio.run(download_resume_readonly(client, {"id": "exact-id", "url": "/resume/other"}))
    assert calls[0][0] == "https://hh.ru/resume/exact-id" and response.disposed


def test_only_trusted_region_redirect_with_same_exact_path_is_followed(monkeypatch):
    monkeypatch.setattr(resume.config, "HH_BASE_URL", "https://hh.ru")
    responses = [Response("https://hh.ru/resume/exact-id", "", 302,
                          location="https://spb.hh.ru/resume/exact-id"),
                 Response("https://spb.hh.ru/resume/exact-id", DOCUMENT)]
    client, calls = session(responses)
    result = asyncio.run(download_resume_readonly(client, {"id": "exact-id"}))
    assert result["title"] == "Fresh title & QA"
    assert [call[0] for call in calls] == [r.url for r in responses]
    assert all(r.disposed for r in responses)


def test_current_hh_catalog_redirect_to_profile_is_read_without_ui_interaction(monkeypatch):
    monkeypatch.setattr(resume.config, "HH_BASE_URL", "https://hh.ru")
    responses = [Response("https://hh.ru/applicant/resumes", "", 302,
                          location="/applicant/profile/me"),
                 Response("https://hh.ru/applicant/profile/me", CATALOG)]
    client, calls = session(responses)
    assert [r["id"] for r in asyncio.run(get_resume_ids_readonly(client))] == ["other", "exact-id"]
    assert [call[0] for call in calls] == [r.url for r in responses]
    assert all(r.disposed for r in responses)


def test_profile_redirect_never_substitutes_for_exact_resume_document(monkeypatch):
    monkeypatch.setattr(resume.config, "HH_BASE_URL", "https://hh.ru")
    response = Response("https://hh.ru/resume/exact-id", "", 302, location="/applicant/profile/me")
    client, calls = session([response])
    with pytest.raises(ValueError):
        asyncio.run(download_resume_readonly(client, {"id": "exact-id"}))
    assert len(calls) == 1 and response.disposed


def test_request_failure_preserves_old_resume(tmp_path, monkeypatch):
    path = tmp_path / "resume.md"
    path.write_text("old resume")
    monkeypatch.setattr(resume.config, "RESUME_FILE", str(path))
    monkeypatch.setattr(resume.config, "HH_PRIMARY_RESUME_ID", "exact-id")
    monkeypatch.setattr(resume.config, "HH_BASE_URL", "https://hh.ru")
    client, calls = session([TimeoutError("synthetic download timeout")])
    async def nothing(**kwargs):
        pass
    client.start = client.stop = nothing
    monkeypatch.setattr(resume, "HHClient", lambda: client)
    assert asyncio.run(resume.do_hh_resume_refresh()) is False
    assert path.read_text() == "old resume" and len(calls) == 1
