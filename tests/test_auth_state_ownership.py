"""Claim/IPC/import failures use synthetic local state and fake HTTP only."""
import asyncio
import concurrent.futures
import json
import multiprocessing
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

import client_hh_auth
import config
from tests.test_auth_state_regressions import credentials, mailbox, auth
from state_store.superjob_auth import SuperJobAuthSession, SuperJobAuthError, validate_auth
from state_store.prompt_mailbox import PromptMailbox, PromptStateError
from state_store.hh_resume_import import HHResumeImport
from state_store.json_store import atomic_write_json


def claim(args):
    path, = args
    try:
        SuperJobAuthSession(path).begin("refresh")
        return True
    except SuperJobAuthError:
        return False


def reply(args):
    pending, response, owner, answer = args
    try:
        PromptMailbox(pending, response).respond(owner, answer)
        return True
    except (PromptStateError, ValueError):
        return False


@pytest.mark.parametrize("processes", [False, True])
@pytest.mark.parametrize("kind", ["tokens", "prompt"])
def test_one_claim_or_reply_under_contention(tmp_path, processes, kind):
    path = tmp_path / "auth.json"
    atomic_write_json(path, auth("original"))
    pending, response = tmp_path / "pending.json", tmp_path / "response.json"
    PromptMailbox(pending, response).create({"id": "owner", "timeout_at": 10**12})
    worker = claim if kind == "tokens" else reply
    args = [(str(path),)] * 30 if kind == "tokens" else [(str(pending), str(response), "owner", str(i)) for i in range(30)]
    pool = (concurrent.futures.ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn"))
            if processes else concurrent.futures.ThreadPoolExecutor(max_workers=30))
    with pool: results = list(pool.map(worker, args))
    assert sum(results) == 1


@pytest.mark.parametrize("error", [OSError("synthetic HTTP failure"), asyncio.CancelledError()])
def test_failed_refresh_stays_uncertain_without_retry(credentials, monkeypatch, error):
    client, path, _ = credentials
    calls = []
    async def request(*a, **k): calls.append(True); raise error
    monkeypatch.setattr(client, "_request", request)
    if isinstance(error, asyncio.CancelledError):
        with pytest.raises(asyncio.CancelledError): asyncio.run(client.refresh_access_token())
    else:
        assert asyncio.run(client.refresh_access_token()) is False
    value = json.loads(path.read_text())
    assert value["_token_attempt"]["status"] == "uncertain"
    assert value["access_token"] == "original"
    assert asyncio.run(client.refresh_access_token()) is False
    assert len(calls) == 1


def test_success_retains_unknown_metadata_and_captured_app(credentials, monkeypatch):
    client, path, _ = credentials
    async def request(*a, **k):
        assert k["params"]["client_secret"] == "synthetic-app-key"
        assert k["params"]["client_id"] == 7
        return {"access_token": "fresh", "refresh_token": "rotated", "expires_in": 900}
    monkeypatch.setattr(client, "_request", request)
    monkeypatch.setattr(config, "SUPERJOB_API_KEY", "other-key")
    monkeypatch.setattr(config, "SUPERJOB_CLIENT_ID", 99)
    assert asyncio.run(client.refresh_access_token()) is True
    value = json.loads(path.read_text())
    assert value["unknown"] == "retain" and value["user"] == {"id": 7} and value["resume_id"] == 9
    assert "_token_attempt" not in value
    assert "fresh" in client._auth_header()


@pytest.mark.parametrize("point", ["claim", "finish"])
def test_token_disk_failures_do_not_invent_success(credentials, monkeypatch, point):
    client, path, _ = credentials
    requests = []
    original = client._auth_session.repository._write
    writes = 0
    def write(value):
        nonlocal writes
        writes += 1
        if writes == (1 if point == "claim" else 2): raise OSError("synthetic disk failure")
        original(value)
    async def request(*a, **k): requests.append(True); return {"access_token": "fresh", "expires_in": 900}
    monkeypatch.setattr(client._auth_session.repository, "_write", write)
    monkeypatch.setattr(client, "_request", request)
    assert asyncio.run(client.refresh_access_token()) is False
    assert len(requests) == (0 if point == "claim" else 1)
    value = json.loads(path.read_text())
    assert value["access_token"] == "original"
    if point == "finish": assert value["_token_attempt"]["status"] == "active"


def test_post_replace_token_failure_revokes_owner(credentials, monkeypatch):
    client, path, _ = credentials
    owner = client._auth_session.begin("refresh")
    original = os.fsync
    def fail(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode): raise OSError("synthetic parent fsync")
        original(fd)
    with monkeypatch.context() as scoped:
        scoped.setattr(os, "fsync", fail)
        with pytest.raises(OSError): client._update_tokens({"access_token": "fresh", "expires_in": 900}, owner=owner)
    assert json.loads(path.read_text())["access_token"] == "fresh"
    with pytest.raises(SuperJobAuthError): client._auth_header()


@pytest.mark.parametrize("value", [[], {"refresh_token": 1}, {"user": []}, {"expires_at": True}, {"expires_at": float('nan')}, {"_token_attempt": {}}])
def test_invalid_auth_schema_is_rejected(value):
    with pytest.raises(SuperJobAuthError): validate_auth(value)


def test_cookie_lock_not_held_through_refresh_await(credentials, monkeypatch):
    client, path, _ = credentials
    async def request(*a, **k):
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(client._auth_session.repository.snapshot).result(timeout=2)
        return {"access_token": "fresh", "expires_in": 900}
    monkeypatch.setattr(client, "_request", request)
    assert asyncio.run(client.refresh_access_token())


def test_late_prompt_cleanup_preserves_new_request(mailbox):
    module, create, _ = mailbox
    old, new = create(), create()
    module.complete_request(old, profile_name="qa")
    assert module.peek_pending("qa")["id"] == new


def test_captured_cleanup_does_not_follow_config_switch(mailbox, tmp_path, monkeypatch):
    module, create, _ = mailbox
    owner = create()
    pending = Path(module._pending_path("qa"))
    other = tmp_path / "other"; other.mkdir()
    monkeypatch.setattr(module, "_state_dir", lambda *a: str(other))
    module.complete_request(owner, profile_name="qa")
    assert not pending.exists()
    assert not list(other.glob('*.json'))


def test_corrupt_peer_blocks_prompt_create_without_replacing_pending(mailbox):
    module, create, _ = mailbox
    owner = create()
    pending, response = Path(module._pending_path("qa")), Path(module._response_path("qa"))
    before = pending.read_bytes(); response.write_bytes(b'corrupt')
    with pytest.raises(PromptStateError): create()
    assert pending.read_bytes() == before and response.read_bytes() == b'corrupt'


def test_foreign_profile_cannot_answer_cached_request(mailbox):
    module, create, _ = mailbox
    owner = create()
    with pytest.raises(ValueError): module.write_response(owner, "foreign", profile_name="other")


def test_expired_request_cannot_accept_a_reply(mailbox):
    module, create, _ = mailbox
    owner = create()
    path = Path(module._pending_path("qa")); value = json.loads(path.read_text()); value["timeout_at"] = 1
    atomic_write_json(path, value)
    with pytest.raises(ValueError): module.write_response(owner, "late", profile_name="qa")
    assert module.peek_pending("qa") is None


@pytest.fixture
def importing(tmp_path, monkeypatch):
    home = tmp_path / "a"; home.mkdir()
    (home / "profile.env").write_text("UNKNOWN=retain\n")
    (home / "resume.md").write_text("original resume")
    profile = SimpleNamespace(home_dir=str(home), resume_file=str(home / "resume.md"))
    monkeypatch.setattr(client_hh_auth, "_resolve_profile", lambda name: profile)
    class Client:
        async def get_resume_ids(self): return [{"id": "r1", "title": "QA Engineer"}]
        async def download_resume_by_id(self, item): return {"raw": "downloaded resume", "sections": []}
    from hh.browser import CookieBinding, CookiePaths
    from state_store.hh_cookies import HHCookieRepository
    repository = HHCookieRepository(home / "cookies.json")
    repository.save([{"name": "hhtoken", "value": "synthetic", "domain": ".hh.ru", "path": "/"}])
    client = Client()
    async def browser_cookies(): return repository.snapshot()[0]
    client._context = SimpleNamespace(cookies=browser_cookies)
    client._cookie_paths = CookiePaths(str(home / "cookies.json"), str(home / "state"))
    client._cookie_binding = CookieBinding(repository, repository.snapshot()[1], True, client._context)
    profile.hh = SimpleNamespace(cookies_file=str(home / "cookies.json"))
    return client, home, profile


def test_import_captures_paths_before_first_await(importing, tmp_path, monkeypatch):
    client, home, original = importing
    other = SimpleNamespace(home_dir=str(tmp_path / "b"), resume_file=str(tmp_path / "b" / "resume.md"))
    async def ids():
        monkeypatch.setattr(client_hh_auth, "_resolve_profile", lambda name: other)
        return [{"id": "r1", "title": "QA Engineer"}]
    client.get_resume_ids = ids
    result = asyncio.run(client_hh_auth.import_current_hh_resumes(client, "qa"))
    assert result["resume_file"] == original.resume_file
    assert (home / "resume.md").read_text() == "downloaded resume\n"
    assert not Path(other.home_dir).exists()
    assert Path(result["resumes"][0]["path"]).parent.stat().st_mode & 0o777 == 0o700


@pytest.mark.parametrize("changed", ["catalog", "resume", "env"])
def test_changed_import_inputs_are_not_overwritten(importing, changed):
    client, home, _ = importing
    async def download(item):
        if changed == "catalog": atomic_write_json(home / "hh_resumes.json", [{"id": "new-login"}])
        elif changed == "resume": (home / "resume.md").write_text("newer resume")
        else: (home / "profile.env").write_text("NEW=retain\n")
        return {"raw": "stale resume"}
    client.download_resume_by_id = download
    with pytest.raises(RuntimeError): asyncio.run(client_hh_auth.import_current_hh_resumes(client, "qa"))
    assert (home / "resume.md").read_text() == ("newer resume" if changed == "resume" else "original resume")
    assert json.loads((home / "hh_resume_import.json").read_text())["status"] == "failed"


def test_competing_import_blocks_before_remote_work(importing):
    client, home, profile = importing
    workflow = HHResumeImport(home, profile.resume_file); workflow.begin()
    async def forbidden(): raise AssertionError("Do not fetch while another import owns state")
    client.get_resume_ids = forbidden
    with pytest.raises(RuntimeError): asyncio.run(client_hh_auth.import_current_hh_resumes(client, "qa"))


@pytest.mark.parametrize("bad_id", ["../outside", "a/b", "", "."])
def test_import_rejects_unsafe_export_names(importing, bad_id):
    client, home, _ = importing
    async def ids(): return [{"id": bad_id}]
    client.get_resume_ids = ids
    with pytest.raises(ValueError): asyncio.run(client_hh_auth.import_current_hh_resumes(client, "qa"))
    assert (home / "resume.md").read_text() == "original resume"


def test_partial_import_publication_stays_uncertain(importing, monkeypatch):
    client, home, _ = importing
    def fail(*a, **k): raise OSError("synthetic env publication failure")
    monkeypatch.setattr(client_hh_auth, "_update_profile_resume_ids", fail)
    with pytest.raises(OSError): asyncio.run(client_hh_auth.import_current_hh_resumes(client, "qa"))
    assert json.loads((home / "hh_resume_import.json").read_text())["status"] == "uncertain"
    with pytest.raises(RuntimeError): asyncio.run(client_hh_auth.import_current_hh_resumes(client, "qa"))
    assert (home / "resume.md").read_text() == "original resume"


def test_native_import_rejects_changed_cookie_session(importing):
    from hh.browser import CookiePaths, CookieBinding
    from state_store.hh_cookies import HHCookieRepository
    client, home, profile = importing
    repository = HHCookieRepository(home / 'cookies.json')
    repository.save([{'name':'hhtoken','value':'original'}])
    revision = repository.snapshot()[1]
    async def browser_cookies(): return [{'name':'hhtoken','value':'original'}]
    context = SimpleNamespace(cookies=browser_cookies)
    client._context = context
    client._cookie_paths = CookiePaths(str(repository.path), str(home/'state'))
    client._cookie_binding = CookieBinding(repository, revision, True, context=context)
    profile.hh = SimpleNamespace(cookies_file=str(repository.path))
    async def download(item):
        repository.save([{'name':'hhtoken','value':'new-account'}])
        return {'raw':'old-account resume'}
    client.download_resume_by_id = download
    with pytest.raises(RuntimeError): asyncio.run(client_hh_auth.import_current_hh_resumes(client,'qa'))
    assert (home/'resume.md').read_text() == 'original resume'


def test_cancelled_auth_notification_still_completes_prompt(tmp_path, monkeypatch):
    import hh_auth_bridge
    monkeypatch.setattr(hh_auth_bridge, '_state_dir', lambda *a: str(tmp_path))
    async def cancel(*a, **k): raise asyncio.CancelledError
    monkeypatch.setattr(client_hh_auth, '_notify_hh_auth_request', cancel)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(client_hh_auth._request_hh_auth_value('code','qa','synthetic', page_url='',timeout_s=60,poll_sec=1))
    assert hh_auth_bridge.peek_pending('qa') is None


def test_token_transport_failure_is_not_automatically_replayed(credentials, monkeypatch):
    client, path, _ = credentials
    calls = []
    async def start(**options): calls.append(options)
    async def request(*a, **k): raise OSError('synthetic proxy error')
    monkeypatch.setattr(client, 'start', start)
    monkeypatch.setattr(client, '_request_once', request)
    monkeypatch.setattr('superjob_client.proxy_utils.is_proxy_error', lambda e: True)
    assert asyncio.run(client.refresh_access_token()) is False
    assert calls == [{}]
    assert json.loads(path.read_text())['_token_attempt']['status'] == 'uncertain'


def test_token_payload_numeric_string_expiry_preserves_compatibility(credentials, monkeypatch):
    client, path, _ = credentials
    async def request(*a, **k): return {'access_token':'fresh','expires_in':'900'}
    monkeypatch.setattr(client,'_request',request)
    assert asyncio.run(client.refresh_access_token())
    assert type(json.loads(path.read_text())['expires_at']) is int


def test_invalid_control_state_is_not_repaired_or_overwritten(importing):
    client, home, _ = importing
    path = home/'hh_resume_import.json'; path.write_bytes(b'{}')
    with pytest.raises(RuntimeError): asyncio.run(client_hh_auth.import_current_hh_resumes(client,'qa'))
    assert path.read_bytes() == b'{}' and (home/'resume.md').read_text() == 'original resume'


def test_native_import_completes_with_unchanged_cookie_owner(importing):
    from hh.browser import CookiePaths, CookieBinding
    from state_store.hh_cookies import HHCookieRepository
    client, home, profile = importing
    repository = HHCookieRepository(home / 'cookies.json')
    repository.save([{'name': 'hhtoken', 'value': 'synthetic'}])
    revision = repository.snapshot()[1]
    async def browser_cookies(): return [{'name': 'hhtoken', 'value': 'synthetic'}]
    context = SimpleNamespace(cookies=browser_cookies)
    client._context = context
    client._cookie_paths = CookiePaths(str(repository.path), str(home / 'state'))
    client._cookie_binding = CookieBinding(repository, revision, True, context=context)
    profile.hh = SimpleNamespace(cookies_file=str(repository.path))
    assert asyncio.run(client_hh_auth.import_current_hh_resumes(client, 'qa'))['ok']
    assert repository.snapshot()[1] == revision
    assert json.loads((home / 'hh_resume_import.json').read_text())['status'] == 'completed'
