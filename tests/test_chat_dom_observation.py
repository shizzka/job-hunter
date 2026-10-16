"""Execute production extraction JS against local, fully synthetic DOM."""
import asyncio

from playwright.async_api import async_playwright

from hh.chat import extract_messages, list_chats


async def local_page(callback):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context(service_workers="block")
            await context.route("**/*", lambda route: route.abort())
            page = await context.new_page()
            await callback(page)
        finally:
            await browser.close()


def test_chat_virtual_list_starts_at_top_and_preserves_full_rows():
    async def run(page):
        await page.set_content('''<div id="list" style="overflow-y:auto;height:200px;position:relative">
          <div id="space" style="height:1000px"></div></div>
          <script>
          const list=document.querySelector('#list');
          function render(){
            list.querySelectorAll('[data-qa]').forEach(e=>e.remove());
            const start=Math.floor(list.scrollTop/50);
            for(let i=start;i<Math.min(20,start+5);i++){
              const el=document.createElement('div');
              el.setAttribute('data-qa','chatik-open-chat-'+i);
              el.style='position:absolute;height:50px;overflow:hidden;top:'+i*50+'px';
              el.innerText='Company '+i+'\\n'+'Synthetic '.repeat(60);
              if(i===12)el.setAttribute('data-unread','true');
              list.append(el);
            }
          }
          list.onscroll=render;list.scrollTop=500;render();
          </script>''')
        async def no_navigation(*args, **kwargs):
            pass
        result = await list_chats(page, open_page=no_navigation)
        assert [row["chat_id"] for row in result] == [str(i) for i in range(20)]
        assert result.list_complete is True and result.stop_reason == "stable_bottom"
        assert len(result[0]["raw_text"]) > 300 and "\n" in result[0]["raw_text"]
        assert len(result[0]["preview"]) == 300
        assert result[12]["unread"] is True
    asyncio.run(local_page(run))


def test_chat_dom_messages_capture_evidence_controls_links_and_full_text():
    async def run(page):
        text = "Synthetic full message " * 50
        await page.set_content(f'''<div data-qa="chat-header-title">Synthetic Company</div>
        <a href="https://hh.ru/vacancy/123">Synthetic QA</a>
        <div data-qa="chatik-chat-message-1"><div data-qa="chat-bubble-author-name">HR</div>
          <div data-qa="chatik-chat-message-1-text">{text}<a href="https://t.me/synthetic_bot">Bot</a></div>
          <button>Да</button><button>Нет</button><time datetime="2026-10-16T12:00:00Z">12:00</time></div>
        <div data-qa="chatik-chat-message-2"><div class="chat-bubble_outgoing">
          <div data-qa="chatik-chat-message-2-text">Synthetic answer</div></div></div>
        <div data-qa="chatik-chat-message-3"><div data-qa="chat-bubble-author-name">ИИ-помощник</div>
          <div data-qa="chatik-chat-message-3-text">Работодатель хотел бы узнать: опыт?</div></div>
        <button data-qa="synthetic-quick-reply">Связаться завтра</button>''')
        result = await extract_messages(page)
        first, ours, ai = result["messages"]
        assert result["history_complete"] is False
        assert text in first["raw_text"] and len(first["text"]) > 300
        assert first["dom_index"] == 0 and ai["dom_index"] == 2
        assert first["author"] == "HR" and not first["is_me"] and not first["is_ai"]
        assert first["controls"] == ["Да", "Нет"]
        assert result["thread_controls"] == ["Да", "Нет", "Связаться завтра"]
        assert first["links"][0]["href"] == "https://t.me/synthetic_bot"
        assert first["time"] == "2026-10-16T12:00:00Z"
        assert ours["is_me"] and ours["classification_evidence"] == ["outgoing_css_marker"]
        assert ai["is_ai"] and "ai_author_label" in ai["classification_evidence"]
    asyncio.run(local_page(run))


def test_chat_list_without_proven_scroll_end_is_incomplete():
    async def run(page):
        await page.set_content('<div data-qa="chatik-open-chat-1">Synthetic</div>')
        async def no_navigation(*args, **kwargs):
            pass
        result = await list_chats(page, open_page=no_navigation)
        assert not result.list_complete and result.stop_reason == "no_scroll_container"
    asyncio.run(local_page(run))
