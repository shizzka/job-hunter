"""Synthetic chat previews and sends; no Telegram/HH traffic."""
import asyncio
from unittest.mock import AsyncMock

import pytest
import hh_chat_responder as cr
from hh.chat import quick_reply_choice
from tests.test_chat_workflow_transactions import workflow


async def send_preview(workflow, preview):
    message_id = 'm1' + ('~' + preview['draft_revision'] if preview.get('draft_revision') else '')
    return await cr.process_one(workflow.client, 'chat', message_id=message_id, dry_run=False,
                                runtime_paths=workflow.paths)


def test_preview_A_generator_B_sends_exactly_A(workflow, monkeypatch):
    monkeypatch.setattr(cr, 'generate_answer', AsyncMock(return_value='Answer A'))
    preview = asyncio.run(workflow.run(dry_run=True))
    generator = AsyncMock(return_value='Answer B')
    monkeypatch.setattr(cr, 'generate_answer', generator)
    sent = []
    async def send(page, chat_id, answer, **kwargs):
        await kwargs['before_send']()
        sent.append(answer)
        return True
    monkeypatch.setattr(cr, 'send_message', send)
    result = asyncio.run(send_preview(workflow, preview))
    assert result['sent']
    assert sent == ['Answer A']
    generator.assert_not_awaited()


def test_alternative_invalidates_old_send_revision(workflow, monkeypatch):
    first = asyncio.run(workflow.run(dry_run=True))
    monkeypatch.setattr(cr, 'generate_answer', AsyncMock(return_value='Alternative'))
    second = asyncio.run(cr.process_one(workflow.client, 'chat', message_id='m1', dry_run=True,
                                        alternative=True, runtime_paths=workflow.paths))
    assert first['draft_revision'] != second['draft_revision']
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(cr, 'send_message', send)
    assert not asyncio.run(send_preview(workflow, first))['ok']
    send.assert_not_awaited()
    assert asyncio.run(send_preview(workflow, second))['sent']
    assert send.await_args.args[2] == 'Alternative'


@pytest.mark.parametrize('change', ['message', 'candidate', 'profile'])
def test_old_approval_cannot_send_after_input_version_changes(workflow, monkeypatch, change):
    import hh_client
    preview = asyncio.run(workflow.run(dry_run=True))
    if change == 'message': workflow.data['messages'][-1]['text'] = 'Different question with same ID'
    if change == 'candidate': monkeypatch.setattr(hh_client, '_load_resume_text', lambda: 'Different candidate resume')
    if change == 'profile': monkeypatch.setattr(cr, '_active_profile_name', lambda: 'other-profile')
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(cr, 'send_message', send)
    result = asyncio.run(send_preview(workflow, preview))
    assert not result['ok']
    send.assert_not_awaited()


@pytest.mark.parametrize('text', ['Да, только удалённо и без переезда.', 'Нет, но готов на частичную занятость.', 'Да — если зарплата от 200000'])
def test_quick_reply_never_drops_qualifiers(text):
    assert quick_reply_choice(text) == ''


@pytest.mark.parametrize('text,choice', [('Да','Да'), (' да. ','Да'), ('Нет!', 'Нет')])
def test_plain_yes_no_may_use_shortcut(text, choice):
    assert quick_reply_choice(text) == choice


def test_revision_callback_route_rejects_old_unbound_buttons():
    from telegram_bot_ui import _parse_chat_send_callback_data
    markup = cr.build_chat_answer_preview_markup('qa', '123', '456', draft_revision='abcdef012345')
    data = next(button['callback_data'] for row in markup['inline_keyboard'] for button in row
                if button.get('callback_data', '').startswith('chat_send:'))
    assert _parse_chat_send_callback_data(data) == ('qa', '123', '456~abcdef012345')
    assert _parse_chat_send_callback_data('chat_send:qa:123:456') == ('', '', '')


def test_candidate_change_during_approved_browser_fill_blocks_action(workflow, monkeypatch):
    import hh_client
    preview = asyncio.run(workflow.run(dry_run=True))
    async def send(*args, **kwargs):
        monkeypatch.setattr(hh_client, '_load_resume_text', lambda: 'Changed after browser await')
        await kwargs['before_send']()
        pytest.fail('No click allowed')
    monkeypatch.setattr(cr, 'send_message', send)
    with pytest.raises(RuntimeError, match='changed before send'):
        asyncio.run(send_preview(workflow, preview))


@pytest.mark.parametrize('answer,shortcut', [('Да, только удалённо и без переезда.', False), ('Да', True)])
def test_actual_offline_browser_sends_full_qualified_answer(tmp_path, answer, shortcut):
    from playwright.async_api import async_playwright
    import hh.chat as low
    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                context = await browser.new_context()
                await context.route('**/*', lambda route: route.abort())
                page = await context.new_page()
                await page.set_content("""<textarea data-qa="chatik-new-message-text"></textarea>
                    <button id="quick" onclick="window.sent='Да';window.shortcut=true">Да</button>
                    <button data-qa="chatik-do-send-message" onclick="window.sent=document.querySelector('textarea').value;window.shortcut=false">Send</button>""")
                async def noop(*a, **kw): pass
                page.wait_for_timeout = noop
                async def fill(*args):
                    return await low.fill_and_preview(*args, state_dir=str(tmp_path), open_page=noop,
                                                       dismiss_cookies=noop)
                async def messages(*args):
                    return {'messages': [{'is_me': True, 'text': await page.evaluate('() => window.sent')}]}
                assert await low.send_message(page, '123', answer, fill_preview=fill, extract_current_messages=messages)
                assert await page.evaluate('() => window.sent') == answer
                assert await page.evaluate('() => window.shortcut') is shortcut
            finally:
                await browser.close()
    asyncio.run(run())
