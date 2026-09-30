"""Offline Chromium regression for HH's portalled resume picker."""
import asyncio

from playwright.async_api import async_playwright
from hh.apply import selected_resume_matches


def test_portal_checks_only_selected_resume_id():
    async def run():
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content('''<form name="vacancy_response"><div data-qa="resume-title">QA</div></form>
                <div role="listbox">
                <label data-magritte-select-option="qa-id" aria-selected="true"><input type="radio" value="qa-id" checked><span data-qa="resume-title">QA</span></label>
                <label data-magritte-select-option="wrong-id" aria-selected="false"><input type="radio" value="wrong-id"><span data-qa="resume-title">QA</span></label>
                </div>''')
                assert await selected_resume_matches(page, 'qa-id', 'QA')
                assert not await selected_resume_matches(page, 'wrong-id', 'QA')
                await page.locator('form').evaluate('(el) => el.remove()')
                assert not await selected_resume_matches(page, 'qa-id', 'QA')
            finally:
                await browser.close()
    asyncio.run(run())
