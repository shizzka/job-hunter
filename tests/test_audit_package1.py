"""Offline reproductions of A1/A3; fake clicks, temporary durable state."""
import asyncio
import multiprocessing
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import agent
import config
import hh_client
import manual_apply_queue as queue
from hh import apply as hh_apply
from habr_career_client import HabrCareerClient
from tests.test_hh_resume_target_safety import ApplyPage


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(config, 'HH_COOKIES_FILE', str(tmp_path / 'hh_cookies.json'))
    monkeypatch.setattr(config, 'HABR_COOKIES_FILE', str(tmp_path / 'habr_cookies.json'))
    monkeypatch.setattr(config, 'HH_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setattr(queue, '_queue_path', lambda profile_name=None: tmp_path / 'queue.json')
    monkeypatch.setattr(agent.company_blacklist, 'is_blocked', lambda *a: False)
    return tmp_path


def hh_session(page):
    client = hh_client.HHClient()
    client._page = page
    for name, value in [('_save_debug_snapshot', None), ('_detect_anti_bot_kind', None),
                        ('_handle_anti_bot_with_solver', None), ('_page_closed_or_archived', False),
                        ('_has_existing_response_ui', False), ('_dismiss_magritte_dropdowns', None),
                        ('_response_requires_questions', False), ('_inspect_employer_questions', {'fields': []})]:
        setattr(client, name, AsyncMock(return_value=value))
    client._apply_success_detected = AsyncMock(return_value=False)
    client._detect_response_controls = AsyncMock(side_effect=lambda: (page.url, object(), False, None, page.letter, page.submit))
    return client


async def apply_hh(client):
    return await hh_apply.apply_to_vacancy(client, 'https://hh.ru/vacancy/1', 'letter',
        preferred_resume_id='target', absolute_hh_url=lambda x: x,
        anti_bot_message=lambda *a: '', logger=hh_client.log)


@pytest.mark.parametrize('failure', ['lost', 'wait_timeout', 'cancel'])
def test_hh_possible_click_never_replays_on_this_or_next_run(isolated, failure):
    page = ApplyPage()
    dom_calls = []
    original_evaluate = page.evaluate
    async def evaluate(script, expected=None):
        if 'form.requestSubmit' in script:
            dom_calls.append('submit')
        return await original_evaluate(script, expected)
    page.evaluate = evaluate
    async def wait(ms):
        if page.sent:
            if failure == 'wait_timeout': raise TimeoutError('post-click lost')
            if failure == 'cancel': raise asyncio.CancelledError()
    page.wait_for_timeout = wait
    client = hh_session(page)
    if failure == 'cancel':
        with pytest.raises(asyncio.CancelledError): asyncio.run(apply_hh(client))
    else:
        result = asyncio.run(apply_hh(client))
        assert not result['ok'] and result['uncertain']
    assert page.submit.attempts == 1
    assert not dom_calls
    asyncio.run(apply_hh(hh_session(page)))
    assert page.submit.attempts == 1
    assert not dom_calls


@pytest.mark.parametrize('source', ['hh', 'habr'])
def test_post_click_wait_cannot_try_another_strategy(isolated, source):
    page = ApplyPage()
    async def wait(ms):
        if page.sent: raise TimeoutError('confirmation unavailable')
    page.wait_for_timeout = wait
    client = hh_session(page) if source == 'hh' else HabrCareerClient()
    client._page = page
    try:
        asyncio.run(client._click_with_fallbacks(page.submit, 'submit_button'))
    except TimeoutError:
        pass
    assert page.submit.attempts == 1


class LostResponse:
    async def __aenter__(self): return self
    async def __aexit__(self, *args):
        if args[0] is not None: return False
        raise TimeoutError('response lost after click')


@pytest.mark.parametrize('failure', ['lost', 'cancel'])
def test_habr_lost_response_is_durable_and_never_second_submit(isolated, failure):
    page = ApplyPage()
    apply_button = SimpleNamespace(click=AsyncMock(), evaluate=AsyncMock())
    async def query(selector):
        if 'textarea' in selector: return page.letter
        if "'Отправить'" in selector: return page.submit
        if "'Откликнуться'" in selector: return apply_button
        return None
    page.query_selector = query
    page.expect_response = lambda *a, **kw: LostResponse()
    if failure == 'cancel':
        async def click(**kw):
            page.submit.attempts += 1
            raise asyncio.CancelledError()
        page.submit.click = click
    client = HabrCareerClient()
    client._page = page
    client._page_is_logged_in = AsyncMock(return_value=True)
    if failure == 'cancel':
        with pytest.raises(asyncio.CancelledError):
            asyncio.run(client.apply_to_vacancy('https://career.habr.com/vacancies/1', 'letter'))
    else:
        result = asyncio.run(client.apply_to_vacancy('https://career.habr.com/vacancies/1', 'letter'))
        assert not result['ok'] and result['uncertain']
    assert page.submit.attempts == 1
    asyncio.run(client.apply_to_vacancy('https://career.habr.com/vacancies/1', 'letter'))
    assert page.submit.attempts == 1


@pytest.fixture
def manual(isolated, monkeypatch):
    token = queue.create_candidate({'source': 'hh', 'id': '1', 'url': 'https://hh.ru/vacancy/1'},
                                   {'score': 55}, details='Synthetic description')['token']
    class Client:
        async def start(self): pass
        async def is_logged_in(self): return True
        async def stop(self): pass
    monkeypatch.setattr(agent, 'HHClient', Client)
    monkeypatch.setattr(agent.hh_guard, 'can_auto_apply', lambda: (True, ''))
    monkeypatch.setattr(agent, 'generate_cover_letter', AsyncMock(return_value='letter'))
    monkeypatch.setattr(agent, 'notify_needs_manual', AsyncMock())
    monkeypatch.setattr(agent.analytics, 'record_decision', lambda **kw: None)
    return token


def test_manual_two_processes_one_token_exactly_one_dispatch(manual, isolated, monkeypatch):
    ctx = multiprocessing.get_context('fork')
    gate = ctx.Barrier(2)
    dispatches = isolated / 'dispatches'
    async def cover(*a):
        await asyncio.sleep(0.05)
        return 'letter'
    async def dispatch(*a, **kw):
        with dispatches.open('a') as stream: stream.write('dispatch\n')
        return {'ok': False, 'message': 'offline'}
    monkeypatch.setattr(agent, 'generate_cover_letter', cover)
    monkeypatch.setattr(agent.apply_orchestrator, 'dispatch_apply', dispatch)
    def worker():
        gate.wait()
        asyncio.run(agent.do_manual_apply_token(manual))
    processes = [ctx.Process(target=worker) for _ in range(2)]
    for p in processes: p.start()
    for p in processes:
        p.join(10)
        assert p.exitcode == 0
    assert dispatches.read_text().splitlines() == ['dispatch']


def test_manual_revoked_during_model_await_cannot_dispatch(manual, monkeypatch):
    async def cover(*a):
        queue.record_feedback(manual, 'bad')
        return 'letter'
    dispatch = AsyncMock(return_value={'ok': False})
    monkeypatch.setattr(agent, 'generate_cover_letter', cover)
    monkeypatch.setattr(agent.apply_orchestrator, 'dispatch_apply', dispatch)
    asyncio.run(agent.do_manual_apply_token(manual))
    dispatch.assert_not_awaited()


def test_manual_cancel_after_possible_dispatch_is_not_pending(manual, monkeypatch):
    monkeypatch.setattr(agent.apply_orchestrator, 'dispatch_apply', AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError): asyncio.run(agent.do_manual_apply_token(manual))
    assert queue.get_candidate(manual)['status'] == 'uncertain'
    assert not asyncio.run(agent.do_manual_apply_token(manual))['ok']


def test_manual_owner_cannot_finalize_a_new_attempt(manual):
    owner = queue.claim_candidate(manual)['owner']
    assert queue.finish_candidate(manual, owner, 'pending')
    newer = queue.claim_candidate(manual)['owner']
    assert newer != owner
    assert not queue.finish_candidate(manual, owner, 'applied')
    assert queue.get_candidate(manual)['owner'] == newer


def test_single_boundary_with_real_offline_chromium_click(isolated):
    from playwright.async_api import async_playwright
    from state_store.native_apply import NativeApplyRepository, run_native_attempt
    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            try:
                context = await browser.new_context()
                await context.route('**/*', lambda route: route.abort())
                page = await context.new_page()
                await page.set_content('<button onclick="window.submits=(window.submits||0)+1">Submit</button>')
                client = hh_client.HHClient()
                client._page = page
                button = await page.query_selector('button')
                original_wait = page.wait_for_timeout
                async def wait(ms):
                    if await page.evaluate('() => window.submits || 0'):
                        raise TimeoutError('post-action transport lost')
                    await original_wait(0)
                page.wait_for_timeout = wait
                repo = NativeApplyRepository(config.HH_COOKIES_FILE, 'hh')
                async def operation():
                    await client._click_with_fallbacks(button, 'submit_button')
                    return {'ok': False}
                first = await run_native_attempt(client, repo, 'https://hh.ru/vacancy/2', operation)
                second = await run_native_attempt(client, repo, 'https://hh.ru/vacancy/2', operation)
                assert first['uncertain'] and second['uncertain']
                assert repo.get('https://hh.ru/vacancy/2')['status'] == 'uncertain'
                assert await page.evaluate('() => window.submits') == 1
            finally:
                await browser.close()
    asyncio.run(run())
