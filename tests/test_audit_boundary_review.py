"""Additional local boundary review after A1–A8; synthetic state/browser only."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

import hh_client
import manual_apply_queue as queue
from hh import apply as hh_apply
from hh.submit_boundary import arm_submit_boundary
from tests.test_audit_package1 import isolated, manual


def test_late_revocation_cannot_erase_a_confirmed_external_outcome(manual):
    owner = queue.claim_candidate(manual)['owner']
    assert queue.begin_external(manual, owner)
    queue.record_feedback(manual, 'bad')
    assert queue.finish_candidate(manual, owner, 'applied')
    assert queue.get_candidate(manual)['status'] == 'applied'
    assert queue.claim_candidate(manual) is None


def test_generic_status_update_cannot_reopen_uncertain_attempt(manual):
    owner = queue.claim_candidate(manual)['owner']
    assert queue.begin_external(manual, owner)
    assert queue.finish_candidate(manual, owner, 'failed')
    queue.mark_candidate(manual, 'pending')
    assert queue.get_candidate(manual)['status'] == 'uncertain'
    assert queue.claim_candidate(manual) is None


@pytest.mark.parametrize('change', ['outside_control', 'replaced_form'])
def test_actual_action_must_belong_to_approved_form(change):
    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                context = await browser.new_context()
                await context.route('**/*', lambda route: route.abort())
                page = await context.new_page()
                await page.set_content('''<form name="vacancy_response">
                    <input name="resume_id" value="target" type="hidden">
                    <textarea name="letter">Approved</textarea>
                    <button type="submit">Submit</button></form>
                    <button id="outside" type="button" onclick="window.sent++">Other action</button>
                    <script>window.sent=0; document.addEventListener('submit', e=>{
                        e.preventDefault(); window.sent++;
                    });</script>''')
                client = hh_client.HHClient()
                client._page = page
                client._approved_hh_payload = {'resume_id': 'target', 'cover_letter': 'Approved', 'answers': []}
                page.wait_for_timeout = AsyncMock()
                if change == 'outside_control':
                    await client._click_with_fallbacks(await page.query_selector('#outside'),
                            'submit_button', before_click=AsyncMock(return_value=True))
                else:
                    assert await arm_submit_boundary(client)
                    await page.evaluate('''() => {
                        const old=document.querySelector('form');
                        const other=old.cloneNode(true); old.replaceWith(other); other.requestSubmit();
                    }''')
                assert await page.evaluate('() => window.sent') == 0
            finally:
                await browser.close()
    asyncio.run(run())
