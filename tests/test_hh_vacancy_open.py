"""Offline regressions for the initial HH apply vacancy navigation."""
import asyncio
import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import hh.apply as hh_apply
import hh_client
from tests.test_hh_search_session import retain_unproven_attempt
from hh.ui import HHUnexpectedUI
_DETAILS_SPEC = importlib.util.spec_from_file_location(
    "hh_vacancy_details_helpers", Path(__file__).with_name("test_hh_vacancy_details.py")
)
_DETAILS = importlib.util.module_from_spec(_DETAILS_SPEC)
_DETAILS_SPEC.loader.exec_module(_DETAILS)
HTML = _DETAILS.HTML
NEXT_URL = _DETAILS.NEXT_URL
URL = _DETAILS.URL
browser_scenario = _DETAILS.browser_scenario

# Isolate the first parser event (the static MTS iframe), as in the observed
# details regression. The page's unrelated synthetic POST telemetry is omitted.
BODY = HTML[: HTML.index("<script>window.syntheticPageScriptRan")] + "</body></html>"


class VacancyOpenBoundary(Exception):
    """Stop at the first vacancy-open check, before response controls or submit."""


class ResumePreflightBoundary(BaseException):
    """Stop after the ordinary response GET, before inspecting response controls."""


async def invoke_apply(client):
    return await hh_apply._apply_to_vacancy(
        client,
        URL,
        preferred_resume_id="synthetic-resume-id",
        absolute_hh_url=lambda value: value,
        anti_bot_message=lambda *args: "synthetic anti-bot message",
        logger=hh_client.log,
    )


def install_vacancy_open_boundary(monkeypatch):
    original = hh_apply.ensure_session_ui

    async def guard(session, stage, *, allowed=()):
        result = await original(session, stage, allowed=allowed)
        if stage == "vacancy_open":
            raise VacancyOpenBoundary(stage)
        return result

    monkeypatch.setattr(hh_apply, "ensure_session_ui", guard)


def test_initial_apply_response_get_keeps_functional_document_after_vacancy_open(
    tmp_path, monkeypatch
):
    response_url = "https://hh.ru/applicant/vacancy_response?vacancyId=123"
    response_body = """<!doctype html><html><body><main>Ordinary response page</main>
<script>window.responseScriptRan = true;</script></body></html>"""

    async def check(client, requests, frames, fetched):
        client._ui_home = str(tmp_path)
        client._save_debug_snapshot = AsyncMock()
        client._detect_anti_bot_kind = AsyncMock(return_value=None)
        client._handle_anti_bot_with_solver = AsyncMock(return_value=None)
        client._page_closed_or_archived = AsyncMock(return_value=False)
        client._has_existing_response_ui = AsyncMock(return_value=False)
        client._apply_success_detected = AsyncMock(return_value=False)
        client._response_requires_questions = AsyncMock(return_value=False)
        response_requests = []

        async def response_route(route):
            response_requests.append((route.request.method, route.request.url))
            await route.fulfill(status=200, content_type="text/html", body=response_body)

        # More specific than the fixture helper's offline catch-all route.
        await client._context.route(response_url, response_route)
        from playwright.async_api import Route
        from types import SimpleNamespace
        original_fetch = Route.fetch

        async def response_fetch(route, **options):
            target = options.get('url', route.request.url)
            if target != response_url:
                return await original_fetch(route, **options)
            assert route.request.method == 'GET'
            assert options == {
                'url': response_url, 'max_redirects': 0,
                'max_retries': 0, 'timeout': 20000,
            }
            response_requests.append(('GET', target))
            return SimpleNamespace(
                status=200, url=target, headers={'content-type': 'text/html'},
                text=AsyncMock(return_value=response_body), dispose=AsyncMock(),
            )

        monkeypatch.setattr(Route, 'fetch', response_fetch)


        original = hh_apply.ensure_session_ui

        async def guard(session, stage, *, allowed=()):
            result = await original(session, stage, allowed=allowed)
            if stage == "resume_preflight":
                raise ResumePreflightBoundary(stage)
            return result

        monkeypatch.setattr(hh_apply, "ensure_session_ui", guard)

        with pytest.raises(ResumePreflightBoundary, match="resume_preflight"):
            await invoke_apply(client)

        page = client._page
        assert page.url == response_url
        assert await page.locator("main").inner_text() == "Ordinary response page"
        assert await page.evaluate("window.responseScriptRan") is True
        assert await page.evaluate("Boolean(window.__jhActionBoundary)")
        assert frames == []
        assert all(method == "GET" for method, _ in requests + response_requests)
        assert len(fetched) == 1 and fetched[0][0] == URL
        assert response_requests == [("GET", response_url)]
        assert getattr(client, "_external_attempt", None) is None
        assert not page._hh_action_monitor.unknown
        assert not client._context._hh_action_watch.unknown
        assert await page.evaluate("document.forms.length") == 0

    asyncio.run(browser_scenario(tmp_path, monkeypatch, check, body=BODY))


def test_initial_apply_reload_handles_static_mts_embed_before_vacancy_open_boundary(
    tmp_path, monkeypatch
):
    async def check(client, requests, frames, fetched):
        client._ui_home = str(tmp_path)
        client._save_debug_snapshot = AsyncMock()
        client._detect_anti_bot_kind = AsyncMock(return_value=None)
        client._handle_anti_bot_with_solver = AsyncMock(return_value=None)
        client._page_closed_or_archived = AsyncMock(return_value=False)
        client._has_existing_response_ui = AsyncMock(return_value=False)
        client._apply_success_detected = AsyncMock(return_value=False)
        client._response_requires_questions = AsyncMock(return_value=False)
        install_vacancy_open_boundary(monkeypatch)

        boundary_reached = False
        try:
            await invoke_apply(client)
        except VacancyOpenBoundary as exc:
            assert str(exc) == "vacancy_open"
            boundary_reached = True
        except HHUnexpectedUI as exc:
            pytest.fail(
                "apply stopped before the safe vacancy-open boundary: "
                f"stage={exc.stage}, reason={getattr(exc, 'hh_stop_reason', '')}, "
                f"uncertain={getattr(exc, 'hh_uncertain', False)}"
            )
        assert boundary_reached, "apply returned before the synthetic vacancy_open boundary"

        page = client._page
        assert page.url == URL
        assert await page.locator('[data-qa="vacancy-title"]').inner_text() == "QA Engineer"
        assert await page.locator('[data-qa="vacancy-company-name"]').inner_text() == "Synthetic employer"
        assert await page.locator('[data-qa="vacancy-description"]').inner_text() == "Test browser lifecycle and Python services."
        assert await page.evaluate("Boolean(window.__jhActionBoundary)")
        assert await page.evaluate("Boolean(window.syntheticPageScriptRan)") is False
        assert frames == []
        assert all(method == "GET" for method, _ in requests)
        assert len(fetched) == 1
        assert fetched[0][0] == URL
        assert getattr(client, "_external_attempt", None) is None
        assert not page._hh_action_monitor.unknown
        assert not client._context._hh_action_watch.unknown

        # The scoped vacancy-open policy must not leak into a later ordinary page.
        await page.goto(NEXT_URL)
        assert await page.evaluate("window.nextPageScriptRan") is True
        assert await page.evaluate("Boolean(window.syntheticPageScriptRan)") is False
        assert not page._hh_action_monitor.unknown
        assert not client._context._hh_action_watch.unknown

    asyncio.run(browser_scenario(tmp_path, monkeypatch, check, body=BODY))


def test_initial_apply_does_not_clear_preexisting_uncertainty(tmp_path, monkeypatch):
    async def check(template_client, requests, frames, fetched):
        client = template_client
        await retain_unproven_attempt(client, tmp_path)
        client._page._hh_action_monitor.mark_unknown()

        with pytest.raises(HHUnexpectedUI) as raised:
            await invoke_apply(client)

        assert raised.value.hh_stop_reason == "possible_external_action"
        assert raised.value.hh_uncertain is True
        assert client._page.url == "about:blank"
        assert fetched == [] and requests == [] and frames == []
        assert client._page._hh_action_monitor.unknown
        assert client._context._hh_action_watch.unknown

    asyncio.run(browser_scenario(tmp_path, monkeypatch, check))


@pytest.mark.parametrize(
    "body",
    [
        HTML.replace('id="mts-pixel"', 'id="unknown-embed"'),
        HTML.replace(
            "document.head.appendChild(sfmb);",
            "document.body.appendChild(sfmb);",
        ),
        HTML.replace(
            "https://sm.rtb.mts.ru/s?p=", "https://unknown.invalid/action?p="
        ),
        HTML.replace(
            'style="display:none" srcdoc=',
            'style="display:none" src="https://unknown.invalid/" srcdoc=',
        ),
    ],
    ids=["unknown-id", "same-id-altered-script", "same-id-altered-network", "same-id-added-src"],
)
def test_initial_apply_unknown_or_tampered_embed_fails_closed(
    tmp_path, monkeypatch, body
):
    async def check(template_client, requests, frames, fetched):
        client = template_client

        with pytest.raises(ValueError, match="Unknown vacancy details embed"):
            await invoke_apply(client)

        assert getattr(client, "_external_attempt", None) is None
        assert all(method == "GET" for method, _ in requests)
        assert requests == []
        assert frames == []
        assert len(fetched) == 1
        assert fetched[0][0] == URL
        assert not client._page._hh_action_monitor.unknown
        assert not client._context._hh_action_watch.unknown

        # Failed validation must release any temporary route/CSP handling.
        await client._page.goto(NEXT_URL)
        assert await client._page.evaluate("window.nextPageScriptRan") is True
        assert not client._page._hh_action_monitor.unknown
        assert not client._context._hh_action_watch.unknown

    asyncio.run(browser_scenario(tmp_path, monkeypatch, check, body=body))
