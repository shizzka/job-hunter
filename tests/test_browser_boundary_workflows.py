"""Independent complete approved workflows; native fill/boundary/click/state."""
import asyncio
import copy
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
import hh_client
import hh_chat_responder as cr
import google_form_filler as gf
from google_forms import filling
from google_forms.extraction import extract_form_questions
from state_store.google_forms import GoogleFormStateRepository
from runtime_context import RuntimePaths
from tests.test_browser_boundary_independent import browser_case, evidence
import notifier
import aiohttp
from aiohttp import web

def chat_setup(monkeypatch,page,tmp_path):
    paths=RuntimePaths(str(tmp_path),str(tmp_path/'state'),str(tmp_path/'resume.md'))
    message={'id':'m1','text':'Synthetic question','author':'Robot','is_ai':True,'is_me':False}
    data={'messages':[message],'vacancy':{'title':'Synthetic QA','company':'Example'}}
    async def messages(*args,**kwargs):return copy.deepcopy(data)
    async def open_chat(page,url,*args,**kwargs):
        await page.evaluate('url=>history.replaceState({},"",url)',url)
    monkeypatch.setattr(cr,'get_messages',messages)
    monkeypatch.setattr(cr,'_extract_messages',messages)
    monkeypatch.setattr(cr,'_active_profile_name',lambda:'synthetic')
    monkeypatch.setattr(hh_client,'_load_resume_text',lambda:'Synthetic resume')
    monkeypatch.setattr(cr,'_open_chatik_page',open_chat)
    monkeypatch.setattr(cr,'_dismiss_cookies_banner',AsyncMock())
    page.goto=AsyncMock()
    return paths,SimpleNamespace(_page=page),cr._state_repository(paths)

async def install_chat(context,page):
    html='''<section><textarea data-qa="chatik-new-message-text"></textarea>
        <button data-qa="chatik-do-send-message" onclick="window.sent.push({chat:location.pathname,text:document.querySelector('textarea').value})">Send</button>
        </section><script>window.sent=[]</script>'''
    await context.unroute('**/*');await context.route('**/*',lambda r:r.fulfill(status=200,content_type='text/html',body=html))
    await page.goto('https://chatik.hh.ru/chat/synthetic')

@pytest.mark.parametrize('mutation',['text','prior_capture','none'])
def test_stored_approved_chat_real_send(tmp_path,monkeypatch,mutation):
    async def run(context,page):
        await install_chat(context,page)
        paths,client,repo=chat_setup(monkeypatch,page,tmp_path)
        monkeypatch.setattr(cr,'generate_answer',AsyncMock(return_value='Approved A'))
        preview=await cr.process_one(client,'a',message_id='m1',dry_run=True,runtime_paths=paths)
        assert preview['ok']
        if mutation=='prior_capture':
            await page.evaluate('''()=>document.addEventListener('click',e=>{
                if(e.target.matches('[data-qa="chatik-do-send-message"]'))window.sent.push({chat:location.pathname,text:document.querySelector('textarea').value})
            },true)''')
        generator=AsyncMock(side_effect=AssertionError('Approved send must not regenerate'))
        monkeypatch.setattr(cr,'generate_answer',generator)
        original=cr._chat_send_message
        async def native(page,chat_id,text,**kwargs):
            guard=kwargs['before_send']
            async def guarded():
                await guard()
                await page.evaluate('''mutation=>{const b=document.querySelector('button');b.disabled=true;setTimeout(()=>{
                    if(mutation!=='none')document.querySelector('textarea').value='Unapproved B';b.disabled=false},150)}''',mutation)
            kwargs['before_send']=guarded
            # Verify through actual handler output, not the Python sender argument.
            async def messages(*args):
                return {'messages':[{'is_me':True,'text':v['text']} for v in await page.evaluate('()=>window.sent')]}
            kwargs['extract_current_messages']=messages
            return await original(page,chat_id,text,**kwargs)
        monkeypatch.setattr(cr,'_chat_send_message',native)
        result=await cr.process_one(client,'a',message_id='m1~'+preview['draft_revision'],dry_run=False,runtime_paths=paths)
        sent=await page.evaluate('()=>window.sent')
        state=repo.load()['a'];statuses={v['kind']+':'+v['key']:v['status'] for v in state['attempts'].values()}
        evidence('full_stored_chat_'+mutation,sent=sent,result=result,statuses=statuses)
        assert sent==([] if mutation!='none' else [{'chat':'/chat/a','text':'Approved A'}])
        assert result['ok'] is (mutation=='none')
        assert statuses['reply:m1']==('failed' if mutation!='none' else 'completed')
        generator.assert_not_awaited()
    asyncio.run(browser_case(run))

def test_two_stored_chat_workflows_same_browser(tmp_path,monkeypatch):
    async def run(context,page):
        await install_chat(context,page)
        paths,client,repo=chat_setup(monkeypatch,page,tmp_path)
        drafts={}
        for cid in ['a','b']:
            monkeypatch.setattr(cr,'generate_answer',AsyncMock(return_value='Approved '+cid.upper()))
            drafts[cid]=await cr.process_one(client,cid,message_id='m1',dry_run=True,runtime_paths=paths)
            assert drafts[cid]['ok']
        monkeypatch.setattr(cr,'generate_answer',AsyncMock(side_effect=AssertionError('No regeneration')))
        entered={cid:asyncio.Event() for cid in ['a','b']};release={cid:asyncio.Event() for cid in ['a','b']}
        original=cr._chat_send_message
        async def native(page,cid,text,**kwargs):
            guard=kwargs['before_send']
            async def guarded():await guard();entered[cid].set();await release[cid].wait()
            kwargs['before_send']=guarded
            async def messages(*args):
                return {'messages':[{'is_me':True,'text':v['text']} for v in await page.evaluate('()=>window.sent')]}
            kwargs['extract_current_messages']=messages
            return await original(page,cid,text,**kwargs)
        monkeypatch.setattr(cr,'_chat_send_message',native)
        async def send(cid):
            return await cr.process_one(client,cid,message_id='m1~'+drafts[cid]['draft_revision'],dry_run=False,runtime_paths=paths)
        a=asyncio.create_task(send('a'));await asyncio.wait_for(entered['a'].wait(),15)
        b=asyncio.create_task(send('b'));await asyncio.wait_for(entered['b'].wait(),15)
        release['a'].set();ra=await a;release['b'].set();rb=await b
        sent=await page.evaluate('()=>window.sent')
        statuses={cid:next(v['status'] for v in repo.load()[cid]['attempts'].values() if v['kind']=='reply') for cid in ['a','b']}
        evidence('full_stored_chat_concurrent',sent=sent,result_a=ra,result_b=rb,statuses=statuses)
        assert len([v for v in sent if v['chat']=='/chat/b'])<=1, 'A approval cannot dispatch B a second time'
    asyncio.run(browser_case(run))

@pytest.mark.parametrize('mutation',['text','root','prior_capture','none'])
def test_saved_forms_full_native_workflow(tmp_path,monkeypatch,mutation):
    async def run(context,page):
        url='https://docs.google.com/forms/d/e/synthetic/viewform'
        html='''<form id="f"><div role="listitem"><div role="heading">Synthetic name</div><input type="text" name="entry.1" required></div>
            <button type="submit">Submit</button></form><script>window.sent=[];document.addEventListener('submit',e=>{
            e.preventDefault();window.sent.push([...new FormData(e.target,e.submitter).entries()]);document.body.insertAdjacentHTML('beforeend','<p>Your response has been recorded</p>')});
            document.querySelector('input').addEventListener('input',()=>{const cover=document.createElement('div');cover.style='position:fixed;inset:0;z-index:999;background:white';document.body.append(cover);
            setTimeout(()=>{MUTATION;cover.remove()},350)},{once:true});</script>'''.replace('MUTATION',{'text':"document.querySelector('input').value='Unapproved'",'prior_capture':"document.querySelector('input').value='Unapproved'",'root':"const root=document.querySelector('form'),b=document.querySelector('button'),replacement=root.cloneNode(true);replacement.querySelector('button').replaceWith(b);root.replaceWith(replacement)",'none':''}[mutation])
        if mutation=='prior_capture':
            html+='''<script>document.addEventListener('click',e=>{if(e.target.tagName==='BUTTON'){
                window.sent.push([...new FormData(document.querySelector('form')).entries()]);fetch('/record',{method:'POST',body:new FormData(document.querySelector('form'))})}},true)</script>'''
        await context.unroute('**/*');await context.route('**/*',lambda r:r.fulfill(status=200,content_type='text/html',body=html))
        # Native extraction supplies the saved question topology, not a fake success.
        await page.goto(url);questions=await extract_form_questions(page)
        assert len(questions)==1
        questions[0].update(page_index=0,page_question_index=0)
        paths=RuntimePaths(str(tmp_path),str(tmp_path/'state'),str(tmp_path/'resume.md'))
        repo=GoogleFormStateRepository(tmp_path);token='abcdef123456'
        repo.remember(token,{'token':token,'status':'preview','ok':True,'form_url':url,'questions':questions,
            'answers':[{'index':0,'answer':'Approved','confidence':'high'}],'fill_result':{'filled':[{'index':0}],'skipped':[]},'pages_total':1},trim_expired=False)
        result=await gf.submit_saved_preview(SimpleNamespace(_page=page),token,runtime_paths=paths)
        sent=await page.evaluate('()=>window.sent');status=repo.load()['items'][token]['status']
        evidence('full_saved_forms_'+mutation,sent=sent,result=result,status=status)
        assert sent==([] if mutation!='none' else [[['entry.1','Approved']]])
        assert result['ok'] is (mutation=='none')
        assert status==('submitted' if mutation=='none' else 'submit_failed')
    asyncio.run(browser_case(run))

@pytest.mark.parametrize('oversize',['raw','html'])
@pytest.mark.parametrize('screenshot',[False,True])
def test_n2_independent_local_reject_then_alternative(tmp_path,monkeypatch,oversize,screenshot):
    async def run(context,page):
        await install_chat(context,page)
        paths,client,repo=chat_setup(monkeypatch,page,tmp_path)
        answers=iter(['x'*5000 if oversize=='raw' else '<'*1100,'Short alternative'])
        async def generate(*args,**kwargs):return next(answers)
        monkeypatch.setattr(cr,'generate_answer',generate)
        original=cr.fill_and_preview
        async def fill(*args,**kwargs):
            result=await original(*args,**kwargs)
            if not screenshot:result['screenshot_path']=''
            return result
        monkeypatch.setattr(cr,'fill_and_preview',fill)
        deliveries=[]
        async def notify(*args,**kwargs):deliveries.append(True);return True
        monkeypatch.setattr(notifier,'send_photo',notify)
        monkeypatch.setattr(notifier,'send_message_with_markup',notify)
        rejected=await cr.process_one(client,'a',message_id='m1',dry_run=True,notify=True,runtime_paths=paths)
        assert rejected.get('notification_rejected') and deliveries==[]
        status=next(v['status'] for v in repo.load()['a']['attempts'].values())
        alternative=await cr.process_one(client,'a',message_id='m1',dry_run=True,alternative=True,notify=True,runtime_paths=paths)
        evidence('n2_local_'+oversize+'_'+str(screenshot),rejected=rejected.get('notification_rejected'),status=status,alternative_ok=alternative['ok'],deliveries=len(deliveries))
        assert status=='failed' and alternative['ok'] and len(deliveries)==1
    asyncio.run(browser_case(run))
