"""R2: exact approved composer payload/root at actual send event, offline."""
import asyncio
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright
from hh import chat


@pytest.mark.parametrize('mutation', ['text', 'editor', 'root', 'control', 'none'])
@pytest.mark.parametrize('obstacle', ['disabled', 'covered'])
def test_chat_send_event_requires_exact_approved_dom_payload(tmp_path, mutation, obstacle):
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
                await page.set_content('''<section id="composer"><textarea data-qa="chatik-new-message-text"></textarea>
                    <button id="send" data-qa="chatik-do-send-message" onclick="window.sent.push(document.querySelector('textarea').value)">Send</button>
                    </section><script>window.sent=[];</script>''')
                if obstacle == 'disabled':
                    await page.locator('#send').evaluate('el => el.disabled=true')
                else:
                    await page.evaluate("() => {const el=document.createElement('div');el.id='cover';el.style='position:fixed;inset:0;z-index:999;background:white';document.body.append(el)}")
                page.wait_for_timeout = AsyncMock()
                async def noop(*args, **kwargs): pass
                async def fill(*args):
                    return await chat.fill_and_preview(*args, state_dir=str(tmp_path), open_page=noop, dismiss_cookies=noop)
                async def messages(*args):
                    sent = await page.evaluate('() => window.sent')
                    return {'messages': [{'is_me': True, 'text': value} for value in sent]}
                async def last_guard():
                    assert await page.locator('textarea').input_value() == 'Approved exact answer'
                    await page.evaluate('''mutation => setTimeout(() => {
                        const button=document.querySelector('#send'), root=document.querySelector('#composer');
                        if(mutation==='text') document.querySelector('textarea').value='Unapproved';
                        if(mutation==='editor') document.querySelector('textarea').replaceWith(document.querySelector('textarea').cloneNode(true));
                        if(mutation==='root') {const replacement=root.cloneNode(true); replacement.querySelector('button').replaceWith(button);root.replaceWith(replacement);}
                        if(mutation==='control') root.insertAdjacentHTML('beforeend','<textarea name="another_payload">Unapproved</textarea>');
                        button.disabled=false;document.querySelector('#cover')?.remove();
                    }, 150)''', mutation)
                result = await chat.send_message(page, 'synthetic', 'Approved exact answer', fill_preview=fill,
                                                 extract_current_messages=messages, before_send=last_guard)
                sent = await page.evaluate('() => window.sent')
                assert len(sent) == (1 if mutation == 'none' else 0)
                assert result is (mutation == 'none')
                if sent: assert sent == ['Approved exact answer']
                assert not requests
            finally:
                await browser.close()
    asyncio.run(run())
