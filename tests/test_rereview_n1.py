"""Proven zero browser dispatch must remain retryable; Chromium is offline."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

import hh_client
import hh_chat_responder as cr
import google_form_filler as gforms
from hh import chat
from google_forms import filling
from state_store.native_apply import NativeApplyRepository, run_native_attempt
from habr_career_client import HabrCareerClient
from tests.test_chat_workflow_transactions import workflow, attempt_status
from tests.test_async_form_workflow import flow, TOKEN


@pytest.mark.parametrize('kind', ['dom_unavailable', 'guard_blocked'])
def test_hh_confirmed_zero_dispatch_is_failed_and_retryable(tmp_path, monkeypatch, kind):
    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                context = await browser.new_context()
                await context.route('**/*', lambda route: route.abort())
                page = await context.new_page()
                await page.set_content('''<form name="vacancy_response"><input type="hidden" name="resume_id" value="target">
                    <textarea name="letter">Approved</textarea><button type="submit">Apply</button></form><script>window.sent=0;
                    document.addEventListener('submit',e=>{e.preventDefault();window.sent++})</script>''')
                page.wait_for_timeout = AsyncMock()
                client = hh_client.HHClient()
                client._page = page
                client._approved_hh_payload = {'resume_id':'target','cover_letter':'Approved','answers':[]}
                if kind == 'dom_unavailable':
                    await page.evaluate('() => document.querySelector("form").requestSubmit=undefined')
                async def operation():
                    if kind == 'dom_unavailable':
                        ok = await client._submit_response_form_via_dom()
                    else:
                        await page.evaluate('''() => {const button=document.querySelector('button');button.disabled=true;
                            setTimeout(()=>{document.querySelector('textarea').value='Unapproved';button.disabled=false},150)}''')
                        ok = await client._click_with_fallbacks(await page.query_selector('button'), 'submit_button')
                    return {'ok':ok}
                import manual_apply_queue as queue
                monkeypatch.setattr(queue,'_queue_path',lambda profile_name=None:tmp_path/'queue.json')
                token=queue.create_candidate({'source':'hh','id':'2','url':'https://hh.ru/vacancy/2'},{'score':55})['token']
                owner=queue.claim_candidate(token)['owner']
                client._manual_apply_guard=lambda:queue.begin_external(token,owner)
                client._manual_apply_no_action=lambda:queue.confirm_no_action(token,owner)
                repo = NativeApplyRepository(str(tmp_path/'hh.json'), 'hh')
                result = await run_native_attempt(client, repo, 'https://hh.ru/vacancy/2', operation)
                assert await page.evaluate('() => window.sent') == 0
                assert repo.get('https://hh.ru/vacancy/2')['status'] == 'failed'
                assert not result.get('uncertain')
                assert repo.claim('https://hh.ru/vacancy/2','','')
                assert not queue.get_candidate(token)['external_started']
                assert queue.finish_candidate(token,owner,'pending')
                assert queue.claim_candidate(token)
            finally:
                await browser.close()
    asyncio.run(run())


def test_habr_blocked_click_has_retryable_owned_result(tmp_path):
    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                context = await browser.new_context()
                await context.route('**/*', lambda route: route.fulfill(status=200, content_type='text/html', body='''<section data-vacancy-id="2"><button onclick="window.sent++">Apply</button></section><script>window.sent=0</script>'''))
                page = await context.new_page()
                await page.goto('https://career.habr.com/vacancies/2')
                page.wait_for_timeout = AsyncMock()
                client = HabrCareerClient(); client._page = page
                client._apply_destination = page.url
                repo = NativeApplyRepository(str(tmp_path/'habr.json'), 'habr')
                async def operation():
                    await page.evaluate('''() => {const button=document.querySelector('button');button.disabled=true;
                        setTimeout(()=>{document.querySelector('section').dataset.vacancyId='1';button.disabled=false},150)}''')
                    return {'ok':await client._click_with_fallbacks(await page.query_selector('button'),'submit_button')}
                await run_native_attempt(client, repo, 'https://career.habr.com/vacancies/2', operation)
                assert await page.evaluate('() => window.sent') == 0
                assert repo.get('https://career.habr.com/vacancies/2')['status'] == 'failed'
                assert repo.claim('https://career.habr.com/vacancies/2','','')
            finally:
                await browser.close()
    asyncio.run(run())


def test_chat_owned_zero_send_does_not_poison_next_preview(workflow, monkeypatch):
    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                context = await browser.new_context()
                await context.route('**/*', lambda route: route.abort())
                page = await context.new_page()
                await page.set_content('''<section><textarea data-qa="chatik-new-message-text"></textarea><button data-qa="chatik-do-send-message" onclick="window.sent++">Send</button></section><script>window.sent=0</script>''')
                page.goto = AsyncMock(); page.wait_for_timeout = AsyncMock()
                workflow.client._page = page
                async def fill(page, _, text):
                    await page.locator('textarea').fill(text)
                    return {'filled':True}
                async def send(page, chat_id, text, **kwargs):
                    original = kwargs['before_send']
                    async def before():
                        await original()
                        await page.evaluate('''() => {const button=document.querySelector('button');button.disabled=true;
                            setTimeout(()=>{document.querySelector('textarea').value='Unapproved';button.disabled=false},150)}''')
                    return await chat.send_message(page,chat_id,text,fill_preview=fill,before_send=before,
                        **({'on_no_action':kwargs['on_no_action']} if 'on_no_action' in kwargs else {}))
                monkeypatch.setattr(cr,'send_message',send)
                assert not (await workflow.run())['ok']
                assert await page.evaluate('() => window.sent') == 0
                assert attempt_status(workflow) == 'failed'
                state = workflow.repo.load()['chat']
                assert not state.get('last_replied_msg_id') and not state.get('replies_count')
                assert (await workflow.run(dry_run=True))['ok']
            finally:
                await browser.close()
    asyncio.run(run())


def test_forms_owned_zero_submit_does_not_poison_recheck(flow, monkeypatch):
    paths, repo, client, _ = flow
    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                context = await browser.new_context()
                await context.route('**/*',lambda route:route.abort())
                page = await context.new_page()
                await page.set_content('''<form><div role="listitem"><input type="text" name="entry.1"></div><button type="submit">Submit</button></form><script>window.sent=0;document.addEventListener('submit',e=>{e.preventDefault();window.sent++})</script>''')
                page.goto = AsyncMock(); page.wait_for_timeout = AsyncMock()
                client._page = page
                async def fill(page, questions, answers, **kwargs):
                    result = await filling.fill_form(page,questions,answers, **kwargs)
                    await page.evaluate('''() => {const cover=document.createElement('div');cover.style='position:fixed;inset:0;z-index:999;background:white';document.body.append(cover);
                        setTimeout(()=>{document.querySelector('input').value='Unapproved';cover.remove()},350)}''')
                    return result
                monkeypatch.setattr(gforms,'fill_form',fill)
                monkeypatch.setattr(gforms,'_click_google_form_submit',filling._click_google_form_submit)
                assert not (await gforms.submit_saved_preview(client,TOKEN,runtime_paths=paths))['ok']
                assert await page.evaluate('() => window.sent') == 0
                assert repo.load()['items'][TOKEN]['status'] == 'submit_failed'
                from google_forms.workflow import FormWorkflow
                assert FormWorkflow(paths.home_dir).capture(TOKEN)
            finally:
                await browser.close()
    asyncio.run(run())


@pytest.mark.parametrize('case',['admitted_click','missing_receipt','replaced_receipt'])
def test_native_without_exclusive_owned_zero_receipt_stays_uncertain(tmp_path,case):
    async def run():
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True)
            try:
                context=await browser.new_context()
                await context.route('**/*',lambda route:route.abort())
                page=await context.new_page()
                await page.set_content('''<form name="vacancy_response"><input type="hidden" name="resume_id" value="target"><textarea name="letter">Approved</textarea>
                    <button type="submit" onclick="window.clicks++;document.querySelector('textarea').value='Unapproved'">Apply</button></form><script>window.clicks=0;window.sent=0;
                    document.addEventListener('submit',e=>{e.preventDefault();window.sent++})</script>''')
                await page.evaluate('''kind=>document.addEventListener('click',e=>{
                    if(e.target.tagName!=='BUTTON') return;
                    if(kind==='missing_receipt') setTimeout(()=>document.__hhSubmitApproval=null,0);
                    if(kind==='replaced_receipt') setTimeout(()=>document.__hhSubmitApproval={id:'wrong',blocked:true,admitted:false},0);
                },true)''',case)
                page.wait_for_timeout=AsyncMock()
                client=hh_client.HHClient();client._page=page
                client._approved_hh_payload={'resume_id':'target','cover_letter':'Approved','answers':[]}
                repo=NativeApplyRepository(str(tmp_path/'hh.json'),'hh')
                async def operation():
                    button=await page.query_selector('button')
                    original_evaluate=button.evaluate
                    async def evaluate(script,args=None):
                        receipt = await original_evaluate(script,args)
                        if 'codex:action-dispatch' in script:
                            if case=='missing_receipt': return None
                            if case=='replaced_receipt': return {'id':'foreign','dispatched':False,'ok':False}
                        return receipt
                    button.evaluate=evaluate
                    return {'ok':await client._click_with_fallbacks(button,'submit_button')}
                result=await run_native_attempt(client,repo,'https://hh.ru/vacancy/2',operation)
                assert await page.evaluate('()=>window.sent')==0
                assert await page.evaluate('()=>window.clicks')==1
                assert result['uncertain'] and repo.get('https://hh.ru/vacancy/2')['status']=='uncertain'
                assert not repo.claim('https://hh.ru/vacancy/2','','')
            finally:await browser.close()
    asyncio.run(run())


def test_no_action_receipt_cannot_reopen_another_manual_owner(tmp_path,monkeypatch):
    import manual_apply_queue as queue
    monkeypatch.setattr(queue,'_queue_path',lambda profile_name=None:tmp_path/'queue.json')
    token=queue.create_candidate({'source':'hh','id':'2','url':'https://hh.ru/vacancy/2'},{'score':55})['token']
    owner=queue.claim_candidate(token)['owner']
    assert queue.begin_external(token,owner)
    assert not queue.confirm_no_action(token,'stale')
    assert queue.get_candidate(token)['external_started']
    assert queue.confirm_no_action(token,owner)
    assert queue.finish_candidate(token,owner,'pending')
    new=queue.claim_candidate(token)['owner']
    assert queue.begin_external(token,new)
    assert not queue.confirm_no_action(token,owner)
    assert queue.finish_candidate(token,new,'failed')
    assert queue.get_candidate(token)['status']=='uncertain'


def test_no_action_receipt_cannot_change_another_chat_owner(workflow):
    owner=workflow.repo.claim('chat','reply','m1')
    workflow.repo.mark_acting('chat',owner)
    with pytest.raises(RuntimeError,match='ownership'):
        workflow.repo.confirm_no_action('chat','stale')
    assert attempt_status(workflow)=='acting'
    assert workflow.repo.finish('chat',owner,'failed')
    assert attempt_status(workflow)=='uncertain'


def test_no_action_receipt_cannot_change_another_form_owner(flow):
    paths,repo,_,_=flow
    from google_forms.workflow import FormWorkflow
    workflow=FormWorkflow(paths.home_dir)
    _,attempt=workflow.claim(TOKEN)
    workflow.mark_submitting(TOKEN,attempt)
    with pytest.raises(ValueError,match='Владелец'):
        workflow.confirm_no_action(TOKEN,'stale')
    assert repo.load()['items'][TOKEN]['submission']['phase']=='submitting'
    assert workflow.finish(TOKEN,attempt)
    assert repo.load()['items'][TOKEN]['status']=='submit_uncertain'


def test_habr_reserved_but_rejected_before_browser_command_is_failed(tmp_path):
    async def run():
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True)
            try:
                context=await browser.new_context()
                await context.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body='<section data-vacancy-id="1"><button onclick="window.sent++">Apply</button></section><script>window.sent=0</script>'))
                page=await context.new_page();await page.goto('https://career.habr.com/vacancies/2')
                page.wait_for_timeout=AsyncMock()
                client=HabrCareerClient();client._page=page;client._apply_destination=page.url
                repo=NativeApplyRepository(str(tmp_path/'habr.json'),'habr')
                async def operation():
                    client._external_attempt.begin()
                    return {'ok':await client._click_with_fallbacks(await page.query_selector('button'),'habr_apply_button')}
                await run_native_attempt(client,repo,'https://career.habr.com/vacancies/2',operation)
                assert await page.evaluate('()=>window.sent')==0
                assert repo.get('https://career.habr.com/vacancies/2')['status']=='failed'
            finally:await browser.close()
    asyncio.run(run())
