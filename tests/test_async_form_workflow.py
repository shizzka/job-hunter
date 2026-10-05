"""Synthetic async submit/approval/prompt races: no real browser or transports."""
import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import google_form_filler as gforms
from google_forms import drafts
from runtime_context import RuntimePaths
from state_store.google_forms import GoogleFormStateRepository
from tests.test_google_form_drafts import FakeBot

TOKEN, NEXT = 'abcdef123456', '123456abcdef'


def preview(token=TOKEN, url='https://docs.google.com/forms/d/e/synthetic/viewform'):
    return {'token': token, 'status': 'preview', 'ok': True, 'form_url': url,
            'questions': [{'index': 0, 'question': 'Synthetic name', 'type': 'text', 'required': True,
                           'page_index': 0, 'page_question_index': 0}],
            'answers': [{'index': 0, 'answer': 'Synthetic answer', 'confidence': 'high'}],
            'fill_result': {'filled': [{'index': 0}], 'skipped': []}, 'pages_total': 1}


@pytest.fixture
def flow(tmp_path, monkeypatch):
    paths = RuntimePaths(str(tmp_path), str(tmp_path / 'state'), str(tmp_path / 'resume.md'))
    repo = GoogleFormStateRepository(tmp_path)
    repo.remember(TOKEN, preview(), trim_expired=False)
    page = SimpleNamespace(url=preview()['form_url'], goto=AsyncMock(), wait_for_timeout=AsyncMock())
    client = SimpleNamespace(_page=page, start=AsyncMock())
    monkeypatch.setattr(gforms, '_fill_google_form_email_consent', AsyncMock(return_value=False))
    monkeypatch.setattr(gforms, 'extract_form_questions', AsyncMock(return_value=preview()['questions']))
    monkeypatch.setattr(gforms, 'fill_form', AsyncMock(return_value=preview()['fill_result']))
    monkeypatch.setattr(gforms, '_has_google_form_submit_button', AsyncMock(return_value=True))
    monkeypatch.setattr(gforms, '_wait_google_form_submit_success', AsyncMock(return_value=(True, 'Synthetic recorded')))
    monkeypatch.setattr(gforms, '_safe_screenshot', AsyncMock())
    clicks = []
    async def click(page, *, before_click=None, on_no_action=None, approval_id=None):
        if before_click:
            before_click()
        clicks.append('click')
        return True
    monkeypatch.setattr(gforms, '_click_google_form_submit', click)
    return paths, repo, client, clicks


def test_two_sessions_submit_once(flow):
    paths, repo, client, clicks = flow
    async def navigate(*args, **kwargs):
        await asyncio.sleep(.02)
    client._page.goto.side_effect = navigate
    async def run():
        return await asyncio.gather(*(gforms.submit_saved_preview(client, TOKEN, runtime_paths=paths) for _ in range(2)))
    results = asyncio.run(run())
    assert len(clicks) == 1 and sum(bool(r['ok']) for r in results) == 1
    assert repo.load()['items'][TOKEN]['status'] == 'submitted'


def test_submit_keeps_preview_added_during_browser_await(flow):
    paths, repo, client, _ = flow
    async def navigate(*args, **kwargs):
        repo.remember(NEXT, preview(NEXT, 'https://docs.google.com/forms/d/e/other/viewform'), trim_expired=False)
    client._page.goto.side_effect = navigate
    assert asyncio.run(gforms.submit_saved_preview(client, TOKEN, runtime_paths=paths))['ok']
    assert NEXT in repo.load()['items']


def test_active_submit_blocks_edits_and_supersede(flow):
    paths, _, client, _ = flow
    async def navigate(*args, **kwargs):
        with pytest.raises(ValueError, match='отправ|выполня'):
            drafts.save_answer(paths.home_dir, TOKEN, 0, 'Changed', 7)
        with pytest.raises(ValueError, match='отправ|выполня'):
            drafts.supersede(paths.home_dir, TOKEN, NEXT)
    client._page.goto.side_effect = navigate
    assert asyncio.run(gforms.submit_saved_preview(client, TOKEN, runtime_paths=paths))['ok']


def test_cancel_after_possible_click_is_uncertain_and_not_retryable(flow, monkeypatch):
    paths, repo, client, clicks = flow
    async def verification(*args, **kwargs):
        raise asyncio.CancelledError()
    monkeypatch.setattr(gforms, '_wait_google_form_submit_success', verification)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(gforms.submit_saved_preview(client, TOKEN, runtime_paths=paths))
    assert repo.load()['items'][TOKEN]['status'] == 'submit_uncertain'
    assert not asyncio.run(gforms.submit_saved_preview(client, TOKEN, runtime_paths=paths))['ok']
    assert len(clicks) == 1


def test_click_timeout_is_uncertain(flow, monkeypatch):
    paths, repo, client, _ = flow
    async def click(page, *, before_click=None, on_no_action=None, approval_id=None):
        if before_click:
            before_click()
        raise TimeoutError('Synthetic click timeout')
    monkeypatch.setattr(gforms, '_click_google_form_submit', click)
    with pytest.raises(TimeoutError):
        asyncio.run(gforms.submit_saved_preview(client, TOKEN, runtime_paths=paths))
    assert repo.load()['items'][TOKEN]['status'] == 'submit_uncertain'


def test_recheck_cannot_approve_edits_made_during_preview(flow, monkeypatch):
    from commands import google_forms as commands
    paths, repo, client, _ = flow
    client.stop = AsyncMock()
    monkeypatch.setattr(commands, 'HHClient', lambda: client)
    monkeypatch.setattr(gforms, '_runtime_paths', lambda: paths)
    ready = deepcopy(preview(NEXT))
    async def recheck(*args, **kwargs):
        drafts.save_answer(paths.home_dir, TOKEN, 0, 'Changed during await', 7)
        if kwargs.get('persist', True):
            repo.remember(NEXT, ready, trim_expired=False)
        return ready
    monkeypatch.setattr(gforms, 'preview_form', recheck)
    submit = AsyncMock(return_value={'ok': True})
    monkeypatch.setattr(gforms, 'submit_saved_preview', submit)
    from google_forms.workflow import FormWorkflow
    _, _, revision = FormWorkflow(paths.home_dir).capture(TOKEN)
    with pytest.raises(ValueError, match='измен|верси'):
        asyncio.run(commands.recheck(TOKEN, profile_name='qa', submit_after=True, approval_revision=revision))
    submit.assert_not_awaited()
    assert not drafts.edits_store(paths.home_dir).load()[TOKEN].get('superseded_by')
    assert NEXT not in repo.load()['items']
    client.stop.assert_awaited_once()


def test_late_field_prompt_does_not_replace_newer_prompt(flow):
    paths, _, _, _ = flow
    bot = FakeBot(paths.home_dir)
    principal = {'user_id': 7, 'role': 'admin'}
    async def run():
        entered, finish = asyncio.Event(), asyncio.Event()
        count = 0
        async def send(*args, **kwargs):
            nonlocal count
            count += 1
            if count == 1:
                entered.set()
                await finish.wait()
                return {'message_id': 101}
            return {'message_id': 102}
        bot._send_text = send
        first = asyncio.create_task(bot._ask_form_field(7, principal, 'qa', TOKEN, 0))
        await entered.wait()
        await bot._ask_form_field(7, principal, 'qa', TOKEN, 0)
        finish.set()
        await first
    asyncio.run(run())
    assert bot._load_state()['form_pending']['7:7']['prompt_id'] == 102


def test_cancel_during_preparation_is_failed_not_uncertain(flow):
    paths, repo, client, clicks = flow
    client._page.goto.side_effect = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(gforms.submit_saved_preview(client, TOKEN, runtime_paths=paths))
    assert repo.load()['items'][TOKEN]['status'] == 'submit_failed'
    assert not clicks


def test_notify_cancellation_does_not_reopen_completed_submit(flow, monkeypatch):
    paths, repo, client, clicks = flow
    monkeypatch.setattr(gforms, 'notify_form_submit', AsyncMock(side_effect=asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(gforms.submit_saved_preview(client, TOKEN, notify=True, runtime_paths=paths))
    assert repo.load()['items'][TOKEN]['status'] == 'submitted' and len(clicks) == 1


def test_original_paths_survive_profile_switch_while_preparing(flow, monkeypatch):
    paths, repo, client, _ = flow
    async def navigate(*args, **kwargs):
        monkeypatch.setattr(gforms.config, 'JOB_HUNTER_HOME', paths.home_dir + '/other')
        monkeypatch.setattr(gforms.config, 'HH_STATE_DIR', paths.home_dir + '/other/state')
    client._page.goto.side_effect = navigate
    monkeypatch.setattr(gforms, '_runtime_paths', lambda: paths)
    assert asyncio.run(gforms.submit_saved_preview(client, TOKEN))['ok']
    assert repo.load()['items'][TOKEN]['status'] == 'submitted'
    from pathlib import Path
    assert not Path(paths.home_dir + '/other').exists()


@pytest.mark.parametrize('allowed', [True, False])
def test_submit_button_guard_runs_after_last_scroll_await(monkeypatch, allowed):
    from google_forms import filling
    order = []
    async def scroll(**kwargs):
        order.append('scroll')
    async def evaluate(script, boundary_id=None):
        if 'codex:google-form-bind' in script:
            assert order == ['scroll']
            order.append('bind')
        elif 'codex:action-dispatch' in script:
            order.append('dispatch')
            return {'id':boundary_id['id'],'dispatched':True,'ok':True}
        else:
            assert 'codex:action-ready' in script
        return True
    button = SimpleNamespace(scroll_into_view_if_needed=scroll, wait_for_element_state=AsyncMock(), evaluate=evaluate)
    page = SimpleNamespace(_jh_form_plan_id='owned', evaluate=AsyncMock(), wait_for_load_state=AsyncMock(), wait_for_timeout=AsyncMock())
    monkeypatch.setattr(filling, '_find_google_form_button', AsyncMock(return_value=button))
    def guard():
        assert order == ['scroll', 'bind']
        order.append('claim')
        return allowed
    assert asyncio.run(filling._click_google_form_submit(page, before_click=guard, approval_id='owned')) is allowed
    assert order.count('dispatch') == int(allowed)


def test_claim_storage_error_never_reaches_browser(flow, monkeypatch):
    paths, repo, client, clicks = flow
    from state_store import json_store
    def fail(*args, **kwargs):
        raise OSError('Synthetic claim storage failure')
    monkeypatch.setattr(json_store.os, 'replace', fail)
    with pytest.raises(OSError):
        asyncio.run(gforms.submit_saved_preview(client, TOKEN, runtime_paths=paths))
    client._page.goto.assert_not_awaited()
    assert not clicks
