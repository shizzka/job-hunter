"""Exact Google Forms readback with fakes and offline Chromium."""
import asyncio
import pytest
from playwright.async_api import async_playwright
from google_forms import filling
from tests.test_google_forms_filling import FakeField, FakeItem, FakeOption, FakePage


@pytest.mark.parametrize('scenario', ['extra_stuck', 'desired_stuck', 'unavailable', 'unknown_desired'])
def test_checkboxes_require_all_and_only_approved_options(scenario):
    a, b, extra = FakeOption('A'), FakeOption('B'), FakeOption('Extra')
    if scenario == 'extra_stuck': extra.checked = True
    if scenario == 'desired_stuck':
        async def no_change(**kwargs): pass
        b.click = no_change
    options = [a, extra] if scenario == 'unavailable' else [a, b, extra]
    question = {'index': 0, 'type': 'checkbox', 'options': ['A','B','Extra'], 'required': True}
    desired = ['A','Unknown'] if scenario == 'unknown_desired' else ['A','B']
    result = asyncio.run(filling.fill_form(FakePage([FakeItem(options=options)]), [question],
                [{'index': 0, 'options': desired}]))
    assert not result['filled']
    assert not filling._google_form_preview_status([question], result)[0]


def test_text_readback_failure_cannot_substitute_expected():
    field = FakeField()
    async def unreadable(): raise RuntimeError('readback transport failed')
    field.input_value = unreadable
    question = {'index': 0, 'type': 'text', 'required': True}
    result = asyncio.run(filling.fill_form(FakePage([FakeItem(field=field)]), [question],
                                           [{'index': 0, 'answer': 'Approved'}]))
    assert not result['filled']


@pytest.mark.parametrize('mutation', ['extra', 'missing', 'text', 'none'])
def test_exact_values_rechecked_after_all_fields_are_filled(mutation):
    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                context = await browser.new_context()
                await context.route('**/*', lambda route: route.abort())
                page = await context.new_page()
                await page.set_content('''<div role="listitem"><div role="checkbox" aria-label="A" aria-checked="false" tabindex="0" onclick="this.setAttribute('aria-checked',this.getAttribute('aria-checked')==='true'?'false':'true')">A</div>
                <div role="checkbox" aria-label="B" aria-checked="false" tabindex="0" onclick="this.setAttribute('aria-checked',this.getAttribute('aria-checked')==='true'?'false':'true')">B</div>
                <div role="checkbox" aria-label="Extra" aria-checked="false" tabindex="0" onclick="this.setAttribute('aria-checked',this.getAttribute('aria-checked')==='true'?'false':'true')">Extra</div></div>
                <div role="listitem"><input type="text"></div>''')
                await page.evaluate('''mutation => document.querySelector('input').addEventListener('input', e => {
                    if(mutation==='extra') document.querySelector('[aria-label=Extra]').setAttribute('aria-checked','true');
                    if(mutation==='missing') document.querySelector('[aria-label=B]').setAttribute('aria-checked','false');
                    if(mutation==='text') e.target.value='Unapproved';
                })''', mutation)
                questions = [{'index': 0, 'type': 'checkbox', 'options': ['A','B','Extra'], 'required': False},
                             {'index': 1, 'type': 'text', 'required': True}]
                answers = [{'index': 0, 'options': ['A','B']}, {'index': 1, 'answer': 'Approved'}]
                result = await filling.fill_form(page, questions, answers)
                ready, _ = filling._google_form_preview_status(questions, result)
                assert ready is (mutation == 'none')
                assert len(result['filled']) == (2 if mutation == 'none' else 1)
            finally: await browser.close()
    asyncio.run(run())
