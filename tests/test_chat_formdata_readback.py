"""X1 regression: native chat validation must never dispatch page formdata senders.

All Chromium requests are fulfilled locally. Stored workflow uses synthetic
model/history data while native fill, approval, dispatch and durable state run.
"""
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright
from browser_action_boundary import bootstrap_boundary, install_boundary, release_boundary, dispatch_approved
from hh import chat
import hh_chat_responder as cr
import hh_client
from runtime_context import RuntimePaths
from tests.test_browser_boundary_independent import evidence as observe

async def browser(html, callback, url='https://chatik.hh.ru/chat/a'):
    async with async_playwright() as pw:
        instance=await pw.chromium.launch(headless=True)
        try:
            context=await instance.new_context()
            posts=[]
            async def offline(route):
                if route.request.method=='POST':
                    posts.append({'url':route.request.url,'body':route.request.post_data})
                    await route.fulfill(status=200,content_type='application/json',body='{"ok":true}')
                else:
                    await route.fulfill(status=200,content_type='text/html; charset=utf-8',body=html)
            await context.route('**/*',offline)
            page=await context.new_page()
            await bootstrap_boundary(page)
            await page.goto(url)
            await callback(context,page,posts)
        finally:
            await instance.close()

CHAT_FORM='''<form id="composer"><textarea name="message" data-qa="chatik-new-message-text">Approved A</textarea>
<button type="button" data-qa="chatik-do-send-message">Send</button></form>
<script>window.fdActions=[];window.clicked=[];
document.querySelector('form').addEventListener('formdata',e=>{
 const entries=[...e.formData];window.fdActions.push(entries);
 fetch('/record',{method:'POST',body:JSON.stringify(entries)});
});document.querySelector('button').onclick=()=>window.clicked.push(document.querySelector('textarea').value);
</script>'''

def test_chat_approval_readback_must_not_dispatch_formdata_handler():
    async def run(context,page,posts):
        control=await page.query_selector('button')
        await install_boundary(page,'independent-readback')
        try:
            armed=await chat.arm_send_boundary(control,'Approved A',boundary_id='independent-readback')
            await asyncio.sleep(.1)
            handlers=await page.evaluate('()=>({fd:window.fdActions,clicked:window.clicked})')
            observe('chat_arm_readback',armed=armed,handlers=handlers,posts=posts)
            assert armed is True
            assert handlers['fd']==[] and posts==[], 'Approval readback dispatched page formdata sender before action command'
        finally:
            await release_boundary(page,'independent-readback')
    asyncio.run(browser(CHAT_FORM,run))

@pytest.mark.parametrize('scenario', ['unchanged', 'late_mutation', 'formdata_transform'])
def test_full_stored_chat_readback_does_not_send_or_make_delivery_retryable(tmp_path,monkeypatch,scenario):
    late_mutation = scenario != 'unchanged'
    html = CHAT_FORM
    if scenario == 'formdata_transform':
        html = html.replace(' const entries=[...e.formData];',
            " document.querySelector('textarea').value='Unapproved B';e.formData.set('message','Unapproved B');const entries=[...e.formData];")
    async def run(context,page,posts):
        page.wait_for_timeout=AsyncMock()
        page.goto=AsyncMock()
        paths=RuntimePaths(str(tmp_path),str(tmp_path/'state'),str(tmp_path/'resume.md'))
        client=SimpleNamespace(_page=page)
        message={'id':'m1','text':'Synthetic question','author':'Robot','is_ai':True,'is_me':False}
        data={'messages':[message],'vacancy':{'title':'Synthetic QA','company':'Example'}}
        async def messages(*args,**kwargs):return copy.deepcopy(data)
        async def open_chat(page,url,*args,**kwargs):await page.evaluate('url=>history.replaceState({},"",url)',url)
        monkeypatch.setattr(cr,'get_messages',messages)
        monkeypatch.setattr(cr,'_extract_messages',messages)
        monkeypatch.setattr(cr,'_active_profile_name',lambda:'synthetic')
        monkeypatch.setattr(hh_client,'_load_resume_text',lambda:'Synthetic resume')
        monkeypatch.setattr(cr,'_open_chatik_page',open_chat)
        monkeypatch.setattr(cr,'_dismiss_cookies_banner',AsyncMock())
        monkeypatch.setattr(cr,'generate_answer',AsyncMock(return_value='Approved A'))
        preview=await cr.process_one(client,'a',message_id='m1',dry_run=True,runtime_paths=paths)
        assert preview['ok']
        monkeypatch.setattr(cr,'generate_answer',AsyncMock(side_effect=AssertionError('Do not regenerate approved draft')))
        native=cr._chat_send_message
        async def send(page,cid,text,**kwargs):
            before=kwargs['before_send']
            async def guarded():
                await before()
                if late_mutation:
                    await page.evaluate('''()=>{const button=document.querySelector('button');button.disabled=true;
                    setTimeout(()=>{document.querySelector('textarea').value='Unapproved B';button.disabled=false},150)}''')
            async def sent_messages(*args):
                return {'messages':[{'is_me':True,'text':text} for text in await page.evaluate('()=>window.clicked')]}
            kwargs.update(before_send=guarded,extract_current_messages=sent_messages)
            return await native(page,cid,text,**kwargs)
        monkeypatch.setattr(cr,'_chat_send_message',send)
        bound='m1~'+preview['draft_revision']
        result=await cr.process_one(client,'a',message_id=bound,dry_run=False,runtime_paths=paths)
        await asyncio.sleep(.1)
        repository=cr._state_repository(paths)
        status=next(attempt['status'] for attempt in repository.load()['a']['attempts'].values() if attempt['kind']=='reply')
        first_posts=len(posts)
        handlers=await page.evaluate('()=>({fd:window.fdActions,clicked:window.clicked})')
        replay=None
        if late_mutation:
            replay=await cr.process_one(client,'a',message_id=bound,dry_run=False,runtime_paths=paths)
            await asyncio.sleep(.1)
        observe('full_stored_chat_readback_'+scenario,result=result,status=status,handlers=handlers,first_posts=first_posts,total_posts=len(posts),replay=replay,posts=posts)
        assert handlers['fd']==[], 'Native approved send snapshot executed extra FormData send handlers'
        if late_mutation:
            assert posts==[] and handlers['clicked']==[]
            assert result['ok'] is False
            assert status == 'failed'
            assert replay['ok'] is False
        else:
            assert handlers['clicked']==['Approved A'] and result['ok'] is True
            assert status == 'completed'
            assert posts == []
    asyncio.run(browser(html,run))


def test_native_chat_formdata_sender_runs_once_only_after_dispatch():
    html = CHAT_FORM.replace(
        "document.querySelector('button').onclick=()=>window.clicked.push(document.querySelector('textarea').value);",
        "document.querySelector('button').onclick=()=>{window.clicked.push(document.querySelector('textarea').value);new FormData(document.querySelector('form'))};")
    async def run(context, page, posts):
        control = await page.query_selector('button')
        owner = 'chat-native-formdata'
        await install_boundary(page, owner)
        try:
            assert await chat.arm_send_boundary(control, 'Approved A', boundary_id=owner)
            assert await page.evaluate('()=>window.fdActions') == []
            assert posts == []
            assert await dispatch_approved(page, control, owner)
            await asyncio.sleep(.1)
            assert await page.evaluate('()=>window.clicked') == ['Approved A']
            assert await page.evaluate('()=>window.fdActions') == [[['message', 'Approved A']]]
            assert len(posts) == 1
            assert posts[0]['body'] == '[["message","Approved A"]]'
            assert not await dispatch_approved(page, control, owner)
            assert len(posts) == 1
        finally:
            await release_boundary(page, owner)
    asyncio.run(browser(html, run))
