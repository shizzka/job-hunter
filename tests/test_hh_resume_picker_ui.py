"""Offline synthetic Magritte portal regression; no candidate/account data."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

from hh.ui import HHUIGuard, HHUnexpectedUI
from hh.apply import selected_resume_matches


FORM = '''<form name="vacancy_response">
    <div data-qa="resume-title">Synthetic QA</div>
    <button id="send" data-qa="vacancy-response-submit-popup">Apply</button>
</form>'''
PICKER = '''<div role="dialog" data-qa="drop-base"><div data-qa="drop">
    <div role="listbox" data-qa="magritte-select-option-list">
      <label role="option" data-magritte-select-option="resume-one" aria-selected="true">
        <input type="radio" value="resume-one" checked><span data-qa="resume-title">Synthetic QA</span>
      </label>
      <label role="option" data-magritte-select-option="resume-two" aria-selected="false">
        <input type="radio" value="resume-two"><span data-qa="resume-title">Synthetic other</span>
      </label>
    </div>
</div></div>'''


async def browser_case(html, check):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            await context.route('**/*', lambda route: route.abort())
            page = await context.new_page()
            await page.set_content(html)
            await check(page)
        finally:
            await browser.close()


def test_native_response_resume_portal_is_allowed_but_does_not_change_selection(tmp_path):
    async def check(page):
        notify = AsyncMock()
        guard = HHUIGuard(tmp_path, notify=notify)
        assert await guard.ensure(page, 'response_controls', allowed=('response', 'captcha')) is False
        assert await selected_resume_matches(page, 'resume-one', 'Synthetic QA')
        assert not await selected_resume_matches(page, 'resume-two', 'Synthetic other')
        assert await page.locator('input:checked').input_value() == 'resume-one'
        notify.assert_not_awaited()
    asyncio.run(browser_case(FORM + PICKER, check))


@pytest.mark.parametrize('form,picker', [
    ('', PICKER),
    (FORM + FORM, PICKER),
    (FORM.replace('vacancy_response', 'profile'), PICKER),
    (FORM.replace('vacancy-response-submit-popup', 'profile-save'), PICKER),
    (FORM.replace('data-qa="resume-title"', 'data-qa="other"'), PICKER),
    (FORM.replace('<form ', '<form style="display:none" '), PICKER),
    (FORM, PICKER.replace('data-qa="drop-base"', 'data-qa="other"')),
    (FORM, PICKER.replace('magritte-select-option-list', 'other-list')),
    (FORM, PICKER.replace('data-qa="resume-title"', 'data-qa="profile-answer"')),
    (FORM, PICKER.replace('type="radio"', 'type="text"')),
    (FORM, PICKER.replace('value="resume-one"', 'value="wrong-id"')),
    (FORM, PICKER.replace('</div></div>', '<textarea name="profile-answer"></textarea></div></div>')),
    (FORM, PICKER.replace('</div></div>', '<button type="submit">Save</button></div></div>')),
    (FORM, PICKER.replace('</div></div>', '<div role="dialog"><h2>New questions</h2></div></div></div>')),
])
def test_unrelated_or_changed_dropdown_still_blocks(tmp_path, form, picker):
    async def check(page):
        guard = HHUIGuard(tmp_path, notify=AsyncMock(return_value=True))
        with pytest.raises(HHUnexpectedUI):
            await guard.ensure(page, 'response_controls', allowed=('response', 'captcha'))
    asyncio.run(browser_case(form + picker, check))


@pytest.mark.parametrize('mutation', [
    "document.querySelector('form').removeAttribute('name')",
    "document.querySelector('[data-qa=drop-base]').insertAdjacentHTML('beforeend', '<input name=profile-question>')",
    "document.body.insertAdjacentHTML('beforeend', '<div role=dialog><h2>Unknown questions</h2></div>')",
])
@pytest.mark.parametrize('event', ['click', 'submit'])
def test_capture_barrier_reclassifies_after_picker_or_page_changes(tmp_path, mutation, event):
    async def check(page):
        guard = HHUIGuard(tmp_path, notify=AsyncMock(return_value=True))
        await guard.ensure(page, 'response_controls', allowed=('response', 'captcha'))
        await page.evaluate("""() => {
            window.sent = 0;
            document.querySelector('form').addEventListener('submit', e => {e.preventDefault(); window.sent++;});
        }""")
        await page.evaluate('() => {' + mutation + '}')
        await page.evaluate("() => document.querySelector('#send').click()" if event == 'click' else
                            "() => document.querySelector('form').requestSubmit()")
        assert await page.evaluate('window.sent') == 0
        with pytest.raises(HHUnexpectedUI):
            await guard.ensure(page, 'after', allowed=('response', 'captcha'))
    asyncio.run(browser_case(FORM + PICKER, check))


def test_resume_picker_is_not_allowed_in_non_response_context(tmp_path):
    async def check(page):
        with pytest.raises(HHUnexpectedUI):
            await HHUIGuard(tmp_path, notify=AsyncMock(return_value=True)).ensure(page, 'chat', allowed=('captcha',))
    asyncio.run(browser_case(FORM + PICKER, check))
