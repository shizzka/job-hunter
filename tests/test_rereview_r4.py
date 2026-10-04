"""R4: native destination/context at actual click, real offline Chromium."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright, ElementHandle, Locator
import config
from habr_career_client import HabrCareerClient
from superjob_client import SuperJobClient


@pytest.mark.parametrize('source,stage', [('habr','apply'),('habr','submit'),('superjob','apply'),('superjob','submit')])
@pytest.mark.parametrize('mutation', ['url','identity','control','root','none'])
@pytest.mark.parametrize('obstacle', ['disabled','covered'])
def test_native_actual_action_requires_bound_destination(tmp_path, monkeypatch, source, stage, mutation, obstacle):
    monkeypatch.setattr(config,'HABR_COOKIES_FILE',str(tmp_path/'habr.json'))
    monkeypatch.setattr(config,'SUPERJOB_COOKIES_FILE',str(tmp_path/'sj.json'))
    monkeypatch.setattr(config,'SUPERJOB_AUTH_FILE',str(tmp_path/'sj-auth.json'))
    monkeypatch.setattr(config,'HH_STATE_DIR',str(tmp_path/'state'))
    expected = 'https://career.habr.com/vacancies/2' if source=='habr' else 'https://www.superjob.ru/vakansii/qa-2.html'
    old_path = '/vacancies/1' if source=='habr' else '/vakansii/qa-1.html'
    label = 'Откликнуться'
    if source=='habr' and stage=='submit':
        buttons='<textarea name="letter"></textarea><button id="submit" onclick="window.record(this)">Откликнуться</button>'
    else:
        buttons='<button id="apply" class="f-test-vacancy-response-button" onclick="window.record(this)">Откликнуться</button>'
        if source=='superjob': buttons+='<button id="submit" type="submit" class="f-test-button-Otkliknutsya" onclick="window.record(this)">Отправить</button>'
    html='''<meta charset="utf-8"><section id="vacancy" data-vacancy-id="2"><h1>Synthetic vacancy</h1>
        <input type="hidden" name="vacancy_id" value="2">'''+buttons+'''</section><script>window.actions=[];
        window.record=button=>window.actions.push({stage:button.id,url:location.pathname,
            context:document.querySelector('#vacancy').dataset.vacancyId,
            id:document.querySelector('[name=vacancy_id]').value});</script>'''
    async def run():
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True)
            try:
                context=await browser.new_context()
                await context.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body=html))
                page=await context.new_page()
                page.wait_for_timeout=AsyncMock()
                originals={ElementHandle:ElementHandle.click,Locator:Locator.click}
                async def click(control,*args,**kwargs):
                    if await control.evaluate('el=>el.id')==stage:
                        await control.evaluate('''(button,args)=>{
                            if(args.obstacle==='disabled') button.disabled=true;
                            else {const cover=document.createElement('div');cover.id='cover';cover.style='position:fixed;inset:0;z-index:999;background:white';document.body.append(cover);}
                            setTimeout(()=>{
                                const root=document.querySelector('#vacancy');
                                if(args.mutation==='url') history.replaceState({},'',args.path);
                                if(args.mutation==='identity') root.dataset.vacancyId='1';
                                if(args.mutation==='control') root.querySelector('[name=vacancy_id]').value='1';
                                if(args.mutation==='root') {const replacement=root.cloneNode(true);replacement.querySelector('#'+button.id).replaceWith(button);root.replaceWith(replacement);}
                                button.disabled=false;document.querySelector('#cover')?.remove();
                            },150);
                        }''',{'mutation':mutation,'obstacle':obstacle,'path':old_path})
                    return await originals[ElementHandle if isinstance(control,ElementHandle) else Locator](control,*args,**kwargs)
                monkeypatch.setattr(ElementHandle,'click',click)
                monkeypatch.setattr(Locator,'click',click)
                class Response:
                    async def __aenter__(self):
                        self.value=asyncio.get_running_loop().create_future()
                        self.value.set_result(SimpleNamespace(status=200))
                        return self
                    async def __aexit__(self,*args): return False
                page.expect_response=lambda *args,**kwargs:Response()
                client=HabrCareerClient() if source=='habr' else SuperJobClient()
                client._page=page
                client._page_is_logged_in=AsyncMock(return_value=True)
                if source=='habr': await client.apply_to_vacancy(expected,'Approved' if stage=='submit' else '')
                else: await client.apply_to_vacancy({'url':expected,'external_id':'2'})
                actions=await page.evaluate('()=>window.actions')
                relevant=[item for item in actions if item['stage']==stage]
                assert len(relevant)==(1 if mutation=='none' else 0),actions
                if relevant: assert relevant[0]['context']==relevant[0]['id']=='2'
                if mutation!='none' and stage=='apply': assert not actions
            finally: await browser.close()
    asyncio.run(run())


@pytest.mark.parametrize('source',['habr','superjob'])
@pytest.mark.parametrize('mutation',['change_external_id','none'])
def test_native_form_associated_vacancy_payload_is_bound(tmp_path,monkeypatch,source,mutation):
    monkeypatch.setattr(config,'HABR_COOKIES_FILE',str(tmp_path/'habr.json'))
    monkeypatch.setattr(config,'SUPERJOB_AUTH_FILE',str(tmp_path/'sj-auth.json'))
    expected='https://career.habr.com/vacancies/2' if source=='habr' else 'https://www.superjob.ru/vakansii/qa-2.html'
    html='''<meta charset="utf-8"><form id="approved" data-vacancy-id="2"><button id="apply" type="submit" class="f-test-vacancy-response-button">Откликнуться</button></form>
        <input id="external" type="hidden" form="approved" name="vacancy_id" value="2"><script>window.sent=[];
        document.addEventListener('submit',e=>{e.preventDefault();window.sent.push([...new FormData(e.target,e.submitter).entries()])});</script>'''
    async def run():
        async with async_playwright() as pw:
            browser=await pw.chromium.launch(headless=True)
            try:
                context=await browser.new_context()
                await context.route('**/*',lambda route:route.fulfill(status=200,content_type='text/html',body=html))
                page=await context.new_page();await page.goto(expected)
                client=HabrCareerClient() if source=='habr' else SuperJobClient();client._page=page
                control=await page.query_selector('#apply')
                assert await client._arm_destination_boundary(control,expected)
                await page.evaluate('''mutation=>{
                    const cover=document.createElement('div');cover.style='position:fixed;inset:0;z-index:999;background:white';document.body.append(cover);
                    setTimeout(()=>{if(mutation==='change_external_id') document.querySelector('#external').value='1';cover.remove()},150);
                }''',mutation)
                await control.click()
                assert len(await page.evaluate('()=>window.sent'))==(1 if mutation=='none' else 0)
            finally:await browser.close()
    asyncio.run(run())
