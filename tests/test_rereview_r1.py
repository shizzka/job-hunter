"""R1: full associated successful controls, real Chromium, no external traffic."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

import hh_client


@pytest.mark.parametrize('mutation', ['add_resume', 'add_letter', 'add_consent', 'change_resume',
                                    'change_letter', 'change_consent', 'change_owner', 'none'])
@pytest.mark.parametrize('obstacle', ['disabled', 'covered'])
def test_hh_actual_formdata_bound_to_all_associated_controls(mutation, obstacle):
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
                await page.set_content('''<form id="approved" name="vacancy_response">
                    <input name="resume_id" value="target" type="hidden">
                    <textarea name="letter">Approved</textarea>
                    <button id="submit" type="submit">Apply</button></form>
                    <input id="external_resume" form="approved" name="resumeHash" value="target">
                    <textarea id="external_letter" form="approved" name="letter" disabled>Approved</textarea>
                    <input id="external_consent" form="approved" name="consent" value="reviewed">
                    <script>window.sent=[]; document.addEventListener('submit', e=>{
                        e.preventDefault(); window.sent.push([...new FormData(e.target,e.submitter).entries()]);
                    });</script>''')
                if obstacle == 'disabled':
                    await page.locator('#submit').evaluate('el => el.disabled=true')
                else:
                    await page.evaluate("() => {const el=document.createElement('div');el.id='cover';el.style='position:fixed;inset:0;z-index:999;background:white';document.body.append(el)}")
                page.wait_for_timeout = AsyncMock()
                client = hh_client.HHClient()
                client._page = page
                client._approved_hh_payload = {'resume_id': 'target', 'cover_letter': 'Approved', 'answers': []}
                async def approved():
                    await page.evaluate('''mutation => setTimeout(() => {
                        if(mutation.startsWith('add_')) {
                            const el=document.createElement('input');el.setAttribute('form','approved');
                            el.name={add_resume:'resume_id',add_letter:'letter',add_consent:'new_consent'}[mutation];
                            el.value='Unapproved';document.body.append(el);
                        }
                        if(mutation==='change_resume') document.querySelector('#external_resume').value='wrong';
                        if(mutation==='change_letter') {const el=document.querySelector('#external_letter');el.disabled=false;el.value='Unapproved';}
                        if(mutation==='change_consent') document.querySelector('#external_consent').value='Unapproved';
                        if(mutation==='change_owner') document.querySelector('#external_consent').removeAttribute('form');
                        document.querySelector('#submit').disabled=false; document.querySelector('#cover')?.remove();
                    }, 150)''', mutation)
                    return True
                await client._click_with_fallbacks(await page.query_selector('#submit'), 'submit_button', before_click=approved)
                sent = await page.evaluate('() => window.sent')
                assert len(sent) == (1 if mutation == 'none' else 0)
                if sent:
                    assert sent[0] == [['resume_id','target'],['letter','Approved'],['resumeHash','target'],['consent','reviewed']]
                assert not requests
            finally:
                await browser.close()
    asyncio.run(run())
