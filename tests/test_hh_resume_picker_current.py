"""Exercise real HH preflight against a synthetic current-DOM fixture, no submit."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from playwright.async_api import async_playwright, ElementHandle

import hh_client
from hh import apply
from hh.ui import HHUnexpectedUI
from debug_trace import ApplyTrace

HTML = (Path(__file__).parent / 'fixtures/hh/resume_picker_current.html').read_text()


class PickerComplete(Exception):
    """Stop successful preflight before the apply flow can reach submit."""


class PreflightTrace:
    def __init__(self):
        self.selection = None

    def event(self, stage, *, ok=None, **fields):
        if stage == 'RESUME_SELECTED':
            self.selection = ok
            if ok:
                raise PickerComplete

    async def capture(self, *args, **kwargs):
        pass


async def run_preflight(tmp_path, *, mode='', expected_id='qa-target', already_applied=False, html=HTML, trace=None):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            # Every navigation resolves locally. Neither HH nor any other host is contacted.
            async def fixture(route):
                await route.fulfill(status=200, content_type='text/html', body=html)
            await context.route('**/*', fixture)
            page = await context.new_page()
            await page.add_init_script("""document.addEventListener('DOMContentLoaded', () => {
                if (window.testMode === 'missing-id') window.options = window.options.filter(o => o.id !== 'qa-target');
                if (window.testMode === 'non-id-titles') window.options.filter(o => o.id !== 'electrician-id').forEach(o => o.id = '');
                window.mode = window.testMode;
            });""")
            await page.add_init_script('window.testMode = ' + repr(mode))
            client = hh_client.HHClient()
            client._page = page
            client._ui_home = str(tmp_path)
            from hh.ui import HHUIGuard
            from types import SimpleNamespace
            from playwright.async_api import Route
            client._context = context
            client._ui_guard = HHUIGuard(str(tmp_path), notify=AsyncMock(return_value=False))

            async def fetch(route, **options):
                target = options.pop('url', route.request.url)
                assert target in {
                    'https://hh.ru/vacancy/1',
                    'https://hh.ru/applicant/vacancy_response?vacancyId=1',
                }
                assert route.request.method == 'GET'
                assert options == {'max_redirects': 0, 'max_retries': 0, 'timeout': 20000}
                return SimpleNamespace(
                    status=200, url=target, headers={'content-type': 'text/html'},
                    text=AsyncMock(return_value=html), dispose=AsyncMock(),
                )

            client._save_debug_snapshot = AsyncMock()
            client._detect_anti_bot_kind = AsyncMock(return_value=None)
            client._handle_anti_bot_with_solver = AsyncMock(return_value=None)
            client._page_closed_or_archived = AsyncMock(return_value=False)
            client._has_existing_response_ui = AsyncMock(return_value=already_applied)
            client._apply_success_detected = AsyncMock(return_value=already_applied)
            client._response_requires_questions = AsyncMock(return_value=False)
            # Keep real controls, exact identity readback, picker and click implementation.
            original_click = ElementHandle.click
            async def click(element, **kwargs):
                if await element.get_attribute('data-magritte-select-option') == 'qa-target':
                    if mode == 'throw-on-click':
                        raise RuntimeError('Synthetic picker click failure')
                    if mode == 'detach-on-click':
                        await element.evaluate('(el) => el.remove()')
                return await original_click(element, **kwargs)
            submit = AsyncMock(side_effect=AssertionError('Picker tests must never submit'))
            client._submit_response_form_via_dom = submit
            trace = trace or PreflightTrace()
            # Picker delays are unnecessary in this synchronous synthetic DOM.
            page.wait_for_timeout = AsyncMock()
            try:
                with patch.object(ElementHandle, 'click', click), patch.object(Route, 'fetch', fetch):
                    result = await apply._apply_to_vacancy(
                        client, 'https://hh.ru/vacancy/1',
                        preferred_resume_id=expected_id, preferred_resume_title='Synthetic QA', trace=trace,
                        absolute_hh_url=lambda url: url, anti_bot_message=lambda *a: '', logger=hh_client.log,
                    )
            except PickerComplete:
                result = {'resume_selection_verified': trace.selection}
            except HHUnexpectedUI:
                result = {'ok': False, 'resume_selection_verified': False, 'ui_blocked': True}
            submit.assert_not_awaited()
            if page.url == 'about:blank':
                assert not expected_id
                return result, None, 0
            assert await page.evaluate('window.submitCalls') == 0
            selected = await page.evaluate(apply.SELECTED_RESUME_SCRIPT)
            return result, selected, await page.evaluate('window.toggleCalls')
        finally:
            await browser.close()


def test_current_picker_opens_options_and_verifies_exact_selection(tmp_path):
    result, selected, toggles = asyncio.run(run_preflight(tmp_path))
    assert result['resume_selection_verified'] is True
    assert set(selected['ids']) == {'qa-target'}
    # One open to select the target, then reopen to read back its exact ID.
    assert toggles == 2


@pytest.mark.parametrize('mode', ['missing-id', 'non-id-titles', 'remove-after-click', 'throw-on-click', 'detach-on-click'])
def test_current_picker_identity_and_click_failures_remain_closed(tmp_path, mode):
    result, selected, _ = asyncio.run(run_preflight(tmp_path, mode=mode))
    assert not result.get('ok')
    assert not result.get('resume_selection_verified')
    assert set(selected['ids']) != {'qa-target'}


def test_ambiguous_titles_without_expected_id_never_enter_picker(tmp_path):
    result, selected, toggles = asyncio.run(run_preflight(tmp_path, expected_id=''))
    assert result['reason'] == 'hh_resume_target_unresolved'
    assert not result['resume_selection_verified']
    assert selected is None and toggles == 0


def test_existing_response_keeps_unknown_original_resume_identity(tmp_path):
    result, _, toggles = asyncio.run(run_preflight(tmp_path, already_applied=True))
    assert result['ok'] and result['already_applied']
    assert result['resume_selection_verified'] is False
    assert result['selected_resume_id'] == ''
    assert result['resume_selection_status'] == 'unknown_existing_response'
    assert toggles == 0


def test_default_electrician_does_not_verify_qa_target(tmp_path):
    result, selected, _ = asyncio.run(run_preflight(tmp_path, mode='missing-id'))
    assert not result['resume_selection_verified']
    assert selected['ids'] != ['qa-target']


@pytest.mark.parametrize('control', [
    '<div role="button" tabindex="0">Other control</div>',
    '<div data-qa="cell">Other cell</div>',
    '<label data-qa="cell"><input type="radio" value="qa-target">Other choice</label>',
    '<div data-qa="cell-text-content" data-resume-id="qa-target">Other content</div>',
])
def test_unrecognized_picker_stops_without_any_click_and_saves_private_structure(tmp_path, control):
    html = (Path(__file__).parent / 'fixtures/hh/resume_picker_unrecognized.html').read_text()
    html = html.replace('<!-- control -->', control)
    trace = ApplyTrace.create(home_dir=str(tmp_path), source='hh', vacancy_id='1',
                              profile='synthetic', mode='manual')
    result, selected, clicks = asyncio.run(run_preflight(tmp_path, html=html, trace=trace))
    assert result == {
        'ok': False, 'reason': 'hh_resume_picker_unrecognized', 'proven_no_action': True,
        'message': 'hh не показал список резюме — откликнитесь вручную или попробуйте позже',
        'resume_selection_verified': False,
    }
    assert not selected['ids'] and clicks == 0
    artifact = trace.trace_dir / 'response_form_unrecognized.html'
    content = artifact.read_text()
    assert 'form name="vacancy_response"' in content
    assert 'data-qa="vacancy-response-submit-popup"' in content
    assert '[REDACTED]' in content
    for private in ('Синтетиков', 'Тестовое резюме', '+7 (999)', '79991234567',
                    'synthetic@example.invalid', 'synthetic-cookie', 'private-id', 'Private title'):
        assert private not in content
    assert artifact.stat().st_mode & 0o777 == 0o600
    trace.finish(ok=False, message=result['message'])
    assert artifact.name in trace.summary_path.read_text()
