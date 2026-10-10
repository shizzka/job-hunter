"""Injected callbacks must not bypass native validation and ownership checks."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import client_hh_auth as auth
from hh import browser
from state_store.hh_cookies import HHCookieStateError
from tests.test_hh_cookie_ownership import lifecycle, cookies
from tests.test_hh_browser import terminate_fake_browser
from tests.test_auth_state_ownership import importing


@pytest.mark.parametrize('change', ['revoked', 'context', 'binding', 'invalid_cookies'])
def test_injected_writer_cannot_skip_common_native_cookie_checks(lifecycle, change):
    asyncio.run(lifecycle.start())
    called = []
    async def capture():
        if change == 'revoked': lifecycle.session._cookie_binding.revoked = True
        if change == 'context': lifecycle.session._context = object()
        if change == 'binding': lifecycle.session._cookie_binding = object()
        return {'cookies': []} if change == 'invalid_cookies' else cookies('captured')
    lifecycle.context.cookies = capture
    with pytest.raises(HHCookieStateError):
        asyncio.run(browser.save_session(lifecycle.session, save_cookies=called.append))
    assert called == []


def test_injected_shutdown_writer_cannot_erase_original_native_auth(lifecycle):
    asyncio.run(lifecycle.start())
    called = []
    lifecycle.context.cookies = AsyncMock(return_value=[])
    asyncio.run(browser.stop_browser(lifecycle.session, save_cookies=called.append,
                                    terminate=terminate_fake_browser))
    lifecycle.context.cookies.assert_awaited_once()
    assert called == []
    assert lifecycle.repo.snapshot()[0] == cookies('original')
    assert lifecycle.instance.closed and lifecycle.pw.stopped


def test_unowned_compat_resume_import_does_not_write_profile(importing):
    client, home, _ = importing
    # Existing fixture has a native declared cookie owner in the fixed tree;
    # the compatibility case intentionally has neither paths nor binding.
    for name in ('_cookie_paths', '_cookie_binding', '_context'):
        if hasattr(client, name): delattr(client, name)
    called = []
    async def ids():
        called.append(True)
        return [{'id': 'r1', 'title': 'QA'}]
    client.get_resume_ids = ids
    with pytest.raises(RuntimeError): asyncio.run(auth.import_current_hh_resumes(client, 'qa'))
    assert called == [] and (home / 'resume.md').read_text() == 'original resume'


@pytest.mark.parametrize('kind', ['code', 'login'])
@pytest.mark.parametrize('change', ['page', 'context', 'url', 'nonce', 'challenge_url', 'challenge_text'])
def test_auth_bridge_answer_cannot_fill_after_page_context_or_attempt_changes(monkeypatch, kind, change):
    class Page:
        url = 'https://hh.ru/account/login'
        def is_closed(self): return False
    page = Page()
    client = SimpleNamespace(_page=page, _context=object())
    text = 'Введите код из SMS' if kind == 'code' else 'Введите номер телефона'
    state = {'text': text}
    async def page_text(*args): return state['text']
    monkeypatch.setattr(auth, '_hh_auth_page_text', page_text)
    monkeypatch.setattr(auth, '_first_visible_locator', AsyncMock(return_value=None))
    monkeypatch.setattr(auth, '_click_first_visible', AsyncMock(return_value=False))
    monkeypatch.setattr(auth, '_has_hh_auth_captcha_marker', AsyncMock(return_value=False))
    async def value(*args, **kwargs):
        if change == 'page': client._page = Page()
        if change == 'context': client._context = object()
        if change == 'url': page.url = 'https://hh.ru/vacancy/123'
        if change == 'nonce': client._auth_step_nonce = object()
        if change == 'challenge_url': page.url = 'https://hh.ru/account/login?challenge=other'
        if change == 'challenge_text': state['text'] = text + ' для другого аккаунта'
        return '123456' if kind == 'code' else 'synthetic@example.test'
    monkeypatch.setattr(auth, '_request_hh_auth_value', value)
    fill_code, fill_login = AsyncMock(return_value=True), AsyncMock(return_value=True)
    monkeypatch.setattr(auth, '_fill_hh_auth_code', fill_code)
    monkeypatch.setattr(auth, '_fill_hh_auth_login', fill_login)
    monkeypatch.setattr(auth, '_submit_hh_auth_form', AsyncMock(return_value={'status': auth.HH_AUTH_STEP_SUBMITTED}))
    monkeypatch.setattr(auth, '_wait_for_hh_auth_progress', AsyncMock(return_value={'status': auth.HH_AUTH_STEP_PROGRESS}))
    asyncio.run(auth._drive_hh_auth_step(client, 'qa', timeout_s=1, poll_sec=0.01))
    assert fill_code.await_count == 0 and fill_login.await_count == 0


@pytest.mark.parametrize('change', ['lookup_fill', 'lookup_submit', 'readback', 'enter', 'challenge_during_guard', 'positive'])
@pytest.mark.parametrize('kind', ['code', 'login'])
def test_native_auth_helpers_recheck_after_lookup_and_read_back_exact_value(monkeypatch, kind, change):
    """Run real fill/submit helpers, not AsyncMock substitutes for side effects."""
    calls = []
    class Page:
        url = 'https://hh.ru/account/login?challenge=original'
        def is_closed(self): return False
    page = Page()
    client = SimpleNamespace(_page=page, _context=object())
    state = {'text': 'Введите код из SMS' if kind == 'code' else 'Введите номер телефона', 'received': False}
    class Item:
        value = ''
        async def fill(self, value, **kwargs):
            calls.append('fill'); self.value = value
        async def input_value(self):
            return 'different-value' if change == 'readback' else self.value
        async def click(self, **kwargs): calls.append('click')
    item = Item()
    async def lookup(current_page, selectors):
        if selectors == auth.HH_AUTH_PASSWORD_INPUT_SELECTORS and state['received'] and change == 'challenge_during_guard':
            state['text'] += ' другой аккаунт'
        if selectors in (auth.HH_AUTH_PASSWORD_INPUT_SELECTORS, auth.HH_AUTH_CODE_MODE_SELECTORS,
                         auth.HH_AUTH_ROLE_INPUT_SELECTORS, auth.HH_AUTH_PHONE_MODE_SELECTORS):
            return None
        if selectors == auth.HH_AUTH_CONTINUE_SELECTORS:
            if change in ('lookup_submit', 'enter'): client._context = object()
            return None if change == 'enter' else item
        if change == 'lookup_fill': client._auth_step_nonce = object()
        return item
    page.keyboard = SimpleNamespace(press=AsyncMock(side_effect=lambda *a: calls.append('enter')))
    monkeypatch.setattr(auth, '_first_visible_locator', lookup)
    async def page_text(*args): return state['text']
    async def answer(*args, **kwargs):
        state['received'] = True
        return '123456' if kind == 'code' else 'synthetic@example.test'
    monkeypatch.setattr(auth, '_hh_auth_page_text', page_text)
    monkeypatch.setattr(auth, '_request_hh_auth_value', answer)
    monkeypatch.setattr(auth, '_has_hh_auth_captcha_marker', AsyncMock(return_value=False))
    monkeypatch.setattr(auth, '_wait_for_hh_auth_progress', AsyncMock(return_value={'status': auth.HH_AUTH_STEP_PROGRESS}))
    asyncio.run(auth._drive_hh_auth_step(client, 'qa', timeout_s=1, poll_sec=.01))
    if change == 'positive':
        assert calls == ['fill', 'click']
    else:
        assert 'click' not in calls and 'enter' not in calls
        if change in ('lookup_fill', 'challenge_during_guard'): assert 'fill' not in calls


def test_explicit_bound_native_writer_uses_original_repository_cas(lifecycle):
    asyncio.run(lifecycle.start())
    writer = browser.BoundCookieWriter(lifecycle.repo)
    asyncio.run(browser.save_session(lifecycle.session, save_cookies=writer))
    assert lifecycle.repo.snapshot()[0] == cookies('rotated')
    lifecycle.repo.save(cookies('new-login'))
    with pytest.raises(HHCookieStateError):
        asyncio.run(browser.save_session(lifecycle.session, save_cookies=writer))
    assert lifecycle.repo.snapshot()[0] == cookies('new-login')


def test_bound_native_writer_rejects_different_repository(lifecycle, tmp_path):
    from state_store.hh_cookies import HHCookieRepository
    asyncio.run(lifecycle.start())
    other = HHCookieRepository(tmp_path / 'other.json')
    other.save(cookies('other'))
    with pytest.raises(HHCookieStateError):
        asyncio.run(browser.save_session(lifecycle.session, save_cookies=browser.BoundCookieWriter(other)))
    assert other.snapshot()[0] == cookies('other')
    assert lifecycle.repo.snapshot()[0] == cookies('original')


@pytest.mark.parametrize('point', ['initial', 'listing', 'download', 'publication'])
def test_resume_import_rejects_same_context_browser_auth_drift(importing, point):
    client, home, _ = importing
    original = client._cookie_binding.repository.snapshot()[0]
    checks = []
    async def browser_cookies():
        checks.append(True)
        target = {'initial': 1, 'listing': 2, 'download': 3, 'publication': 4}[point]
        return cookies('different-account') if len(checks) >= target else original
    client._context.cookies = browser_cookies
    with pytest.raises(RuntimeError):
        asyncio.run(auth.import_current_hh_resumes(client, 'qa'))
    assert (home / 'resume.md').read_text() == 'original resume'
    assert not (home / 'hh_resumes.json').exists()


@pytest.mark.parametrize('changed', ['resume', 'catalog', 'env'])
def test_initial_browser_auth_await_cannot_adopt_newer_publication_inputs(importing, changed):
    from state_store.json_store import atomic_write_json
    client, home, _ = importing
    original = client._cookie_binding.repository.snapshot()[0]
    first = True
    async def browser_cookies():
        nonlocal first
        if first:
            first = False
            if changed == 'resume': (home / 'resume.md').write_text('newer resume')
            if changed == 'catalog': atomic_write_json(home / 'hh_resumes.json', [{'id': 'newer-catalog'}])
            if changed == 'env': (home / 'profile.env').write_text('NEWER=retain\n')
        return original
    client._context.cookies = browser_cookies
    with pytest.raises(RuntimeError):
        asyncio.run(auth.import_current_hh_resumes(client, 'qa'))
    assert (home / 'resume.md').read_text() == ('newer resume' if changed == 'resume' else 'original resume')
    if changed == 'env': assert (home / 'profile.env').read_text() == 'NEWER=retain\n'
    if changed == 'catalog': assert (home / 'hh_resumes.json').read_text().find('newer-catalog') >= 0


@pytest.mark.parametrize('branch', ['mode', 'role'])
@pytest.mark.parametrize('change', ['context', 'nonce', 'password', 'unchecked', 'positive'])
def test_auth_early_control_clicks_require_original_owner_and_applicant_selection(monkeypatch, branch, change):
    calls = []
    class Page:
        url = 'https://hh.ru/account/login'
        def is_closed(self): return False
        async def wait_for_timeout(self, *args): pass
    page = Page()
    client = SimpleNamespace(_page=page, _context=object())
    state = {'password': False, 'unchecked': False, 'lookups': 0}
    class Item:
        async def click(self, **kwargs): calls.append('click')
        async def is_checked(self): return not state['unchecked']
    item = Item()
    async def lookup(current_page, selectors):
        if selectors == auth.HH_AUTH_CODE_MODE_SELECTORS:
            if branch != 'mode': return None
            if change == 'context': client._context = object()
            if change == 'nonce': client._auth_step_nonce = object()
            return item
        if selectors == auth.HH_AUTH_PASSWORD_INPUT_SELECTORS:
            return item if state['password'] else None
        if selectors == auth.HH_AUTH_ROLE_INPUT_SELECTORS:
            return item if branch == 'role' else None
        if selectors == auth.HH_AUTH_ROLE_SUBMIT_SELECTORS:
            if change == 'context': client._context = object()
            if change == 'nonce': client._auth_step_nonce = object()
            if change == 'password': state['password'] = True
            if change == 'unchecked': state['unchecked'] = True
            return item
        return None
    monkeypatch.setattr(auth, '_first_visible_locator', lookup)
    monkeypatch.setattr(auth, '_hh_auth_page_text', AsyncMock(return_value='Выберите профиль соискателя'))
    monkeypatch.setattr(auth, '_wait_for_hh_auth_progress', AsyncMock(return_value={'status': auth.HH_AUTH_STEP_PROGRESS}))
    asyncio.run(auth._drive_hh_auth_step(client, 'qa', timeout_s=1, poll_sec=.01))
    allowed = change == 'positive' or (branch == 'mode' and change in ('password', 'unchecked'))
    assert calls == (['click'] if allowed else [])
