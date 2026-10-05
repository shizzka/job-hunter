"""Behavior at the browser commit boundary; all requests are intercepted."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import hh_client
from hh import chat
from google_forms import filling
from state_store.native_apply import NativeApplyRepository, run_native_attempt
from tests.test_browser_boundary_independent import browser_case


async def synthetic_page(context, page, html):
    posts = []
    await context.unroute('**/*')
    async def route(request):
        if request.request.method == 'POST':
            posts.append(request.request.post_data)
            await request.fulfill(status=200, body='{}')
        else:
            await request.fulfill(status=200, content_type='text/html', body=html)
    await context.route('**/*', route)
    await page.goto('https://hh.ru/vacancy/2')
    return posts


@pytest.mark.parametrize('kind', ['hh', 'chat', 'forms'])
def test_earlier_window_capture_cannot_dispatch_late_payload(kind):
    async def run(context, page):
        html = '''<form id="f" name="vacancy_response"><input type="hidden" name="resume_id" value="target">
            <div role="listitem"><textarea name="letter" data-qa="chatik-new-message-text">Approved</textarea></div>
            <button type="submit" data-qa="chatik-do-send-message">Submit</button></form>
            <script>window.sent=[];window.addEventListener('click',event=>{
              if(event.target.tagName==='BUTTON') {
                const data=new FormData(document.querySelector('form'));
                window.sent.push([...data.entries()]);fetch('/record',{method:'POST',body:data});
              }
            },true);document.addEventListener('submit',e=>e.preventDefault());</script>'''
        posts = await synthetic_page(context, page, html)
        control = await page.query_selector('button')
        original_wait = control.wait_for_element_state
        async def pending(state, **kwargs):
            await page.evaluate("()=>document.querySelector('textarea').value='Unapproved'")
            return await original_wait(state, **kwargs)
        control.wait_for_element_state = pending
        if kind == 'hh':
            client = hh_client.HHClient();client._page = page
            client._approved_hh_payload = {'resume_id':'target','cover_letter':'Approved','answers':[]}
            result = await client._click_with_fallbacks(control, 'submit_button')
        elif kind == 'chat':
            page.query_selector = AsyncMock(return_value=control)
            result = await chat.send_message(page, 'a', 'Approved',
                fill_preview=AsyncMock(return_value={'filled':True}),
                extract_current_messages=AsyncMock(return_value={'messages':[]}))
        else:
            readback = await filling.fill_form(page, [{'index':0,'type':'text'}], [{'index':0,'answer':'Approved'}])
            assert readback['filled']
            # Use the exact element whose auto-wait receives the mutation.
            original_find = filling._find_google_form_button
            async def find(*args):return control
            filling._find_google_form_button = find
            try:
                result = await filling._click_google_form_submit(page,approval_id=readback['browser_approval_id'])
            finally:
                filling._find_google_form_button = original_find
        assert result is False
        assert await page.evaluate('()=>window.sent') == []
        assert posts == []
    asyncio.run(browser_case(run))


@pytest.mark.parametrize('tag', [
    '<input type="button" value="Submit">', '<input type="submit" value="Submit">',
    '<button type="button">Submit</button>', '<button type="submit">Submit</button>',
    '<a href="#">Submit</a>', '<div role="button">Submit</div>',
])
def test_every_hh_clickable_shape_checks_payload_before_handlers(tag):
    async def run(context,page):
        html = '''<form name="vacancy_response"><input type="hidden" name="resume_id" value="target">
          <textarea name="letter">Approved</textarea>'''+tag.replace('>', ' id="send" data-qa="vacancy-response-submit-popup">',1)+'''</form>
          <script>window.sent=[];document.querySelector('#send').onclick=e=>{
            e.preventDefault();const data=new FormData(document.querySelector('form'));
            window.sent.push([...data.entries()]);fetch('/record',{method:'POST',body:data});};</script>'''
        posts = await synthetic_page(context,page,html)
        client=hh_client.HHClient();client._page=page
        client._approved_hh_payload={'resume_id':'target','cover_letter':'Approved','answers':[]}
        control=await page.query_selector('#send');original=control.wait_for_element_state
        async def pending(state, **kwargs):
            await page.evaluate("()=>document.querySelector('textarea').value='Unapproved'")
            return await original(state, **kwargs)
        control.wait_for_element_state=pending
        assert await client._click_with_fallbacks(control,'submit_button') is False
        assert await page.evaluate('()=>window.sent') == []
        assert posts == []
    asyncio.run(browser_case(run))


def test_forms_attempt_cannot_borrow_other_approval():
    async def run(context,page):
        posts=await synthetic_page(context,page,'''<form><div role="listitem"><input type="text" name="entry.1"></div>
          <button type="submit">Submit</button></form><script>window.sent=[];document.addEventListener('submit',e=>{
          e.preventDefault();const data=new FormData(e.target,e.submitter);window.sent.push([...data]);fetch('/record',{method:'POST',body:data})});</script>''')
        questions=[{'index':0,'type':'text'}]
        a=await filling.fill_form(page,questions,[{'index':0,'answer':'Approved A'}],approval_owner='a')
        b=await filling.fill_form(page,questions,[{'index':0,'answer':'Approved B'}],approval_owner='b')
        assert a['browser_approval_id'] != b['browser_approval_id']
        assert await filling._click_google_form_submit(page,approval_id=a['browser_approval_id']) is False
        assert await page.evaluate('()=>window.sent') == []
        assert await filling._click_google_form_submit(page,approval_id=b['browser_approval_id']) is True
        await page.evaluate('()=>new Promise(r=>setTimeout(r,25))')
        assert await page.evaluate('()=>window.sent') == [[['entry.1','Approved B']]]
        assert len(posts) == 1
    asyncio.run(browser_case(run))


@pytest.mark.parametrize('reserved', ['resume_id','letter'])
def test_hh_foreign_actual_submitter_precedes_earlier_submit_handler(tmp_path,reserved):
    async def run(context,page):
        html='''<form name="vacancy_response"><input type="hidden" name="resume_id" value="target">
          <textarea name="letter">Approved</textarea>
          <button id="approved" type="button" onclick="this.form.requestSubmit(document.querySelector('#other'))">Submit</button>
          <button id="other" type="submit" name="RESERVED" value="Unapproved" hidden>Other</button></form>
          <script>window.sent=[];window.addEventListener('submit',e=>{e.preventDefault();
            const data=new FormData(e.target,e.submitter);window.sent.push([...data]);fetch('/record',{method:'POST',body:data});},true);</script>'''.replace('RESERVED',reserved)
        posts=await synthetic_page(context,page,html)
        client=hh_client.HHClient();client._page=page
        client._approved_hh_payload={'resume_id':'target','cover_letter':'Approved','answers':[]}
        repo=NativeApplyRepository(tmp_path,'hh');url='https://hh.ru/vacancy/2'
        async def operation():return {'ok':await client._click_with_fallbacks(await page.query_selector('#approved'),'submit_button')}
        result=await run_native_attempt(client,repo,url,operation)
        assert result['ok'] is False
        assert await page.evaluate('()=>window.sent') == []
        assert posts == []
        # Page JS ran during the approved click: a later blocked submit is not zero-command proof.
        assert repo.get(url)['status'] == 'uncertain'
        assert repo.claim(url,'','') is None
    asyncio.run(browser_case(run))


@pytest.mark.parametrize('failure', ['exception','cancel'])
def test_chat_releases_only_own_listener_when_before_send_terminates(failure):
    async def run(context,page):
        await synthetic_page(context,page,'''<section><textarea data-qa="chatik-new-message-text">Approved</textarea>
          <button data-qa="chatik-do-send-message">Send</button></section>
          <form><button id="save" type="submit">Save</button></form>
          <script>window.saved=0;document.addEventListener('submit',e=>{e.preventDefault();window.saved++});</script>''')
        async def fail():
            if failure=='cancel':raise asyncio.CancelledError()
            raise RuntimeError('Local pre-command rejection')
        expected=asyncio.CancelledError if failure=='cancel' else RuntimeError
        with pytest.raises(expected):
            await chat.send_message(page,'a','Approved',fill_preview=AsyncMock(return_value={'filled':True}),before_send=fail)
        assert await page.evaluate('()=>document.__chatSendApproval') is None
        await page.locator('#save').click()
        assert await page.evaluate('()=>window.saved') == 1
    asyncio.run(browser_case(run))


def test_browser_approval_is_single_use_even_for_overlapping_commands():
    from browser_action_boundary import dispatch_approved
    from hh.submit_boundary import arm_submit_boundary, bind_submit_control
    async def run(context,page):
        posts=await synthetic_page(context,page,'''<form name="vacancy_response"><input type="hidden" name="resume_id" value="target">
            <textarea name="letter">Approved</textarea><button type="submit">Submit</button></form>
            <script>window.sent=[];document.addEventListener('submit',e=>{e.preventDefault();
            const data=new FormData(e.target,e.submitter);window.sent.push([...data]);fetch('/record',{method:'POST',body:data});});</script>''')
        client=hh_client.HHClient();client._page=page
        client._approved_hh_payload={'resume_id':'target','cover_letter':'Approved','answers':[]}
        assert await arm_submit_boundary(client)
        control=await page.query_selector('button')
        assert await bind_submit_control(client,control)
        zero=[]
        results=await asyncio.gather(*[dispatch_approved(page,control,client._submit_boundary_id,
            on_no_action=lambda:zero.append(True)) for _ in range(2)])
        assert sum(results)==1
        assert await page.evaluate('()=>window.sent')==[[['resume_id','target'],['letter','Approved']]]
        await page.evaluate('()=>new Promise(r=>setTimeout(r,25))')
        assert len(posts)==1 and zero==[]
    asyncio.run(browser_case(run))


@pytest.mark.parametrize('explicit',[False,True])
def test_forms_missing_own_plan_does_not_use_current_other_plan(explicit):
    async def run(context,page):
        await synthetic_page(context,page,'''<form><div role="listitem"><input type="text" name="entry.1"></div>
            <button type="submit">Submit</button></form><script>window.sent=0;
            document.addEventListener('submit',e=>{e.preventDefault();window.sent++});</script>''')
        result=await filling.fill_form(page,[{'index':0,'type':'text'}],[{'index':0,'answer':'Other approved'}],approval_owner='other')
        assert result['browser_approval_id']
        submitted=await filling._click_google_form_submit(page,**({'approval_id':None} if explicit else {}))
        assert await page.evaluate('()=>window.sent')==0
        assert submitted is False
    asyncio.run(browser_case(run))


@pytest.mark.parametrize('kind',['hh','forms'])
def test_document_start_scope_checks_implicit_actual_submit_before_page_handler(kind):
    from browser_action_boundary import bootstrap_boundary
    async def run(context,page):
        await bootstrap_boundary(page)
        posts=await synthetic_page(context,page,'''<form name="vacancy_response"><input type="hidden" name="resume_id" value="target">
          <div role="listitem"><textarea name="letter">Approved</textarea></div>
          <button type="submit" onclick="document.querySelector('textarea').value='Unapproved'">Submit</button></form>
          <script>window.sent=[];window.addEventListener('submit',e=>{e.preventDefault();const data=new FormData(e.target,e.submitter);
          window.sent.push([...data]);fetch('/record',{method:'POST',body:data});},true);</script>''')
        if kind=='hh':
            client=hh_client.HHClient();client._page=page
            client._approved_hh_payload={'resume_id':'target','cover_letter':'Approved','answers':[]}
            result=await client._click_with_fallbacks(await page.query_selector('button'),'submit_button')
        else:
            readback=await filling.fill_form(page,[{'index':0,'type':'text'}],[{'index':0,'answer':'Approved'}])
            result=await filling._click_google_form_submit(page,approval_id=readback['browser_approval_id'])
        assert result is False
        assert await page.evaluate('()=>window.sent')==[] and posts==[]
        # Terminal scope is off: the earlier trampoline does not block unrelated form handlers.
        await page.evaluate('''()=>{document.body.insertAdjacentHTML('beforeend','<form id="other"><button type="submit">Save</button></form>');
            document.querySelector('#other').requestSubmit(document.querySelector('#other button'));}''')
        assert len(await page.evaluate('()=>window.sent'))==1
    asyncio.run(browser_case(run))


@pytest.mark.parametrize('cancel',[False,True])
def test_no_pointer_preparation_failure_is_proven_zero(tmp_path,cancel):
    from playwright.async_api import Error
    async def run(context,page):
        posts=await synthetic_page(context,page,'''<form name="vacancy_response"><input type="hidden" name="resume_id" value="target">
            <textarea name="letter">Approved</textarea><button type="submit">Submit</button></form>
            <script>window.sent=0;document.addEventListener('submit',e=>{e.preventDefault();window.sent++;fetch('/record',{method:'POST'})});</script>''')
        client=hh_client.HHClient();client._page=page
        client._approved_hh_payload={'resume_id':'target','cover_letter':'Approved','answers':[]}
        control=await page.query_selector('button')
        async def fail(*args,**kwargs):
            if cancel:raise asyncio.CancelledError()
            raise Error('Actionability wait lost its document')
        control.wait_for_element_state=fail
        repo=NativeApplyRepository(tmp_path/'cookies.json','hh');url='https://hh.ru/vacancy/2'
        async def operation():return {'ok':await client._click_with_fallbacks(control,'submit_button')}
        if cancel:
            with pytest.raises(asyncio.CancelledError):await run_native_attempt(client,repo,url,operation)
        else:
            result=await run_native_attempt(client,repo,url,operation)
            assert result.get('uncertain') is False
        assert await page.evaluate('()=>window.sent')==0 and posts==[]
        assert repo.get(url)['status']=='failed' and repo.claim(url,'','')
    asyncio.run(browser_case(run))



def test_terminal_fence_is_removed_from_future_documents():
    from browser_action_boundary import bootstrap_boundary
    async def run(context,page):
        await bootstrap_boundary(page)
        html='''<form name="vacancy_response"><input type="hidden" name="resume_id" value="target">
            <textarea name="letter">Approved</textarea><button type="submit">Submit</button></form>
            <script>window.sent=0;document.addEventListener('submit',e=>{e.preventDefault();window.sent++});</script>'''
        await synthetic_page(context,page,html)
        client=hh_client.HHClient();client._page=page
        client._approved_hh_payload={'resume_id':'target','cover_letter':'Approved','answers':[]}
        assert await client._click_with_fallbacks(await page.query_selector('button'),'submit_button')
        assert not page._jh_boundary_fences
        await page.goto('https://hh.ru/vacancy/3')
        await page.evaluate("()=>document.querySelector('button').click()")
        assert await page.evaluate('()=>window.sent')==1
    asyncio.run(browser_case(run))


def test_releasing_one_fence_preserves_other_attempt_after_navigation():
    from browser_action_boundary import bootstrap_boundary, release_boundary
    from hh.submit_boundary import arm_submit_boundary
    async def run(context,page):
        await bootstrap_boundary(page)
        await synthetic_page(context,page,'''<form name="vacancy_response"><input type="hidden" name="resume_id" value="target">
            <textarea name="letter">Approved</textarea><button type="submit">Submit</button></form>
            <script>window.sent=0;document.addEventListener('submit',e=>{e.preventDefault();window.sent++});</script>''')
        clients=[hh_client.HHClient(),hh_client.HHClient()]
        for client in clients:
            client._page=page
            client._approved_hh_payload={'resume_id':'target','cover_letter':'Approved','answers':[]}
            assert await arm_submit_boundary(client)
        a,b=[client._submit_boundary_id for client in clients]
        await page.goto('https://hh.ru/vacancy/3')
        await release_boundary(page,a)
        await page.evaluate("()=>document.querySelector('button').click()")
        assert await page.evaluate('()=>window.sent')==0
        assert set(page._jh_boundary_fences)=={b}
        await release_boundary(page,b)
        await page.evaluate("()=>document.querySelector('button').click()")
        assert await page.evaluate('()=>window.sent')==1
        assert not page._jh_boundary_fences
    asyncio.run(browser_case(run))



def test_formdata_readback_is_inert_before_rejected_command():
    async def run(context,page):
        posts=await synthetic_page(context,page,'''<form name="vacancy_response"><input type="hidden" name="resume_id" value="target">
            <textarea name="letter">Approved</textarea><button type="submit">Submit</button></form>
            <script>window.sent=[];document.addEventListener('formdata',e=>{
            window.sent.push([...e.formData]);fetch('/record',{method:'POST',body:JSON.stringify([...e.formData])});});</script>''')
        client=hh_client.HHClient();client._page=page
        client._approved_hh_payload={'resume_id':'target','cover_letter':'Approved','answers':[]}
        control=await page.query_selector('button');original=control.wait_for_element_state
        async def late(state,**kwargs):
            await page.evaluate("()=>document.querySelector('textarea').value='Unapproved'")
            return await original(state,**kwargs)
        control.wait_for_element_state=late
        assert await client._click_with_fallbacks(control,'submit_button') is False
        assert await page.evaluate('()=>window.sent')==[] and posts==[]
    asyncio.run(browser_case(run))



def test_file_control_cannot_change_bytes_under_identical_metadata():
    async def run(context,page):
        posts=await synthetic_page(context,page,'''<form name="vacancy_response"><input type="hidden" name="resume_id" value="target">
            <textarea name="letter">Approved</textarea><input type="file" name="attachment"><button type="submit">Submit</button></form>
            <script>window.sent=[];document.addEventListener('submit',async e=>{e.preventDefault();const data=new FormData(e.target,e.submitter);window.sent.push(await data.get('attachment').text());fetch('/record',{method:'POST',body:data})});</script>''')
        async def file(value):
            await page.evaluate('''value=>{const transfer=new DataTransfer();transfer.items.add(new File([value],'synthetic.txt',{type:'text/plain',lastModified:7}));
                document.querySelector('[type=file]').files=transfer.files;}''',value)
        await file('A')
        client=hh_client.HHClient();client._page=page
        client._approved_hh_payload={'resume_id':'target','cover_letter':'Approved','answers':[]}
        control=await page.query_selector('button');original=control.wait_for_element_state
        async def late(state,**kwargs):
            await file('B')
            return await original(state,**kwargs)
        control.wait_for_element_state=late
        result=await client._click_with_fallbacks(control,'submit_button')
        if result:
            await page.wait_for_function('window.sent.length>0',timeout=1000)
        assert await page.evaluate('()=>window.sent')==[] and posts==[]
        assert result is False
    asyncio.run(browser_case(run))



@pytest.mark.parametrize('mutate',[False,True])
@pytest.mark.parametrize('control_type',['button','submit'])
def test_hh_dialog_native_form_owns_all_successful_controls(mutate,control_type):
    async def run(context,page):
        posts=await synthetic_page(context,page,'''<div role="dialog"><form id="actual"><input type="hidden" name="resume_id" value="target">
            <textarea name="letter">Approved</textarea><input id="send" type="TYPE" data-qa="vacancy-response-submit-popup" value="Submit"></form></div>
            <input id="external" type="hidden" name="consent" value="reviewed" form="actual">
            <script>window.sent=[];const record=form=>{const data=new FormData(form);window.sent.push([...data]);fetch('/record',{method:'POST',body:data})};
            document.querySelector('#send').onclick=e=>{if(e.target.type==='button')record(e.target.form)};
            document.addEventListener('submit',e=>{e.preventDefault();record(e.target)});</script>'''.replace('TYPE',control_type))
        client=hh_client.HHClient();client._page=page
        client._approved_hh_payload={'resume_id':'target','cover_letter':'Approved','answers':[]}
        control=await page.query_selector('#send');original=control.wait_for_element_state
        async def late(state,**kwargs):
            if mutate:
                await page.evaluate("()=>document.querySelector('#external').value='Unapproved'")
            return await original(state,**kwargs)
        control.wait_for_element_state=late
        from hh.submit_boundary import arm_submit_boundary, bind_submit_control
        from browser_action_boundary import dispatch_approved
        assert await arm_submit_boundary(client)
        assert await bind_submit_control(client,control)
        result=await dispatch_approved(page,control,client._submit_boundary_id)
        sent=await page.evaluate('()=>window.sent')
        assert len(sent)==(0 if mutate else 1)
        assert result is (not mutate)
        if sent:assert sent[0]==[['resume_id','target'],['letter','Approved'],['consent','reviewed']]
        if mutate:assert posts==[]
    asyncio.run(browser_case(run))
