"""Offline native GeekJob account/submit regression tests; synthetic transport."""
import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import geekjob_client as geek
from state_store.json_store import atomic_write_json


def cookies(value='original'):
    return [{'name': 'session', 'value': value, 'domain': '.geekjob.ru', 'path': '/'}]


@pytest.fixture
def native(tmp_path, monkeypatch):
    path = tmp_path / 'cookies.json'
    atomic_write_json(path, cookies())
    monkeypatch.setattr(geek.config, 'GEEKJOB_COOKIES_FILE', str(path))
    monkeypatch.setattr(geek.config, 'HH_STATE_DIR', str(tmp_path / 'state'))
    monkeypatch.setattr(geek.config, 'GEEKJOB_BASE_URL', 'https://geekjob.ru')
    monkeypatch.setattr(geek.config, 'GEEKJOB_RESUME_ID', 'cv-a')
    client = geek.GeekJobClient()
    monkeypatch.setattr(client, 'start', AsyncMock())
    monkeypatch.setattr(client, '_get_text', AsyncMock(return_value='window.Vacancy = {"id":"v1","position":"QA","ic":"i","ci":"c"};'))
    requests = []
    behavior = {'after_get': None, 'post_error': None, 'get_payload': None,
                'post_payload': None, 'post_status': 200}
    class Response:
        status = 200
        def __init__(self, method):
            self.method = method
            self.status = behavior['post_status'] if method == 'POST' else 200
        async def __aenter__(self): return self
        async def __aexit__(self, *args): pass
        async def text(self):
            await asyncio.sleep(0)
            if self.method == 'GET':
                if behavior['after_get']: behavior['after_get']()
                return json.dumps(behavior['get_payload'] if behavior['get_payload'] is not None else {'error': False, 'responded': False,
                                   'user': {'id': 'account-a', 'email': 'original@example.test'},
                                   'data': [{'id': 'cv-a', 'public': True}]})
            if behavior['post_error']: raise behavior['post_error']
            return json.dumps(behavior['post_payload'] if behavior['post_payload'] is not None else {'error': False, 'message': 'Synthetic accepted'})
    class Session:
        closed = False
        def request(self, method, url, **kwargs):
            requests.append((method, url, kwargs))
            return Response(method)
    client._session = Session()
    vacancy = {'id': 'v1', 'source': 'geekjob', 'url': 'https://geekjob.ru/vacancy/v1', 'title': 'QA'}
    return SimpleNamespace(client=client, path=path, requests=requests, behavior=behavior,
                           vacancy=vacancy, monkeypatch=monkeypatch)


def test_configured_resume_absent_never_silently_selects_first(native):
    assert native.client._select_resume([{'id': 'other-cv'}]) is None


def test_resume_target_is_captured_with_constructed_profile(native):
    native.monkeypatch.setattr(geek.config, 'GEEKJOB_RESUME_ID', 'other-cv')
    assert native.client._select_resume([{'id': 'other-cv'}, {'id': 'cv-a'}])['id'] == 'cv-a'


def test_cookie_header_rejects_lookalike_domain():
    assert geek._cookie_header([{'name': 'secret', 'value': 'private', 'domain': 'evil-geekjob.ru'}]) == ''


def test_changed_cookie_cache_cannot_reuse_old_apply_context(native):
    asyncio.run(native.client._get_apply_context(native.vacancy['url']))
    atomic_write_json(native.path, cookies('new-account'))
    with pytest.raises(RuntimeError):
        asyncio.run(native.client._get_apply_context(native.vacancy['url']))


def test_native_post_does_not_replay_after_proxy_failure(native):
    request = AsyncMock(side_effect=OSError('synthetic proxy failure'))
    native.monkeypatch.setattr(native.client, '_request_json_once', request)
    native.monkeypatch.setattr(geek.proxy_utils, 'is_proxy_error', lambda exc: True)
    with pytest.raises(OSError):
        asyncio.run(native.client._request_json('POST', '/json/respond/vacancy'))
    assert request.await_count == 1


def test_cookie_change_during_get_blocks_post_instead_of_using_other_account(native):
    native.behavior['after_get'] = lambda: atomic_write_json(native.path, cookies('other-account'))
    result = asyncio.run(native.client.apply_to_vacancy(native.vacancy, 'Original cover'))
    assert result['ok'] is False
    assert not [request for request in native.requests if request[0] == 'POST']


@pytest.mark.parametrize('browser_present', [False, True])
def test_different_effective_get_post_accounts_never_submit(native, browser_present):
    split_cookies = [
        {'name': 'session', 'value': 'account-a', 'domain': '.geekjob.ru', 'path': '/json/mycvlist'},
        {'name': 'session', 'value': 'account-b', 'domain': '.geekjob.ru', 'path': '/json/respond'},
    ]
    atomic_write_json(native.path, split_cookies)
    if browser_present:
        # Failure must happen before even inspecting a browser agreeing with B.
        native.client._context = SimpleNamespace(cookies=AsyncMock(return_value=split_cookies))
    assert asyncio.run(native.client.apply_to_vacancy(native.vacancy, 'Original cover'))['ok'] is False
    assert not [request for request in native.requests if request[0] == 'POST']


def test_concurrent_native_apply_has_one_durable_post_owner(native):
    async def run():
        return await asyncio.gather(*(native.client.apply_to_vacancy(dict(native.vacancy), 'Original cover') for _ in range(8)))
    results = asyncio.run(run())
    assert sum(result['ok'] and not result.get('already_applied') for result in results) == 1
    assert len([request for request in native.requests if request[0] == 'POST']) == 1


def test_uncertain_post_is_not_automatically_retried(native):
    native.behavior['post_error'] = TimeoutError('synthetic timeout')
    result = asyncio.run(native.client.apply_to_vacancy(native.vacancy, 'Original cover'))
    assert result['ok'] is False and result.get('submission_status') == 'uncertain'
    native.behavior['post_error'] = None
    again = asyncio.run(native.client.apply_to_vacancy(native.vacancy, 'Original cover'))
    assert again['ok'] is False
    assert len([request for request in native.requests if request[0] == 'POST']) == 1


@pytest.mark.parametrize('alias', ['?utm=x', '#section', '/', '?utm=x#section'])
def test_same_remote_vacancy_alias_cannot_replay_uncertain_post(native, alias):
    native.behavior['post_error'] = TimeoutError('synthetic timeout')
    assert asyncio.run(native.client.apply_to_vacancy(native.vacancy, 'Original cover'))['submission_status'] == 'uncertain'
    aliased = {**native.vacancy, 'url': native.vacancy['url'] + alias}
    native.behavior['post_error'] = None
    assert asyncio.run(native.client.apply_to_vacancy(aliased, 'Original cover'))['ok'] is False
    assert len([request for request in native.requests if request[0] == 'POST']) == 1


@pytest.mark.parametrize('point', ['url', 'approved_id', 'ssr'])
def test_approved_vacancy_id_must_match_url_and_ssr_before_post(native, point):
    if point == 'url': native.vacancy['url'] = 'https://geekjob.ru/vacancy/other'
    if point == 'approved_id': native.vacancy['id'] = 'other'
    if point == 'ssr': native.monkeypatch.setattr(native.client, '_get_text', AsyncMock(return_value='window.Vacancy = {"id":"other"};'))
    assert asyncio.run(native.client.apply_to_vacancy(native.vacancy, 'Original cover'))['ok'] is False
    assert not [request for request in native.requests if request[0] == 'POST']


def test_native_success_uses_frozen_account_exact_cv_and_nonredirecting_tls(native):
    result = asyncio.run(native.client.apply_to_vacancy(native.vacancy, 'Original cover'))
    assert result['ok'] is True and result['resume_selection_verified'] is True
    post, = [request for request in native.requests if request[0] == 'POST']
    assert post[2]['headers']['Cookie'] == 'session=original'
    assert post[2]['json']['cid'] == 'cv-a' and post[2]['json']['vid'] == 'v1'
    assert post[2]['json']['text'].startswith('Original cover')
    assert post[2]['ssl'] is True and post[2]['allow_redirects'] is False
    assert native.client._apply_repository.path.stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize('cv', [{}, {'id': None}, {'id': True}, {'id': 0}, {'id': -1}, {'id': ' cv-a '}])
def test_implicit_resume_selection_cannot_accept_invalid_id(native, cv):
    native.client._resume_id = ''
    assert native.client._select_resume([cv]) is None


@pytest.mark.parametrize('changes', [{'responded': 'false'}, {'responded': 1}, {'error': 'false'},
                                     {'data': {}}, {'user': []}, {'user': {'id': True}}, {'user': {}}])
def test_malformed_get_schema_never_marks_applied_or_sends(native, changes):
    native.behavior['get_payload'] = {'error': False, 'responded': False,
        'data': [{'id': 'cv-a'}], 'user': {'id': 'account-a'}, **changes}
    result = asyncio.run(native.client.apply_to_vacancy(native.vacancy, 'Original cover'))
    assert result['ok'] is False and not result.get('already_applied')
    assert native.client._apply_repository.get(native.vacancy['url']).get('status') != 'completed'
    assert not [request for request in native.requests if request[0] == 'POST']


@pytest.mark.parametrize('url', ['http://geekjob.ru/json/mycvlist', 'https://other.invalid/json/mycvlist',
                                 'https://user:secret@geekjob.ru/json/mycvlist'])
def test_authenticated_api_rejects_insecure_or_foreign_origin_without_transport(native, url):
    with pytest.raises(RuntimeError): asyncio.run(native.client._request_json_once('GET', url))
    assert native.requests == []


@pytest.mark.parametrize('status', [301, 302, 307, 308, 401, 500])
def test_post_redirect_or_failed_http_is_uncertain_without_replay(native, status):
    native.behavior['post_status'] = status
    result = asyncio.run(native.client.apply_to_vacancy(native.vacancy, 'Original cover'))
    assert result['ok'] is False and result['submission_status'] == 'uncertain'
    again = asyncio.run(native.client.apply_to_vacancy(native.vacancy, 'Original cover'))
    assert again['ok'] is False
    assert len([request for request in native.requests if request[0] == 'POST']) == 1


def test_actual_aiohttp_dummy_jar_cannot_override_frozen_manual_cookie(native):
    import aiohttp
    from yarl import URL
    async def run():
        native.client._session = None
        await geek.GeekJobClient.start(native.client)
        try:
            session = native.client._session
            assert isinstance(session.cookie_jar, aiohttp.DummyCookieJar)
            origin = URL('https://geekjob.ru/json/respond/vacancy')
            session.cookie_jar.update_cookies({'session': 'other-account'}, response_url=origin)
            request = aiohttp.ClientRequest('POST', origin, headers={'Cookie': 'session=original'},
                                           cookies=session.cookie_jar.filter_cookies(origin))
            assert request.headers['Cookie'] == 'session=original'
        finally:
            await native.client.stop()
    asyncio.run(run())


@pytest.mark.parametrize('cookie', [
    {'name': 'other', 'value': 'private', 'domain': 'other.geekjob.ru', 'path': '/'},
    {'name': 'other', 'value': 'private', 'domain': '.geekjob.ru', 'path': '/private'},
    {'name': 'other', 'value': 'private', 'domain': '.geekjob.ru', 'path': '/json/responded'},
])
def test_cookie_projection_matches_actual_api_host_and_path(cookie):
    assert geek._cookie_header([cookie], url='https://geekjob.ru/json/respond/vacancy') == ''


def test_ambiguous_same_name_cookie_identity_blocks_instead_of_guessing():
    with pytest.raises(RuntimeError, match='Ambiguous'):
        geek._cookie_header(cookies() + cookies('other-account'))


def test_mutable_approval_change_during_get_does_not_send_stale_approved_vacancy(native):
    native.behavior['after_get'] = lambda: native.vacancy.update(title='Changed vacancy')
    assert asyncio.run(native.client.apply_to_vacancy(native.vacancy, 'Original cover'))['ok'] is False
    assert not [request for request in native.requests if request[0] == 'POST']


def test_browser_account_replacement_during_transport_preparation_blocks(native):
    calls = []
    async def start(**kwargs):
        calls.append(True)
        if len(calls) == 2:
            native.client._context = object()  # Different unbound browser at last preparation.
    native.monkeypatch.setattr(native.client, 'start', start)
    assert asyncio.run(native.client.apply_to_vacancy(native.vacancy, 'Original cover'))['ok'] is False
    assert not [request for request in native.requests if request[0] == 'POST']


def test_post_cancellation_remains_sticky_uncertain_and_propagates(native):
    native.behavior['post_error'] = asyncio.CancelledError()
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(native.client.apply_to_vacancy(native.vacancy, 'Original cover'))
    assert native.client._apply_repository.get(native.vacancy['url'])['status'] == 'uncertain'
    native.behavior['post_error'] = None
    assert asyncio.run(native.client.apply_to_vacancy(native.vacancy, 'Original cover'))['ok'] is False
    assert len([request for request in native.requests if request[0] == 'POST']) == 1


@pytest.mark.parametrize('write_number', [1, 2, 3])
def test_durable_submit_write_failure_does_not_invent_success_or_retry(native, write_number):
    store = native.client._apply_repository.store
    original, writes = store._save_unlocked, []
    def fail(value):
        writes.append(True)
        if len(writes) >= write_number: raise OSError('synthetic write failure')
        original(value)
    native.monkeypatch.setattr(store, '_save_unlocked', fail)
    result = asyncio.run(native.client.apply_to_vacancy(native.vacancy, 'Original cover'))
    assert result['ok'] is False
    assert len([request for request in native.requests if request[0] == 'POST']) == (1 if write_number == 3 else 0)


def test_raw_api_error_or_transport_exception_never_leaks_to_notice(native, caplog):
    native.behavior['get_payload'] = {'error': True, 'message': 'synthetic-private-API-cookie'}
    ready, detail = asyncio.run(native.client.is_auto_apply_ready(native.vacancy['url']))
    assert ready is False and 'synthetic-private' not in detail + caplog.text


def _claim_worker(args):
    from state_store.geekjob_apply import GeekJobApplyRepository
    path, index = args
    return GeekJobApplyRepository(path).claim('https://geekjob.ru/vacancy/v1?utm=' + str(index), 'session', 'approval') is not None


@pytest.mark.parametrize('processes', [False, True])
def test_new_submit_state_machine_has_one_cross_client_owner(tmp_path, processes):
    import concurrent.futures
    import multiprocessing
    args = [(str(tmp_path / 'cookies.json'), index) for index in range(24)]
    pool = (concurrent.futures.ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context('spawn'))
            if processes else concurrent.futures.ThreadPoolExecutor(max_workers=24))
    with pool: results = list(pool.map(_claim_worker, args))
    assert sum(results) == 1
