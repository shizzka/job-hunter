"""R3: a form with a form-associated custom element (design-system web component)
is read back with its real submission value, still without dispatching page JS."""
import asyncio

import browser_action_boundary
from tests.test_browser_boundary_commit import synthetic_page
from tests.test_browser_boundary_independent import browser_case

PAGE = '''<form name="vacancy_response"><input type="hidden" name="resume_id" value="target">
<textarea name="letter">Approved</textarea><x-input name="salary" value="180000"></x-input>
<x-input name="ignored" value="nope" disabled></x-input><button type="submit">Submit</button></form>
<script>
customElements.define('x-input', class extends HTMLElement {
  static formAssociated = true;
  constructor() { super(); this.internals = this.attachInternals(); }
  connectedCallback() { this.internals.setFormValue(this.getAttribute('value')); }
});
window.sent = [];
window.addEventListener('formdata', e => window.sent.push('window'), true);
document.addEventListener('formdata', e => window.sent.push('document'));
</script>'''


def test_custom_control_value_is_read_without_formdata_dispatch():
    async def run(context, page):
        await browser_action_boundary.bootstrap_boundary(page)
        await synthetic_page(context, page, PAGE)
        entries = await page.evaluate('''() => [...window.__jhActionBoundary.formData(
            document.querySelector('form'), document.querySelector('button')).entries()]''')
        assert entries == [['resume_id', 'target'], ['letter', 'Approved'], ['salary', '180000']]
        assert await page.evaluate('() => window.sent') == []
        # The guard is released: the page's own FormData still reaches its handlers.
        await page.evaluate("() => new FormData(document.querySelector('form'))")
        assert await page.evaluate('() => window.sent') == ['window', 'document']
    asyncio.run(browser_case(run))


def test_only_disabled_custom_controls_keep_the_inert_copy_path():
    async def run(context, page):
        await browser_action_boundary.bootstrap_boundary(page)
        await synthetic_page(context, page, PAGE.replace('<x-input name="salary" value="180000"></x-input>', ''))
        entries = await page.evaluate("() => [...window.__jhActionBoundary.formData(document.querySelector('form')).entries()]")
        assert entries == [['resume_id', 'target'], ['letter', 'Approved']]
        assert await page.evaluate('() => window.sent') == []
    asyncio.run(browser_case(run))
