"""Synthetic DOM only: no HH login, provider, Telegram or real submit."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

import config
import notifier
from hh.ui import HHUIGuard, HHUnexpectedUI
from hh_client import HHClient
from hh import apply as hh_apply
from hh import chat as hh_chat
from state_store.hh_ui import HHUIWarnings


async def browser_case(html, action):
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        try:
            context = await browser.new_context()
            await context.route('**/*', lambda route: route.abort())
            page = await context.new_page()
            await page.set_content(html)
            await action(page)
        finally:
            await browser.close()


def optional(title='Резюме стали компактнее', close=True, button_type='button'):
    control = f'<button type="{button_type}" aria-label="Закрыть" onclick="this.closest(\'[role=dialog]\').remove()">X</button>' if close else '<button type="button">Продолжить</button>'
    marker = ' data-qa="applicant-profile-onboarding-modal"' if title in ('Расскажите о себе', 'Заполните профиль') else ''
    return f'<div role="dialog"{marker}><h2>{title}</h2><input name="profile-answer">{control}</div>'


@pytest.mark.parametrize('title', ['Резюме стали компактнее', 'Расскажите о себе', 'Заполните профиль'])
def test_known_optional_modal_closes_without_answers_then_refetches(tmp_path, title):
    async def check(page):
        notify = AsyncMock()
        guard = HHUIGuard(tmp_path, notify=notify)
        await guard.ensure(page, 'synthetic')
        assert await page.locator('[role=dialog]').count() == 0
        notify.assert_not_awaited()
        assert not (tmp_path / 'hh_ui_warnings.json').exists()
    asyncio.run(browser_case(optional(title) + '<button id="response">Apply</button>', check))


@pytest.mark.parametrize('html', [
    optional('Unknown account question'), optional(close=False), optional(button_type='submit'),
    '<div role="dialog"><h2>New HH UI</h2><textarea></textarea><button type="submit">OK</button></div>',
    '<div role="dialog"><h2>Расскажите о себе</h2><button type="button" aria-label="Закрыть">X</button></div>',
    '<div role="dialog" data-qa="whats-new-modal"><h2>Unknown replacement</h2><button type="button" aria-label="Закрыть">X</button></div>',
    '<div role="dialog"><h2>Расскажите о себе</h2><button type="button" aria-label="Закрыть" disabled>X</button></div>',
    optional() + '<div role="dialog"><h2>Unknown second modal</h2></div>',
])
def test_unknown_or_unsafe_modal_blocks_without_close_fill_or_submit(tmp_path, html):
    async def check(page):
        calls = []
        async def notify(photo, stage, fingerprint):
            assert Path(photo).exists()
            assert Path(photo).stat().st_mode & 0o777 == 0o600
            assert Path(photo).parent.stat().st_mode & 0o777 == 0o700
            calls.append(photo)
            return True
        guard = HHUIGuard(tmp_path, notify=notify)
        before = await page.locator('[role=dialog]').count()
        with pytest.raises(HHUnexpectedUI, match='Нестандартное поведение HH'):
            await guard.ensure(page, 'synthetic')
        assert await page.locator('[role=dialog]').count() == before
        assert await page.locator('input,textarea').evaluate_all('(els) => els.every(el => !el.value)')
        assert len(calls) == 1 and not Path(calls[0]).exists()
        assert not list(tmp_path.glob('.hh-ui-*'))
        await page.set_content('<p>Modal disappeared</p>')
        with pytest.raises(HHUnexpectedUI):
            await guard.ensure(page, 'later')
        assert len(calls) == 1
    asyncio.run(browser_case(html, check))


@pytest.mark.parametrize('html,allowed', [
    ('<div role="dialog"><form name="vacancy_response"><button data-qa="vacancy-response-submit-popup">Apply</button></form></div>', ('response',)),
    ('<div role="dialog"><div data-qa="captcha"></div></div>', ('captcha',)),
    ('<div role="dialog" style="display:none"><h2>Unknown</h2></div>', ()),
    ('<div data-qa="modal-overlay">' + optional() + '</div>', ()),
])
def test_expected_hidden_or_nested_surfaces_do_not_trigger_alert(tmp_path, html, allowed):
    async def check(page):
        notify = AsyncMock()
        await HHUIGuard(tmp_path, notify=notify).ensure(page, 'synthetic', allowed=allowed)
        notify.assert_not_awaited()
    asyncio.run(browser_case(html, check))


@pytest.mark.parametrize('event', ['click', 'submit'])
def test_browser_capture_barrier_blocks_modal_appearing_after_last_scan(tmp_path, event):
    html = '<form name="vacancy_response"><button id="submit" data-qa="vacancy-response-submit-popup">Apply</button></form>'
    async def check(page):
        guard = HHUIGuard(tmp_path, notify=AsyncMock(return_value=True))
        await guard.ensure(page, 'before', allowed=('response',))
        await page.evaluate("""() => {
            window.sent = 0;
            document.querySelector('form').addEventListener('submit', e => { e.preventDefault(); window.sent++; });
            document.body.insertAdjacentHTML('beforeend', '<div role="dialog"><h2>Unknown modal</h2></div>');
        }""")
        await page.evaluate("() => document.querySelector('form').requestSubmit()" if event == 'submit' else
                            "() => document.querySelector('#submit').click()")
        assert await page.evaluate('window.sent') == 0
        with pytest.raises(HHUnexpectedUI):
            await guard.ensure(page, 'after', allowed=('response',))
    asyncio.run(browser_case(html, check))


def test_dom_fallback_cannot_submit_unknown_profile_form(tmp_path):
    html = '<div role="dialog"><form name="profile"><input required><button type="submit">Save profile</button></form></div>'
    async def check(page):
        await page.evaluate("() => { window.sent = 0; document.querySelector('form').addEventListener('submit', e => { e.preventDefault(); window.sent++; }); }")
        client = HHClient()
        client._page = page
        client._ui_guard = HHUIGuard(tmp_path, notify=AsyncMock(return_value=True))
        with pytest.raises(HHUnexpectedUI):
            await client._submit_response_form_via_dom()
        assert await page.evaluate('window.sent') == 0
    asyncio.run(browser_case(html, check))


def test_force_and_js_click_fallback_cannot_bypass_blocking_modal(tmp_path):
    async def check(page):
        client = HHClient()
        client._page = page
        client._ui_guard = HHUIGuard(tmp_path, notify=AsyncMock(return_value=True))
        with pytest.raises(HHUnexpectedUI):
            await client._click_with_fallbacks(await page.query_selector('#submit'), 'submit')
        assert await page.evaluate('window.sent') == 0
    html = '<button id="submit" onclick="window.sent++">Submit</button><script>window.sent=0</script>' + optional('Unknown')
    asyncio.run(browser_case(html, check))


@pytest.mark.parametrize('path', ['click', 'dom'])
def test_modal_close_invalidates_handle_and_rechecks_changed_resume(tmp_path, path):
    html = '''<form name="vacancy_response"><input name="resume_id" value="target">
    <button id="submit" data-qa="vacancy-response-submit-popup">Apply</button></form>
    <div role="dialog"><h2>Резюме стали компактнее</h2>
    <button type="button" aria-label="Закрыть" onclick="document.querySelector('[name=resume_id]').value='wrong'; this.parentNode.remove()">X</button></div>'''
    async def check(page):
        await page.evaluate("() => { window.sent=0; document.querySelector('form').addEventListener('submit', e => { e.preventDefault(); window.sent++; }); }")
        client = HHClient()
        client._page = page
        client._ui_guard = HHUIGuard(tmp_path, notify=AsyncMock(return_value=True))
        async def verify():
            return await page.locator('[name=resume_id]').input_value() == 'target'
        if path == 'click':
            assert not await client._click_with_fallbacks(await page.query_selector('#submit'), 'submit', before_click=verify)
        # DOM refetch must see the selection changed by the X handler.
        assert not await client._submit_response_form_via_dom(before_submit=verify)
        assert await page.evaluate('window.sent') == 0
        assert await page.locator('[role=dialog]').count() == 0
    asyncio.run(browser_case(html, check))


def test_chat_safe_reader_does_not_reset_or_swallow_ui_stop():
    stop = HHUnexpectedUI('chat', 'a' * 64)
    reset = AsyncMock()
    with pytest.raises(HHUnexpectedUI):
        asyncio.run(hh_chat.get_messages_safe(object(), 'synthetic', open_page=AsyncMock(side_effect=stop), reset_page=reset))
    reset.assert_not_awaited()


class FakePage:
    def __init__(self, result=None):
        self.result = [{'index': 0, 'kind': 'unknown', 'shape': 'synthetic-structure', 'closable': True}] if result is None else result
        self.photos = []

    async def evaluate(self, script, *args):
        return self.result

    async def screenshot(self, *, path, **kwargs):
        Path(path).write_bytes(b'synthetic image')
        self.photos.append(path)


@pytest.mark.parametrize('result', [None, {}, 'unknown', [{'index': 0}], [{'index': True, 'kind': 'unknown', 'shape': 's', 'closable': True}]])
def test_inspection_failure_fails_closed(tmp_path, result):
    page = FakePage()
    page.result = result
    guard = HHUIGuard(tmp_path, notify=AsyncMock(return_value=True))
    with pytest.raises(HHUnexpectedUI):
        asyncio.run(guard.ensure(page, 'inspect'))


@pytest.mark.parametrize('outcome', ['sent', 'failed', 'cancelled'])
def test_notification_cleanup_and_retry_cooldowns(tmp_path, outcome):
    clock, deliveries = [1000], []
    async def notify(photo, *args):
        deliveries.append(photo)
        assert Path(photo).exists()
        if outcome == 'cancelled':
            raise asyncio.CancelledError()
        return outcome == 'sent'
    async def attempt():
        guard = HHUIGuard(tmp_path, notify=notify, clock=lambda: clock[0])
        with pytest.raises(asyncio.CancelledError if outcome == 'cancelled' else HHUnexpectedUI):
            await guard.ensure(FakePage(), 'synthetic')
    asyncio.run(attempt())
    assert len(deliveries) == 1 and not Path(deliveries[0]).exists()
    # A second independent session/process must also respect persisted claim.
    guard = HHUIGuard(tmp_path, notify=AsyncMock(return_value=True), clock=lambda: clock[0])
    with pytest.raises(HHUnexpectedUI):
        asyncio.run(guard.ensure(FakePage(), 'again'))
    guard.notify.assert_not_awaited()
    clock[0] += 86400 if outcome == 'sent' else 300
    asyncio.run(attempt())
    assert len(deliveries) == 2
    assert not list(tmp_path.glob('.hh-ui-*'))


def test_corrupt_warning_state_preserved_and_delivery_suppressed(tmp_path):
    path = tmp_path / 'hh_ui_warnings.json'
    path.write_bytes(b'{corrupt')
    for _ in range(2):
        notify = AsyncMock()
        page = FakePage()
        with pytest.raises(HHUnexpectedUI):
            asyncio.run(HHUIGuard(tmp_path, notify=notify).ensure(page, 'synthetic'))
        assert path.read_bytes() == b'{corrupt'
        notify.assert_not_awaited()
        assert not page.photos


def test_original_home_and_delivery_target_survive_profile_switch(tmp_path, monkeypatch):
    original, other = tmp_path / 'original', tmp_path / 'other'
    monkeypatch.setattr(config, 'JOB_HUNTER_HOME', str(original))
    target = ('synthetic-token', (123,), '')
    monkeypatch.setattr(notifier, 'capture_delivery_target', lambda: target)
    client = HHClient()
    client._page = FakePage()
    monkeypatch.setattr(config, 'JOB_HUNTER_HOME', str(other))
    monkeypatch.setattr(notifier, 'capture_delivery_target', lambda: ('other-synthetic-token', (456,), ''))
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(notifier, 'notify_hh_unexpected_ui', send)
    with pytest.raises(HHUnexpectedUI):
        asyncio.run(client._ensure_expected_ui('synthetic'))
    assert send.await_args.kwargs['target'] == target
    assert (original / 'hh_ui_warnings.json').exists()
    assert not other.exists()


def test_late_warning_completion_cannot_overwrite_new_owner(tmp_path):
    clock = [1000]
    store = HHUIWarnings(tmp_path, clock=lambda: clock[0])
    fingerprint = 'a' * 64
    first = store.claim(fingerprint)
    clock[0] += 300
    second = store.claim(fingerprint)
    store.finish(fingerprint, first, 'sent')
    item = store.store.load()['alerts'][fingerprint]
    assert item['attempt_id'] == second and item['status'] == 'attempting'


def test_alert_multipart_handles_closed_and_original_target_used(tmp_path, monkeypatch):
    photo = tmp_path / 'synthetic.png'
    photo.write_bytes(b'synthetic')
    handles, routes = [], []
    async def deliver(url, form, **kwargs):
        routes.append(url)
        handle = next(value for _, _, value in form._fields if hasattr(value, 'read'))
        handles.append(handle)
        return True
    monkeypatch.setattr(notifier, '_deliver_multipart', deliver)
    target = ('synthetic-token', (123,), '')
    assert asyncio.run(notifier.notify_hh_unexpected_ui(str(photo), 'test', 'a' * 64, target=target))
    assert handles and all(handle.closed for handle in handles)
    assert all('synthetic-token' in route for route in routes)


def test_chat_send_blocks_unknown_ui_before_any_send(tmp_path):
    page = FakePage()
    page._hh_ui_guard = HHUIGuard(tmp_path, notify=AsyncMock(return_value=True))
    page.query_selector = AsyncMock(return_value=SimpleNamespace(click=AsyncMock()))
    with pytest.raises(HHUnexpectedUI):
        asyncio.run(hh_chat.send_message(page, 'synthetic', 'text', fill_preview=AsyncMock(return_value={'filled': True})))
    page.query_selector.return_value.click.assert_not_awaited()


def test_search_ui_pause_is_not_rejected_or_swallowed(tmp_path, monkeypatch):
    import agent
    import seen
    for key, filename in [('JOB_HUNTER_HOME', ''), ('SEEN_VACANCIES_FILE', 'seen.json'),
                          ('RUNTIME_STATUS_FILE', 'runtime.json'), ('RUN_HISTORY_FILE', 'history.jsonl')]:
        monkeypatch.setattr(config, key, str(tmp_path / filename))
    for key in ('SUPERJOB_ENABLED', 'HABR_ENABLED', 'GEEKJOB_ENABLED'):
        monkeypatch.setattr(config, key, False)
    monkeypatch.setattr(config, 'HH_ENABLED', True)
    blocked = HHUnexpectedUI('synthetic', 'a' * 64)
    client = SimpleNamespace(start=AsyncMock(), stop=AsyncMock(), is_logged_in=AsyncMock(side_effect=blocked))
    monkeypatch.setattr(agent, 'HHClient', lambda: client)
    monkeypatch.setattr(agent.notifier, 'notify_stale_cookies', AsyncMock())
    monkeypatch.setattr(agent, 'office_log', AsyncMock())
    collect = AsyncMock()
    monkeypatch.setattr(agent.search_pipeline, 'collect_all', collect)
    result = asyncio.run(agent.do_search())
    assert result['applied'] == result['skipped'] == 0
    assert result['note'] == str(blocked)
    collect.assert_not_awaited()
    client.stop.assert_awaited_once()
    assert not seen.all_entries()
    history = [json.loads(line) for line in (tmp_path / 'history.jsonl').read_text().splitlines()]
    assert history[-1]['error'] == 'hh_unexpected_ui'


def test_details_and_collector_preserve_ui_stop_signal(monkeypatch):
    import apply_orchestrator
    import search_pipeline
    blocked = HHUnexpectedUI('synthetic', 'a' * 64)
    client = SimpleNamespace(get_vacancy_details=AsyncMock(side_effect=blocked))
    with pytest.raises(HHUnexpectedUI):
        asyncio.run(apply_orchestrator.fetch_vacancy_details({'source': 'hh', 'url': 'https://hh.ru/vacancy/1'}, client))
    monkeypatch.setattr(search_pipeline, 'collect_hh_vacancies', AsyncMock(side_effect=blocked))
    with pytest.raises(HHUnexpectedUI):
        asyncio.run(search_pipeline.collect_all(client, None, None, None))
