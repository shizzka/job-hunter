"""R3: approved Google Forms values at actual submit event, offline Chromium."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright
from google_forms import filling


@pytest.mark.parametrize('mutation', ['text', 'extra', 'missing', 'control', 'item', 'root', 'none'])
@pytest.mark.parametrize('dispatch', ['native_form', 'custom_button'])
def test_forms_submit_revalidates_approved_values_after_readback(mutation, dispatch):
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
                button = '<button id="submit" type="submit">Submit</button>' if dispatch == 'native_form' else '<div id="submit" role="button" tabindex="0" onclick="window.record()">Submit</div>'
                await page.set_content('''<form id="approved"><div role="listitem"><input type="text" name="entry.1"></div>
                    <div role="listitem"><div role="checkbox" aria-label="A" aria-checked="false" tabindex="0" onclick="this.setAttribute('aria-checked',this.getAttribute('aria-checked')==='true'?'false':'true')">A</div>
                    <div role="checkbox" aria-label="Extra" aria-checked="false" tabindex="0" onclick="this.setAttribute('aria-checked',this.getAttribute('aria-checked')==='true'?'false':'true')">Extra</div></div>'''+button+'''</form>
                    <script>window.sent=[];window.record=()=>window.sent.push({text:document.querySelector('input[type=text]').value,
                        options:[...document.querySelectorAll('[aria-checked=true]')].map(el=>el.getAttribute('aria-label')),
                        data:[...new FormData(document.querySelector('form')).entries()]});
                    document.addEventListener('submit',e=>{e.preventDefault();window.record()});</script>''')
                page.wait_for_timeout = AsyncMock()
                questions = [{'index':0,'type':'text','required':True},
                             {'index':1,'type':'checkbox','options':['A','Extra'],'required':True}]
                answers = [{'index':0,'answer':'Approved'}, {'index':1,'options':['A']}]
                readback = await filling.fill_form(page, questions, answers)
                assert filling._google_form_preview_status(questions, readback)[0]
                await page.evaluate('''mutation => {
                    const cover=document.createElement('div');cover.id='cover';cover.style='position:fixed;inset:0;z-index:999;background:white';document.body.append(cover);
                    setTimeout(()=>{
                        const button=document.querySelector('#submit'),root=document.querySelector('form');
                        if(mutation==='text') document.querySelector('input[type=text]').value='Unapproved';
                        if(mutation==='extra') document.querySelector('[aria-label=Extra]').setAttribute('aria-checked','true');
                        if(mutation==='missing') document.querySelector('[aria-label=A]').setAttribute('aria-checked','false');
                        if(mutation==='control') root.insertAdjacentHTML('beforeend','<input type="hidden" name="consent" value="Unapproved">');
                        if(mutation==='item') {const item=document.querySelector('[role=listitem]');item.replaceWith(item.cloneNode(true));}
                        if(mutation==='root') {const replacement=root.cloneNode(true);replacement.querySelector('#submit').replaceWith(button);root.replaceWith(replacement);}
                        cover.remove();
                    }, 350);
                }''', mutation)
                boundary = []
                def claim(): boundary.append(True); return True
                submitted = await filling._click_google_form_submit(page, before_click=claim, approval_id=readback['browser_approval_id'])
                assert boundary == [True]
                sent = await page.evaluate('() => window.sent')
                assert len(sent) == (1 if mutation == 'none' else 0)
                assert submitted is (mutation == 'none')
                if sent: assert sent[0]['text'] == 'Approved' and sent[0]['options'] == ['A']
                assert not requests
            finally:
                await browser.close()
    asyncio.run(run())


@pytest.mark.parametrize('mutation', ['before_next_readback', 'during_submit_wait', 'lost_prior_item', 'prior_control', 'add_prior_control', 'none'])
def test_prior_page_approved_values_remain_bound(mutation):
    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                context = await browser.new_context()
                await context.route('**/*', lambda route: route.abort())
                page = await context.new_page()
                await page.set_content('''<form><input type="hidden" name="consent" value="reviewed"><div role="listitem" id="first"><input type="text" name="first"></div>
                    <div role="listitem" id="last" style="display:none"><input type="text" name="last"></div>
                    <button type="submit">Submit</button></form><script>window.sent=[];
                    document.addEventListener('submit',e=>{e.preventDefault();window.sent.push([...new FormData(e.target).entries()])})</script>''')
                page.wait_for_timeout = AsyncMock()
                first = await filling.fill_form(page, [{'index':0,'dom_index':0,'page_index':0,'type':'text'}], [{'index':0,'answer':'First approved'}],approval_owner='workflow')
                assert first['filled'] and not first['skipped']
                await page.evaluate('''mutation => {
                    document.querySelector('#first').style.display='none';document.querySelector('#last').style.display='block';
                    if(mutation==='before_next_readback') document.querySelector('[name=first]').value='Unapproved';
                    if(mutation==='lost_prior_item') document.querySelector('#first').remove();
                    if(mutation==='prior_control') document.querySelector('[name=consent]').value='Unapproved';
                    if(mutation==='add_prior_control') document.querySelector('form').insertAdjacentHTML('beforeend','<input type=hidden name=new_consent value=Unapproved>');
                }''', mutation)
                questions = [{'index':1,'dom_index':0,'page_index':1,'type':'text','required':True}]
                last = await filling.fill_form(page, questions, [{'index':1,'answer':'Last approved'}],approval_owner='workflow')
                ready = filling._google_form_preview_status(questions, last)[0]
                assert ready is (mutation in {'during_submit_wait','none'})
                if ready:
                    await page.evaluate('''mutation => {
                        const cover=document.createElement('div');cover.style='position:fixed;inset:0;z-index:999;background:white';document.body.append(cover);
                        setTimeout(()=>{if(mutation==='during_submit_wait') document.querySelector('[name=first]').value='Unapproved';cover.remove()},350);
                    }''', mutation)
                    assert await filling._click_google_form_submit(page,approval_id=last['browser_approval_id']) is (mutation == 'none')
                assert len(await page.evaluate('() => window.sent')) == (1 if mutation == 'none' else 0)
            finally:
                await browser.close()
    asyncio.run(run())
