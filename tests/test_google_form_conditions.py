"""Linked forms use fresh HH evidence at preparation and final dispatch."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import config
import google_form_filler as gf
from chat_context import ChatOwner, OWN
from tests.test_async_form_workflow import flow, TOKEN
from tests.test_work_conditions import policy


@pytest.fixture
def linked(flow, monkeypatch):
    paths, repo, client, clicks = flow
    direction = {**policy(allowed_formats=["remote", "hybrid"]), "name": "qa"}
    monkeypatch.setattr(config, "ACTIVE_DIRECTION", direction, raising=False)
    monkeypatch.setattr(config, "JOB_HUNTER_HOME", paths.home_dir)
    import chat_context
    monkeypatch.setattr(chat_context, "safe_resolve", lambda *args: ChatOwner(OWN, "111", "qa"))
    item = repo.load()["items"][TOKEN]
    item.update(chat_id="chat", profile_name="synthetic", vacancy={"url": "https://hh.ru/vacancy/42"},
                application_owner={"direction": "qa", "resume_id": "111"})
    repo.remember(TOKEN, item, trim_expired=False)
    response = SimpleNamespace(url=item["vacancy"]["url"], status=200, headers={"content-type": "text/html"},
        text=AsyncMock(return_value='<div data-qa="work-formats-text">Удалённо</div>'), dispose=AsyncMock())
    context = SimpleNamespace(request=SimpleNamespace(get=AsyncMock(return_value=response)),
                              cookies=AsyncMock(return_value=[{"name": "hhtoken", "value": "synthetic", "domain": "hh.ru", "path": "/"}]))
    client._page.context = context
    return paths, repo, client, clicks, response


@pytest.mark.parametrize("markup", [
    '<div data-qa="work-formats-text">На месте работодателя</div>',
    '<div data-qa="common-employment-text">Вахта</div>', '<main>Нет условий</main>',
])
def test_preview_blocks_conflicts_and_missing_evidence_before_preparation(linked, monkeypatch, markup):
    paths, _, client, clicks, response = linked
    response.text.return_value = markup
    generate = AsyncMock()
    monkeypatch.setattr(gf, 'generate_form_answers', generate)
    result = asyncio.run(gf.preview_form(client._page, 'https://forms.gle/synthetic', chat_id='chat',
        vacancy={"url": response.url}, profile_name='synthetic', runtime_paths=paths))
    assert not result['ok'] and result['reason'] == 'work_conditions'
    generate.assert_not_awaited()
    client._page.goto.assert_not_awaited()
    assert not clicks


@pytest.mark.parametrize("final_markup", [
    '<div data-qa="work-formats-text">На месте работодателя</div>',
    '<div data-qa="common-employment-text">Вахта</div>', '<main>Нет условий</main>',
    '<div data-qa="work-formats-text">Удалённо</div>',
])
def test_submit_fetches_conditions_again_at_final_gate(linked, final_markup):
    paths, repo, client, clicks, response = linked
    response.text.side_effect = ['<div data-qa="work-formats-text">Удалённо</div>', final_markup]
    result = asyncio.run(gf.submit_saved_preview(client, TOKEN, runtime_paths=paths))
    allowed = 'Удалённо' in final_markup
    assert result['ok'] is allowed
    assert bool(clicks) is allowed
    assert client._page.context.request.get.await_count == 2
    assert repo.load()['items'][TOKEN]['status'] == ('submitted' if allowed else 'submit_failed')


def test_saved_form_without_owner_cannot_reuse_answers_after_direction_switch(linked):
    paths, repo, client, clicks, _ = linked
    item = repo.load()['items'][TOKEN]
    item.pop('application_owner')
    repo.remember(TOKEN, item, trim_expired=False)
    result = asyncio.run(gf.submit_saved_preview(client, TOKEN, runtime_paths=paths))
    assert not result['ok'] and not clicks
    assert 'заново' in result['message']
    client._page.goto.assert_not_awaited()


def test_fresh_conditions_cannot_use_cached_evidence_when_fetch_fails(linked):
    paths, _, client, clicks, _ = linked
    client._page.context.request.get.side_effect = TimeoutError('synthetic')
    result = asyncio.run(gf.submit_saved_preview(client, TOKEN, runtime_paths=paths))
    assert not result['ok'] and not clicks
    assert result['work_conditions_status'] == 'review'


def test_async_submit_guard_finishes_before_dispatch(monkeypatch):
    from google_forms import filling
    order = []
    button = SimpleNamespace(scroll_into_view_if_needed=AsyncMock(), evaluate=AsyncMock(return_value=True))
    page = SimpleNamespace(wait_for_load_state=AsyncMock(), wait_for_timeout=AsyncMock())
    monkeypatch.setattr(filling, '_find_google_form_button', AsyncMock(return_value=button))
    monkeypatch.setattr(filling, 'release_boundary', AsyncMock())
    async def dispatch(*args, **kwargs):
        order.append('dispatch')
        return True
    monkeypatch.setattr(filling, 'dispatch_approved', dispatch)
    async def guard():
        await asyncio.sleep(0)
        order.append('fresh_conditions')
        return False
    assert not asyncio.run(filling._click_google_form_submit(page, before_click=guard, approval_id='owned'))
    assert order == ['fresh_conditions']
