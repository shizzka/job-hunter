"""Exercise real HH preflight against a synthetic current-DOM fixture, no submit."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from playwright.async_api import async_playwright, ElementHandle

import hh_client
from hh import apply
from hh.ui import HHUnexpectedUI

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


async def run_preflight(tmp_path, *, mode='', expected_id='qa-target', already_applied=False):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            # Every navigation resolves locally. Neither HH nor any other host is contacted.
            async def fixture(route):
                await route.fulfill(status=200, content_type='text/html', body=HTML)
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
            trace = PreflightTrace()
            # Picker delays are unnecessary in this synchronous synthetic DOM.
            page.wait_for_timeout = AsyncMock()
            try:
                with patch.object(ElementHandle, 'click', click):
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


def test_current_picker_refetches_options_opened_by_exact_readback(tmp_path):
    result, selected, toggles = asyncio.run(run_preflight(tmp_path))
    assert result['resume_selection_verified'] is True
    assert set(selected['ids']) == {'qa-target'}
    # One open to inspect the default, then reopen to read back the selected ID.
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
