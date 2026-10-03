"""Native browser helpers against synthetic DOM; every external request aborted."""
import asyncio
import logging

import pytest
from playwright.async_api import async_playwright

from hh import forms


HTML = """<fieldset><legend>Формат работы</legend>
<select name="format"><option value="">Выберите</option>
<option value="disabled" disabled>Неактивно</option><option value="remote">Удалённая работа</option></select>
</fieldset><fieldset><legend>Формат занятости</legend>
<label style="display:none"><input type="radio" name="hours" value="hidden">Скрыто</label>
<label><input type="radio" name="hours" value="disabled" disabled>Неактивно</label>
<label><input type="radio" name="hours" value="full">Полная занятость</label>
</fieldset><fieldset><legend>Навык</legend><input name="skill" aria-label="Навык"></fieldset>"""


@pytest.mark.parametrize('change', ['none', 'text', 'select', 'radio', 'duplicate_field'])
def test_real_dom_indexes_and_exact_readback_block_mutations(change):
    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                context = await browser.new_context()
                await context.route('**/*', lambda route: route.abort())
                page = await context.new_page()
                await page.set_content(HTML)
                inspected = await forms.inspect_employer_questions(page, logger=logging.getLogger('synthetic'))
                select, radio, text = inspected['fields']
                assert [o['index'] for o in select['options']] == [2]
                assert [o['index'] for o in radio['options']] == [2]
                answers = [dict(field_id=select['field_id'], control='select', selected_indices=[2]),
                           dict(field_id=radio['field_id'], control='radio', selected_indices=[2]),
                           dict(field_id=text['field_id'], answer='SQL')]
                assert (await forms.fill_employer_question_answers(page, answers, logger=logging.getLogger('synthetic')))['filled'] == 3
                assert await forms.verify_filled_answers(page, answers)
                if change == 'text': await page.locator('[name=skill]').fill('Invented experience')
                if change == 'select': await page.locator('[name=format]').select_option('')
                if change == 'radio': await page.locator('[name=hours]').evaluate_all('els => els.forEach(el => el.checked = false)')
                if change == 'duplicate_field': await page.locator('[name=skill]').evaluate('el => el.after(el.cloneNode(true))')
                assert await forms.verify_filled_answers(page, answers) is (change == 'none')
            finally:
                await browser.close()
    asyncio.run(run())
