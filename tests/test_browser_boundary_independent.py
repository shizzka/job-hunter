"""Independent audit of exact 529d173; only synthetic pages/intercepted traffic."""
import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright
from playwright.async_api import Locator, ElementHandle
import config
import hh_client
from hh import chat
from google_forms import filling
from habr_career_client import HabrCareerClient
from superjob_client import SuperJobClient
from state_store.native_apply import NativeApplyRepository, run_native_attempt

EVIDENCE = Path(os.environ['JH_BOUNDARY_EVIDENCE']) if os.environ.get('JH_BOUNDARY_EVIDENCE') else None
def evidence(name, **values):
    if EVIDENCE is None:
        return
    with EVIDENCE.open('a') as f:
        f.write(json.dumps({'case': name, **values}, ensure_ascii=False) + '\n')

async def browser_case(callback):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            await context.route('**/*', lambda route: route.abort())
            page = await context.new_page()
            page.wait_for_timeout = AsyncMock()
            await callback(context, page)
        finally:
            await browser.close()

@pytest.mark.parametrize('source', ['habr', 'superjob'])
def test_r4_native_actual_submitter_identity(tmp_path, monkeypatch, source):
    monkeypatch.setattr(config, 'HABR_COOKIES_FILE', str(tmp_path/'habr.json'))
    monkeypatch.setattr(config, 'SUPERJOB_AUTH_FILE', str(tmp_path/'sj-auth.json'))
    monkeypatch.setattr(config, 'HH_STATE_DIR', str(tmp_path/'state'))
    async def run(context, page):
        url = 'https://career.habr.com/vacancies/2' if source == 'habr' else 'https://www.superjob.ru/vakansii/qa-2.html'
        initial = '' if source == 'habr' else '<button type="button" class="f-test-vacancy-response-button">Откликнуться</button>'
        html = '''<meta charset="utf-8"><form id="approved" data-vacancy-id="2">
            <input type="hidden" name="vacancy_id" value="2"><textarea name="letter"></textarea>'''+initial+'''
            <button id="final" type="button" class="f-test-button-Otkliknutsya" onclick="this.form.requestSubmit(document.querySelector('#other'))">Откликнуться</button>
            <button id="other" type="submit" name="vacancy_id" value="1" hidden>Other</button>
            </form><script>window.sent=[];document.addEventListener('submit',e=>{
            e.preventDefault();window.sent.push([...new FormData(e.target,e.submitter).entries()]);
            fetch('/quick_responses',{method:'POST',body:new FormData(e.target,e.submitter)});
            });</script>'''
        if source == 'superjob':
            html = html.replace('id="final" type="button"', 'id="final" type="submit"').replace('>Откликнуться</button>\n            <button id="other"', '>Отправить</button>\n            <button id="other"')
            # prevent default after explicit requestSubmit to keep exactly one dispatch
            html = html.replace("this.form.requestSubmit(document.querySelector('#other'))", "this.form.requestSubmit(document.querySelector('#other'));return false")
        posts=[]
        async def route(r):
            if r.request.is_navigation_request():
                await r.fulfill(status=200, content_type='text/html', body=html)
            else:
                posts.append(r.request.url)
                await r.fulfill(status=200, content_type='application/json', body='{}')
        await context.unroute('**/*')
        await context.route('**/*', route)
        client = HabrCareerClient() if source == 'habr' else SuperJobClient()
        client._page=page
        client._page_is_logged_in=AsyncMock(return_value=True)
        result = await client.apply_to_vacancy(url, 'Approved') if source == 'habr' else await client.apply_to_vacancy({'url':url,'external_id':'2'}, 'Approved')
        sent=await page.evaluate('()=>window.sent')
        evidence('r4_actual_submitter_'+source, sent=sent, posts=posts, result=result)
        assert sent == [], 'Unapproved actual submitter must yield zero submit'
    asyncio.run(browser_case(run))

def test_r1_locator_replacement_custom_dispatch():
    async def run(context,page):
        await page.set_content('''<form name="vacancy_response"><input type="hidden" name="resume_id" value="target">
            <textarea name="letter">Approved</textarea><button id="send" type="button" onclick="window.sent.push(document.querySelector('textarea').value)">Apply</button>
            </form><script>window.sent=[]</script>''')
        client=hh_client.HHClient();client._page=page
        client._approved_hh_payload={'resume_id':'target','cover_letter':'Approved','answers':[]}
        locator=page.locator('#send')
        original=ElementHandle.wait_for_element_state
        async def pending(handle,state,**kwargs):
            await page.evaluate('''()=>{const old=document.querySelector('#send');old.disabled=true;
                setTimeout(()=>{const replacement=old.cloneNode(true);replacement.disabled=false;
                old.replaceWith(replacement);document.querySelector('textarea').value='Unapproved'},150)}''')
            return await original(handle,state,**kwargs)
        ElementHandle.wait_for_element_state=pending
        try:
            result=await client._click_with_fallbacks(locator,'submit_button')
        finally:
            ElementHandle.wait_for_element_state=original
        sent=await page.evaluate('()=>window.sent')
        evidence('r1_locator_replacement',sent=sent,result=result)
        assert sent==[], 'Replacement control must not bypass binding'
    asyncio.run(browser_case(run))

def test_r2_parallel_native_sends_one_page():
    async def run(context,page):
        await page.set_content('''<section><textarea data-qa="chatik-new-message-text"></textarea>
            <button data-qa="chatik-do-send-message" onclick="window.sent.push(document.querySelector('textarea').value)">Send</button>
            </section><script>window.sent=[]</script>''')
        first_armed,second_armed,release_first,release_second=[asyncio.Event() for _ in range(4)]
        async def noop(*args,**kwargs):pass
        async def fill(page,chat_id,text):
            return await chat.fill_and_preview(page,chat_id,text,state_dir=os.environ['HOME'],open_page=noop,dismiss_cookies=noop)
        async def messages(*args):
            return {'messages':[{'is_me':True,'text':value} for value in await page.evaluate('()=>window.sent')]}
        async def first_guard(): first_armed.set();await release_first.wait()
        async def second_guard(): second_armed.set();await release_second.wait()
        a=asyncio.create_task(chat.send_message(page,'a','Approved A',fill_preview=fill,extract_current_messages=messages,before_send=first_guard))
        await asyncio.wait_for(first_armed.wait(),10)
        b=asyncio.create_task(chat.send_message(page,'b','Approved B',fill_preview=fill,extract_current_messages=messages,before_send=second_guard))
        await asyncio.wait_for(second_armed.wait(),10)
        release_first.set();ra=await a
        release_second.set();rb=await b
        sent=await page.evaluate('()=>window.sent')
        evidence('r2_concurrent',sent=sent,result_a=ra,result_b=rb)
        assert sent.count('Approved B') <= 1, 'A command must not use B approval and duplicate B'
    asyncio.run(browser_case(run))

@pytest.mark.parametrize('kind',['hh','chat','forms','habr','superjob'])
def test_completed_guard_disarms_unrelated_submit(tmp_path, kind):
    async def run(context,page):
        source_url='https://career.habr.com/vacancies/2' if kind=='habr' else 'https://www.superjob.ru/vakansii/qa-2.html'
        html='''<form id="main" name="vacancy_response" data-vacancy-id="2"><input type="hidden" name="resume_id" value="target">
            <input type="hidden" name="vacancy_id" value="2"><div role="listitem"><textarea name="letter" data-qa="chatik-new-message-text">Approved</textarea></div>
            <button id="send" type="submit" data-qa="chatik-do-send-message">Откликнуться</button></form>
            <form id="unrelated"><input name="setting" value="yes"><button id="save" type="submit">Save</button></form>
            <script>window.events=[];document.addEventListener('submit',e=>{e.preventDefault();window.events.push(e.target.id)});</script>'''
        await context.unroute('**/*')
        await context.route('**/*',lambda r:r.fulfill(status=200,content_type='text/html',body=html))
        await page.goto(source_url if kind in {'habr','superjob'} else 'https://hh.ru/vacancy/2')
        button=await page.query_selector('#send')
        if kind=='forms':
            await button.evaluate("el=>el.innerText='Submit'")
        if kind=='hh':
            client=hh_client.HHClient();client._page=page
            client._approved_hh_payload={'resume_id':'target','cover_letter':'Approved','answers':[]}
            assert await client._click_with_fallbacks(button,'submit_button')
        elif kind=='chat':
            async def fill(*args):return {'filled':True}
            async def messages(*args):return {'messages':[{'is_me':True,'text':'Approved'}]}
            assert await chat.send_message(page,'chat','Approved',fill_preview=fill,extract_current_messages=messages)
        elif kind=='forms':
            readback=await filling.fill_form(page,[{'index':0,'type':'text'}],[{'index':0,'answer':'Approved'}])
            assert readback['filled']
            assert await filling._click_google_form_submit(page,approval_id=readback['browser_approval_id'])
        else:
            client=HabrCareerClient() if kind=='habr' else SuperJobClient();client._page=page
            if kind=='habr':
                client._apply_destination=source_url
                assert await client._click_with_fallbacks(button,'submit_button')
            else:
                assert await client._arm_destination_boundary(button,source_url)
                await button.click();assert await client._destination_boundary_passed()
        await page.locator('#save').click()
        events=await page.evaluate('()=>window.events')
        evidence('disarm_'+kind,events=events)
        assert events==['main','unrelated'], 'Finished action guard must not block unrelated action'
    asyncio.run(browser_case(run))

def test_r1_native_input_button_late_mutation():
    async def run(context,page):
        await page.set_content('''<form name="vacancy_response"><input type="hidden" name="resume_id" value="target">
            <textarea name="letter">Approved</textarea><input id="send" type="button" data-qa="vacancy-response-submit-popup"
            value="Apply" onclick="window.sent.push(document.querySelector('textarea').value)"></form><script>window.sent=[]</script>''')
        client=hh_client.HHClient();client._page=page
        client._approved_hh_payload={'resume_id':'target','cover_letter':'Approved','answers':[]}
        async def guard():
            await page.evaluate('''()=>{const b=document.querySelector('#send');b.disabled=true;
                setTimeout(()=>{document.querySelector('textarea').value='Unapproved';b.disabled=false},150)}''')
            return True
        result=await client._click_with_fallbacks(await page.query_selector('#send'),'submit_button',before_click=guard)
        sent=await page.evaluate('()=>window.sent')
        evidence('r1_native_input_button',sent=sent,result=result)
        assert sent==[], 'Supported data-qa submit input must be capture guarded'
    asyncio.run(browser_case(run))

@pytest.mark.parametrize('source',['hh','habr'])
def test_n1_prior_capture_dispatch_is_not_zero(tmp_path,source):
    async def run(context,page):
        expected='https://hh.ru/vacancy/2' if source=='hh' else 'https://career.habr.com/vacancies/2'
        html='''<form name="vacancy_response" data-vacancy-id="2"><input type="hidden" name="resume_id" value="target">
            <textarea name="letter">Approved</textarea><button type="button" id="send">Откликнуться</button></form>
            <script>window.sent=[];document.addEventListener('click',e=>{
                if(e.target.id==='send'){window.sent.push(document.querySelector('textarea').value);fetch('/apply',{method:'POST'})}
            },true)</script>'''
        posts=[]
        async def route(r):
            if r.request.is_navigation_request():await r.fulfill(status=200,content_type='text/html',body=html)
            else:posts.append(r.request.url);await r.abort()
        await context.unroute('**/*');await context.route('**/*',route);await page.goto(expected)
        client=hh_client.HHClient() if source=='hh' else HabrCareerClient();client._page=page
        if source=='hh':client._approved_hh_payload={'resume_id':'target','cover_letter':'Approved','answers':[]}
        else:client._apply_destination=expected
        repo=NativeApplyRepository(str(tmp_path/source/'cookies.json'),source)
        async def operation():
            if source=='habr':client._external_attempt.begin()
            button=await page.query_selector('#send')
            original=button.wait_for_element_state
            async def pending(state,**kwargs):
                await page.evaluate('''()=>{const b=document.querySelector('#send');b.disabled=true;setTimeout(()=>{
                    document.querySelector('textarea').value='Unapproved';document.querySelector('form').dataset.vacancyId='1';b.disabled=false},150)}''')
                return await original(state,**kwargs)
            button.wait_for_element_state=pending
            return {'ok':await client._click_with_fallbacks(button,'submit_button' if source=='hh' else 'habr_apply_button')}
        result=await run_native_attempt(client,repo,expected,operation)
        await page.evaluate('()=>new Promise(r=>setTimeout(r,30))')
        sent=await page.evaluate('()=>window.sent');status=repo.get(expected)['status']
        retryable=bool(repo.claim(expected,'',''))
        evidence('n1_earlier_capture_'+source,sent=sent,posts=posts,result=result,status=status,retryable=retryable)
        assert sent == [] and posts == [], 'Reject before any earlier page handler/POST'
        assert status == 'failed' and retryable, 'Only pre-dispatch rejection proves recoverable zero command'
    asyncio.run(browser_case(run))

@pytest.mark.parametrize('source',['habr','superjob'])
@pytest.mark.parametrize('path',['locator','handle'])
@pytest.mark.parametrize('destination',['wrong_id','foreign'])
def test_r4_pending_click_document_navigation(source,path,destination):
    async def run(context,page):
        expected='https://career.habr.com/vacancies/2' if source=='habr' else 'https://www.superjob.ru/vakansii/qa-2.html'
        new_url=expected.replace('/2','/1').replace('qa-2','qa-1') if destination=='wrong_id' else 'https://foreign.invalid/vacancies/1'
        html='''<meta charset="utf-8"><section data-vacancy-id="2"><button id="apply" class="f-test-vacancy-response-button" onclick="window.sent.push(location.href)">Откликнуться</button></section><script>window.sent=[]</script>'''
        async def route(r):
            await r.fulfill(status=200,content_type='text/html',body=html.replace('data-vacancy-id="2"','data-vacancy-id="1"') if r.request.url==new_url else html)
        await context.unroute('**/*');await context.route('**/*',route)
        await page.goto(expected)
        client=HabrCareerClient() if source=='habr' else SuperJobClient();client._page=page
        control=page.locator('#apply') if path=='locator' else await page.query_selector('#apply')
        assert await client._arm_destination_boundary(control,expected)
        await control.evaluate('''(button,url)=>{button.disabled=true;setTimeout(()=>location.href=url,150)}''',new_url)
        error=''
        try:await control.click(timeout=2000)
        except Exception as exc:error=type(exc).__name__
        sent=await page.evaluate('()=>window.sent')
        evidence('r4_navigation_'+source+'_'+path+'_'+destination,sent=sent,error=error,readback=await client._destination_boundary_passed())
        assert not sent, 'Navigation must not strip the barrier during a pending locator click'
    asyncio.run(browser_case(run))

@pytest.mark.parametrize('stage',['apply','submit'])
@pytest.mark.parametrize('destination',['wrong_id','foreign'])
def test_r4_superjob_full_method_navigation(tmp_path,monkeypatch,stage,destination):
    monkeypatch.setattr(config,'SUPERJOB_AUTH_FILE',str(tmp_path/'sj-auth.json'))
    monkeypatch.setattr(config,'HH_STATE_DIR',str(tmp_path/'state'))
    async def run(context,page):
        expected='https://www.superjob.ru/vakansii/qa-2.html'
        new_url='https://www.superjob.ru/vakansii/qa-1.html' if destination=='wrong_id' else 'https://foreign.invalid/vakansii/qa-1.html'
        html='''<meta charset="utf-8"><section data-vacancy-id="2"><input type="hidden" name="vacancy_id" value="2">
            <button id="apply" class="f-test-vacancy-response-button" onclick="window.actions.push({stage:this.id,url:location.href})">Откликнуться</button>
            <button id="submit" type="submit" class="f-test-button-Otkliknutsya" onclick="window.actions.push({stage:this.id,url:location.href})">Отправить</button>
            </section><script>window.actions=[]</script>'''
        await context.unroute('**/*')
        async def route(r):
            await r.fulfill(status=200,content_type='text/html',body=html.replace('data-vacancy-id="2"','data-vacancy-id="1"').replace('value="2"','value="1"') if r.request.url==new_url else html)
        await context.route('**/*',route)
        original=ElementHandle.wait_for_element_state
        scheduled=False
        async def pending(control,*args,**kwargs):
            nonlocal scheduled
            if not scheduled and await control.evaluate('el=>el.id')==stage:
                scheduled=True
                await control.evaluate('''(b,url)=>{b.disabled=true;setTimeout(()=>location.href=url,150)}''',new_url)
            return await original(control,*args,**kwargs)
        monkeypatch.setattr(ElementHandle,'wait_for_element_state',pending)
        client=SuperJobClient();client._page=page;client._page_is_logged_in=AsyncMock(return_value=True)
        result=await client.apply_to_vacancy({'url':expected,'external_id':'2'})
        actions=await page.evaluate('()=>window.actions')
        evidence('r4_full_sj_'+stage+'_'+destination,actions=actions,result=result)
        assert not [v for v in actions if v['url']==new_url], 'Full native adapter must yield zero wrong-destination action'
    asyncio.run(browser_case(run))

@pytest.mark.parametrize('source',['habr','superjob'])
@pytest.mark.parametrize('path',['locator','handle'])
@pytest.mark.parametrize('stage',['apply','submit'])
def test_r4_replacement_clicked_control(source,path,stage):
    async def run(context,page):
        expected='https://career.habr.com/vacancies/2' if source=='habr' else 'https://www.superjob.ru/vakansii/qa-2.html'
        klass='f-test-vacancy-response-button' if stage=='apply' else 'f-test-button-Otkliknutsya'
        html='<meta charset="utf-8"><section data-vacancy-id="2"><button id="send" type="submit" class="'+klass+'" onclick="window.sent.push(this.id)">Откликнуться</button></section><script>window.sent=[]</script>'
        await context.unroute('**/*');await context.route('**/*',lambda r:r.fulfill(status=200,content_type='text/html',body=html));await page.goto(expected)
        client=HabrCareerClient() if source=='habr' else SuperJobClient();client._page=page
        control=page.locator('#send') if path=='locator' else await page.query_selector('#send')
        assert await client._arm_destination_boundary(control,expected)
        await page.evaluate('''()=>{const old=document.querySelector('#send');old.disabled=true;setTimeout(()=>{
            const replacement=old.cloneNode(true);replacement.disabled=false;old.replaceWith(replacement)},150)}''')
        error=''
        try:await control.click(timeout=1500)
        except Exception as exc:error=type(exc).__name__
        sent=await page.evaluate('()=>window.sent')
        evidence('r4_replacement_'+source+'_'+path+'_'+stage,sent=sent,error=error)
        assert sent==[]
    asyncio.run(browser_case(run))

@pytest.mark.parametrize('dispatch',['click','dom'])
@pytest.mark.parametrize('mutation',['external','none'])
def test_r1_independent_association_and_fallback(dispatch,mutation):
    async def run(context,page):
        await page.set_content('''<form id="f" name="vacancy_response"><input type="hidden" name="resume_id" value="target">
            <textarea name="letter">Approved</textarea><button id="submit" type="submit">Apply</button></form>
            <input form="f" name="consent" value="reviewed"><script>window.sent=[];document.addEventListener('submit',e=>{
            e.preventDefault();window.sent.push([...new FormData(e.target,e.submitter).entries()])})</script>''')
        client=hh_client.HHClient();client._page=page
        client._approved_hh_payload={'resume_id':'target','cover_letter':'Approved','answers':[]}
        button=await page.query_selector('#submit')
        if dispatch=='click':
            async def guard():
                await page.evaluate('''mutation=>{const b=document.querySelector('#submit');b.disabled=true;setTimeout(()=>{
                if(mutation==='external') document.querySelector('[name=consent]').value='Unapproved';b.disabled=false},150)}''',mutation)
                return True
            result=await client._click_with_fallbacks(button,'submit_button',before_click=guard)
        else:
            result=await client._submit_response_form_via_dom()
        sent=await page.evaluate('()=>window.sent')
        evidence('r1_'+dispatch+'_'+mutation,sent=sent,result=result)
        assert len(sent)==(0 if dispatch=='click' and mutation=='external' else 1)
    asyncio.run(browser_case(run))
