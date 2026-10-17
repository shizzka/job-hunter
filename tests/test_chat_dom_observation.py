"""Execute production extraction JS against local, fully synthetic DOM."""
import asyncio
import pytest

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


@pytest.mark.parametrize('label', ['Сегодня', 'Вчера', '5 октября', '5 октября 2026'])
@pytest.mark.parametrize('outgoing', [False, True])
def test_chat_date_dividers_are_neither_incoming_nor_outgoing(label, outgoing):
    async def run(page):
        marker = 'chat-bubble_outgoing' if outgoing else 'chat-bubble'
        await page.set_content(f'''<div data-qa="chatik-chat-message-1">
          <div class="{marker}"><div data-qa="chatik-chat-message-1-text">{label}</div></div></div>
        <div data-qa="chatik-chat-message-2"><div data-qa="chatik-chat-message-2-text">Real question?</div></div>''')
        result = await extract_messages(page)
        assert [m['id'] for m in result['messages']] == ['2']
    asyncio.run(local_page(run))


@pytest.mark.parametrize('disabled', [False, True])
def test_chat_diagnostic_regions_and_neighbours_are_local_sanitized_and_private(tmp_path, disabled):
    import json
    import stat
    from chat_observation import ChatJournal, diagnostic_snapshot

    async def run(page):
        composer = ('<span class="disabled-label">Работодатель отключил переписку по данной вакансии</span>'
                    if disabled else '<textarea data-qa="chatik-new-message-text"></textarea><button>Отправить</button>')
        rows = ''.join(f'''<a data-qa="chatik-open-chat-{i}" class="row {'active' if i == 3 else ''}"
            data-unread="{'true' if i == 4 else 'false'}"><span>Company {i}</span>
            <span data-qa="row-status">{'Отказ' if i == 8 else 'Отклик на вакансию'}</span>
            <span>Последнее сообщение</span><time>12:34</time></a>''' for i in range(10))
        await page.set_content(f'''<aside>{rows}</aside><main>
          <section class="header"><div><h1 data-qa="chat-header-title">Synthetic Company</h1></div>
          <div class="vacancy">Вакансия: Synthetic QA · <a href="/vacancy/123">Перейти</a></div>
          <script type="application/json">"PRIVATE_BOOTSTRAP"</script>
          <iframe srcdoc="PRIVATE_FRAME"></iframe></section>
          <section class="history"><div data-qa="chatik-chat-message-11">
            <div data-qa="chat-bubble-title">Отказ</div>
            <span data-qa="chat-bubble-text">Synthetic message</span>
            <span data-qa="chat-marker-label-text">Сегодня</span>
          </div></section><footer class="composer" onclick="PRIVATE_HANDLER()">{composer}</footer>
          </main><div id="outside">PRIVATE_OUTSIDE</div>
          <script>window.localStorage.setItem('PRIVATE_STORAGE', 'PRIVATE_VALUE')</script>''')
        await diagnostic_snapshot(ChatJournal(tmp_path, 'synthetic'), page, '3', '11', 'rejection', 'ref', 1)
        root = tmp_path / 'chat_diagnostics'
        html = next(root.glob('*.html')).read_text()
        meta = json.loads(next(p for p in root.glob('*.json') if p.name != 'state.json').read_text())
        assert all(secret not in html for secret in (
            'PRIVATE_BOOTSTRAP', 'PRIVATE_FRAME', 'PRIVATE_HANDLER', 'PRIVATE_OUTSIDE', 'PRIVATE_STORAGE'))
        assert '<script' not in html and '<iframe' not in html and 'onclick' not in html
        assert 'Synthetic Company' in html and 'Вакансия:' in html and '/vacancy/123' in html
        assert ('Работодатель отключил' in html) if disabled else ('<textarea' in html)
        fragments = meta['fragments']
        assert [f['kind'] for f in fragments].count('header') == 1
        assert [f['kind'] for f in fragments].count('composer') == 1
        captured_rows = [f for f in fragments if f['kind'] in ('selected_row', 'list_row')]
        assert len(captured_rows) == 4
        assert captured_rows[0]['data_qa'] == 'chatik-open-chat-3'
        assert {f['data_qa'] for f in captured_rows} == {
            'chatik-open-chat-2', 'chatik-open-chat-3', 'chatik-open-chat-4', 'chatik-open-chat-8'}
        assert '12:34' in html and 'data-unread="true"' in html and 'row-status' in html
        assert 'chat-bubble-title' in html and 'chat-marker-label-text' in html
        assert all('html' not in fragment for fragment in fragments)
        assert stat.S_IMODE(root.stat().st_mode) == 0o700
        assert all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in root.iterdir())
    asyncio.run(local_page(run))


def test_chat_diagnostic_fragment_limits_and_missing_regions():
    from chat_observation import collect_diagnostic_fragments, DIAGNOSTIC_FRAGMENT_LIMIT

    async def run(page):
        await page.set_content('<script type="application/json">"private"</script>')
        assert await collect_diagnostic_fragments(page, '42') == []
        await page.set_content('<div data-qa="chatik-chat-message-1">' + 'Я🙂' * 15000 + '</div>')
        fragments = await collect_diagnostic_fragments(page, '42')
        assert len(fragments) == 1 and fragments[0]['kind'] == 'message'
        assert fragments[0]['truncated']
        assert len(fragments[0]['html'].encode('utf-8')) <= DIAGNOSTIC_FRAGMENT_LIMIT
        assert '\ufffd' not in fragments[0]['html']
    asyncio.run(local_page(run))


def test_chat_diagnostic_unknown_header_uses_vacancy_context_without_list_or_message_links():
    from chat_observation import collect_diagnostic_fragments

    async def run(page):
        await page.set_content('''<aside><a data-qa="chatik-open-chat-1" href="/vacancy/1">Wrong row</a></aside>
          <main><div class="unknown-header"><div>Company</div><div>Вакансия: QA ·
          <a href="/vacancy/2">Перейти</a></div></div>
          <div data-qa="chatik-chat-message-1"><a href="/vacancy/3">Wrong message</a></div>
          <footer><textarea data-qa="text-input"></textarea></footer></main>''')
        fragments = await collect_diagnostic_fragments(page, '1')
        header = next(f['html'] for f in fragments if f['kind'] == 'header')
        assert 'Company' in header and '/vacancy/2' in header
        assert 'Wrong' not in header and '<textarea' not in header
    asyncio.run(local_page(run))


def test_chat_diagnostic_empty_thread_keeps_header_and_composer_separate():
    from chat_observation import collect_diagnostic_fragments, DIAGNOSTIC_FRAGMENT_LIMIT

    async def run(page):
        await page.set_content('<div class="shell"><section class="header">'
            '<h1 data-qa="chat-header-title">Company</h1><a href="/vacancy/1">QA</a></section>'
            '<footer><textarea data-qa="text-input"></textarea><span>' + 'Я' * 30000 +
            '</span></footer></div>')
        fragments = await collect_diagnostic_fragments(page, '1')
        assert [f['kind'] for f in fragments] == ['header', 'composer']
        assert '<textarea' not in fragments[0]['html']
        assert 'Company' not in fragments[1]['html']
        assert fragments[1]['truncated']
        assert all(len(f['html'].encode('utf-8')) <= DIAGNOSTIC_FRAGMENT_LIMIT for f in fragments)
    asyncio.run(local_page(run))
