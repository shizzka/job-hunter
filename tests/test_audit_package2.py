"""Actual Chromium auto-wait races; all external requests aborted."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

import hh_client
from hh import apply as hh_apply


@pytest.mark.parametrize('change', ['resume', 'letter', 'answer', 'shape', 'none'])
@pytest.mark.parametrize('obstacle', ['disabled', 'covered'])
def test_approved_state_still_required_when_click_autowait_finishes(change, obstacle):
    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                context = await browser.new_context()
                requests = []
                async def abort(route):
                    requests.append(route.request.url)
                    await route.abort()
                await context.route('**/*', abort)
                page = await context.new_page()
                disabled = 'disabled' if obstacle == 'disabled' else ''
                await page.set_content('''<form name="vacancy_response" action="https://offline.invalid/submit">
                  <input name="resume_id" value="target" type="hidden">
                  <textarea name="letter">Approved letter</textarea>
                  <input name="skill" data-codex-auto-field-id="skill" value="SQL">
                  <button id="submit" type="submit" ''' + disabled + '''>Apply</button>
                </form><script>window.sent=[];
                document.querySelector('form').addEventListener('submit', e => {
                  e.preventDefault(); window.sent.push(Object.fromEntries(new FormData(e.target)));
                });</script>''')
                if obstacle == 'covered':
                    await page.evaluate("() => { const d=document.createElement('div'); d.id='cover'; d.style='position:fixed;inset:0;background:white;z-index:100'; document.body.append(d); }")
                client = hh_client.HHClient()
                client._page = page
                client._approved_hh_payload = {'resume_id': 'target', 'cover_letter': 'Approved letter',
                    'answers': [{'field_id': 'skill', 'answer': 'SQL'}]}
                page.wait_for_timeout = AsyncMock()  # Keep the race inside actual element.click().
                async def guard():
                    assert await hh_apply.selected_resume_matches(page, 'target', '')
                    assert await page.locator('[name=letter]').input_value() == 'Approved letter'
                    await page.evaluate('''change => setTimeout(() => {
                        if(change==='resume') document.querySelector('[name=resume_id]').value='wrong';
                        if(change==='letter') document.querySelector('[name=letter]').value='Unapproved';
                        if(change==='answer') document.querySelector('[name=skill]').value='Invented';
                        if(change==='shape') document.querySelector('form').insertAdjacentHTML('beforeend','<input name="consent" value="yes">');
                        document.querySelector('#submit').disabled=false;
                        document.querySelector('#cover')?.remove();
                    }, 150)''', change)
                    return True
                await client._click_with_fallbacks(await page.query_selector('#submit'), 'submit_button', before_click=guard)
                sent = await page.evaluate('() => window.sent')
                assert len(sent) == (1 if change == 'none' else 0)
                assert not requests
            finally:
                await browser.close()
    asyncio.run(run())
