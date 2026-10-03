"""Synthetic lifecycle/CAS/failure coverage for every native cookie client."""
import asyncio
import concurrent.futures
import multiprocessing
import os
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from state_store.browser_cookies import CookieRepository, CookieStateError, validate_cookies
from state_store.hh_cookies import HHCookieRepository, HHCookieStateError
from state_store.json_store import atomic_write_json
from tests.test_source_cookie_regressions import native_case, cookies, SOURCES


def _cas_worker(args):
    path, revision, value = args
    try:
        CookieRepository(path).save(cookies(value), expected_revision=revision)
        return True
    except CookieStateError:
        return False


def test_constructor_captures_original_path(native_case, monkeypatch):
    case = native_case
    monkeypatch.setattr(case.module.config, case.setting, str(case.other))
    asyncio.run(case.client.start_browser())
    assert case.context.loaded_cookies == cookies("original")


def test_relative_path_does_not_follow_later_cwd(native_case, tmp_path, monkeypatch):
    case = native_case
    monkeypatch.chdir(case.original.parent)
    monkeypatch.setattr(case.module.config, case.setting, "cookies.json")
    client = type(case.client)()
    monkeypatch.chdir(case.other.parent)
    asyncio.run(client.start_browser())
    asyncio.run(client.save_session())
    assert case.read(case.original) == cookies("rotated")
    assert case.read(case.other) == cookies("other")


def test_save_cannot_adopt_new_login_revision(native_case):
    case = native_case
    asyncio.run(case.client.start_browser())
    case.module._save_cookies(cookies("new-login"))
    for _ in range(2):
        with pytest.raises(CookieStateError):
            asyncio.run(case.client.save_session())
    assert case.client._cookie_session.binding.revoked
    assert case.read(case.original) == cookies("new-login")


def test_overlapping_captures_cannot_publish_late_old_result(native_case):
    case = native_case
    async def scenario():
        await case.client.start_browser()
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0
        async def capture():
            nonlocal calls
            calls += 1
            if calls == 1:
                entered.set()
                await release.wait()
                return cookies("stale")
            return cookies("fresh")
        case.context.cookies = capture
        old = asyncio.create_task(case.client.save_session())
        await entered.wait()
        await case.client.save_session()
        release.set()
        with pytest.raises(CookieStateError):
            await old
    asyncio.run(scenario())
    assert case.read(case.original) == cookies("fresh")


def test_replaced_context_cannot_publish(native_case):
    case = native_case
    asyncio.run(case.client.start_browser())
    async def capture():
        case.client._context = object()
        return cookies("stale")
    case.context.cookies = capture
    with pytest.raises(CookieStateError):
        asyncio.run(case.client.save_session())
    assert case.read(case.original) == cookies("original")


def test_empty_shutdown_keeps_nonempty_cache_but_explicit_save_can_clear(native_case):
    case = native_case
    asyncio.run(case.client.start_browser())
    case.context.saved_cookies = []
    asyncio.run(case.client.stop_browser())
    assert case.read(case.original) == cookies("original")
    asyncio.run(case.client.start_browser())
    asyncio.run(case.client.save_session())
    assert case.read(case.original) == []


def test_late_cleanup_does_not_clear_new_generation(native_case):
    case = native_case
    asyncio.run(case.client.start_browser())
    replacements = {key: object() for key in ("_pw", "_browser", "_context", "_page")}
    new_binding = object()
    async def capture():
        for key, value in replacements.items(): setattr(case.client, key, value)
        case.client._cookie_session.binding = new_binding
        return cookies("stale")
    case.context.cookies = capture
    asyncio.run(case.client.stop_browser())
    assert all(getattr(case.client, key) is value for key, value in replacements.items())
    assert case.client._cookie_session.binding is new_binding
    assert case.browser.closed and case.pw.stopped
    assert case.read(case.original) == cookies("original")


@pytest.mark.parametrize("stage", ["launch", "cookies", "page"])
def test_partial_startup_error_closes_owned_resources_without_save(native_case, monkeypatch, stage):
    case = native_case
    error = RuntimeError("synthetic startup error")
    async def fail(*a, **k): raise error
    target, field = {"launch": (case.pw.chromium, "launch"),
                     "cookies": (case.context, "add_cookies"),
                     "page": (case.context, "new_page")}[stage]
    monkeypatch.setattr(target, field, fail)
    with pytest.raises(RuntimeError) as caught:
        asyncio.run(case.client.start_browser())
    assert caught.value is error
    assert case.pw.stopped
    if stage != "launch": assert case.browser.closed
    assert all(getattr(case.client, field) is None for field in ("_pw", "_browser", "_context", "_page"))
    assert case.read(case.original) == cookies("original")


def test_startup_cancel_and_cleanup_error_preserve_original_exception(native_case, monkeypatch):
    case = native_case
    async def cancel(): raise asyncio.CancelledError
    async def close_error(): raise OSError("synthetic cleanup failure")
    monkeypatch.setattr(case.context, "new_page", cancel)
    monkeypatch.setattr(case.browser, "close", close_error)
    with pytest.raises(asyncio.CancelledError): asyncio.run(case.client.start_browser())
    assert case.pw.stopped and case.client._context is None
    assert case.read(case.original) == cookies("original")


def test_parallel_start_stop_save_guard_and_idempotent_ready_start(native_case, monkeypatch):
    case = native_case
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def begin():
            entered.set()
            await release.wait()
            return case.pw
        monkeypatch.setattr(case.starter, "start", begin)
        task = asyncio.create_task(case.client.start_browser())
        await entered.wait()
        for call in [case.client.start_browser, case.client.stop_browser, case.client.save_session]:
            with pytest.raises(RuntimeError): await call()
        release.set()
        await task
        context = case.client._context
        await case.client.start_browser()
        assert case.client._context is context
    asyncio.run(scenario())


def test_shutdown_cancellation_still_closes_and_clears_resources(native_case):
    case = native_case
    asyncio.run(case.client.start_browser())
    async def cancel(): raise asyncio.CancelledError
    case.context.cookies = cancel
    with pytest.raises(asyncio.CancelledError): asyncio.run(case.client.stop_browser())
    assert case.browser.closed and case.pw.stopped
    assert case.client._context is None
    assert case.read(case.original) == cookies("original")


def test_close_error_still_stops_playwright_and_clears_references(native_case):
    case = native_case
    asyncio.run(case.client.start_browser())
    async def fail(): raise OSError("synthetic close error")
    case.browser.close = fail
    with pytest.raises(OSError): asyncio.run(case.client.stop_browser())
    assert case.pw.stopped and case.client._context is None


def test_http_close_failure_does_not_skip_browser_cleanup(native_case):
    case = native_case
    asyncio.run(case.client.start_browser())
    class HTTP:
        closed = False
        async def close(self): raise OSError("synthetic HTTP close failure")
    case.client._session = HTTP()
    with pytest.raises(OSError): asyncio.run(case.client.stop())
    assert case.browser.closed and case.pw.stopped and case.client._session is None


def test_late_http_close_does_not_clear_new_transport(native_case):
    case = native_case
    replacement = object()
    class HTTP:
        closed = False
        async def close(self): case.client._session = replacement
    case.client._session = HTTP()
    asyncio.run(case.client.stop())
    assert case.client._session is replacement


def test_full_stop_cannot_close_browser_started_during_http_cleanup(native_case):
    case = native_case
    asyncio.run(case.client.start_browser())
    class HTTP:
        closed = False
        async def close(self):
            await case.client.stop_browser()
            await case.client.start_browser()
            case.browser.closed = case.pw.stopped = False
    case.client._session = HTTP()
    asyncio.run(case.client.stop())
    assert case.client._context is case.context
    assert not case.browser.closed and not case.pw.stopped


def test_duplicate_shutdown_has_one_owner_and_blocks_save(native_case):
    case = native_case
    async def scenario():
        await case.client.start_browser()
        entered, release = asyncio.Event(), asyncio.Event()
        calls = 0
        async def capture():
            nonlocal calls
            calls += 1
            entered.set()
            await release.wait()
            return cookies("rotated")
        case.context.cookies = capture
        closing = asyncio.create_task(case.client.stop_browser())
        await entered.wait()
        await case.client.stop_browser()
        with pytest.raises(CookieStateError): await case.client.save_session()
        with pytest.raises(CookieStateError): await case.client.start_browser()
        assert not case.browser.closed
        release.set()
        await closing
        assert calls == 1
    asyncio.run(scenario())
    assert case.browser.closed and case.pw.stopped


def test_unbound_context_cannot_be_adopted_or_saved(native_case):
    case = native_case
    case.client._context = case.context
    case.client._page = case.context.page
    for call in [case.client.start_browser, case.client.save_session]:
        with pytest.raises(CookieStateError): asyncio.run(call())
    assert case.read(case.original) == cookies("original")


def test_storage_state_wrapper_loads_browser_cookies(native_case):
    case = native_case
    atomic_write_json(case.original, {"cookies": cookies("wrapped"), "origins": []})
    asyncio.run(case.client.start_browser())
    assert case.context.loaded_cookies == cookies("wrapped")


def test_cancel_before_playwright_returns_resets_startup_ownership(native_case, monkeypatch):
    case = native_case
    async def cancel(): raise asyncio.CancelledError
    monkeypatch.setattr(case.starter, "start", cancel)
    with pytest.raises(asyncio.CancelledError): asyncio.run(case.client.start_browser())
    assert not case.client._cookie_session.starting
    assert case.client._cookie_session.binding is None
    assert case.client._pw is None
    assert case.read(case.original) == cookies("original")


def test_directory_fsync_failure_revokes_binding_and_cannot_erase_new_login(native_case, monkeypatch):
    case = native_case
    asyncio.run(case.client.start_browser())
    original = os.fsync
    def fail(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode): raise OSError("synthetic directory fsync failure")
        original(fd)
    with monkeypatch.context() as scoped:
        scoped.setattr("state_store.json_store.os.fsync", fail)
        with pytest.raises(OSError): asyncio.run(case.client.save_session())
    assert case.client._cookie_session.binding.revoked
    assert case.read(case.original) == cookies("rotated")
    case.module._save_cookies(cookies("new-login"))
    asyncio.run(case.client.stop_browser())
    assert case.read(case.original) == cookies("new-login")


def test_no_cookie_lock_crosses_browser_await(native_case):
    case = native_case
    asyncio.run(case.client.start_browser())
    async def capture():
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
        try:
            future = pool.submit(case.module._save_cookies, cookies("new-login"))
            future.result(timeout=2)
        finally:
            pool.shutdown(wait=False)
        return cookies("stale")
    case.context.cookies = capture
    with pytest.raises(CookieStateError): asyncio.run(case.client.save_session())
    assert case.read(case.original) == cookies("new-login")


@pytest.mark.parametrize("failure", ["os.replace", "os.fsync", "json.dump"])
def test_cookie_write_failures_keep_old_file(native_case, monkeypatch, failure):
    case = native_case
    asyncio.run(case.client.start_browser())
    before = case.original.read_bytes()
    def fail(*a, **k): raise OSError("synthetic publication failure")
    monkeypatch.setattr("state_store.json_store." + failure, fail)
    with pytest.raises(OSError): asyncio.run(case.client.save_session())
    assert case.original.read_bytes() == before
    assert case.client._cookie_session.binding.revoked


@pytest.mark.parametrize("content", [b'not-json', b'\xff', b'{}', b'[{"name":"session","value":3}]'])
def test_invalid_cache_blocks_start_before_playwright(native_case, monkeypatch, content):
    case = native_case
    case.original.write_bytes(content)
    def forbidden(): raise AssertionError("Must not start a browser on corrupt cookies")
    monkeypatch.setattr(case.module, "async_playwright", forbidden)
    with pytest.raises(CookieStateError): asyncio.run(case.client.start_browser())
    assert case.original.read_bytes() == content
    assert case.client._pw is None


def test_cookie_reads_do_not_mask_io_failure(native_case, monkeypatch):
    case = native_case
    original = Path.open
    def fail(path, *a, **k):
        if path == case.original: raise PermissionError("synthetic permission failure")
        return original(path, *a, **k)
    monkeypatch.setattr(Path, "open", fail)
    with pytest.raises(PermissionError): case.module._load_cookies()
    with pytest.raises(PermissionError): asyncio.run(case.client.start_browser())


@pytest.mark.parametrize("source", SOURCES[:2], ids=["habr", "geekjob"])
@pytest.mark.parametrize("error", [RuntimeError("synthetic save failure"), asyncio.CancelledError()])
def test_interactive_login_failure_always_runs_cleanup(tmp_path, monkeypatch, source, error):
    module, client_type, setting = source
    monkeypatch.setattr(module.config, setting, str(tmp_path / "cookies.json"))
    monkeypatch.setattr(module.config, "HH_STATE_DIR", str(tmp_path / "state"))
    client = client_type()
    cleaned = []
    async def nothing(*a, **k): pass
    async def fail(): raise error
    async def cleanup(): cleaned.append(True)
    client._page = SimpleNamespace(goto=nothing)
    monkeypatch.setattr(client, "start_browser", nothing)
    monkeypatch.setattr(client, "save_session", fail)
    monkeypatch.setattr(client, "stop_browser", cleanup)
    async def scenario():
        monkeypatch.setattr(asyncio.get_running_loop(), "run_in_executor", nothing)
        await client.login_interactive()
    with pytest.raises(type(error)): asyncio.run(scenario())
    assert cleaned == [True]


@pytest.mark.parametrize("processes", [False, True])
def test_shared_cookie_cas_has_one_contending_winner(tmp_path, processes):
    repo = CookieRepository(tmp_path / "cookies.json")
    repo.save(cookies("original"))
    revision = repo.snapshot()[1]
    pool = (concurrent.futures.ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn"))
            if processes else concurrent.futures.ThreadPoolExecutor(max_workers=30))
    with pool:
        results = list(pool.map(_cas_worker, [(str(repo.path), revision, str(i)) for i in range(30)]))
    assert sum(results) == 1


def test_missing_cache_claim_cannot_replace_a_concurrent_creator(tmp_path):
    repo = CookieRepository(tmp_path / "cookies.json")
    _, revision = repo.snapshot()
    repo.save(cookies("new-login"))
    with pytest.raises(CookieStateError): repo.save(cookies("stale"), expected_revision=revision)


def test_wrapper_unknown_metadata_noop_and_private_mode(tmp_path, monkeypatch):
    repo = CookieRepository(tmp_path / "cookies.json")
    payload = cookies("original")
    payload[0]["unknown"] = {"synthetic": True}
    atomic_write_json(repo.path, {"cookies": payload, "origins": []})
    before = repo.path.read_bytes()
    monkeypatch.setattr(repo, "_write", lambda *a: (_ for _ in ()).throw(AssertionError("No-op write")))
    revision = repo.save(payload)
    assert repo.path.read_bytes() == before and repo.snapshot()[1] == revision
    assert repo.path.stat().st_mode & 0o777 == 0o600


def test_shared_hh_compatibility_preserves_typed_error_and_writer_seam(tmp_path, monkeypatch):
    repo = HHCookieRepository(tmp_path / "cookies.json")
    repo.save(cookies("original"))
    def fail(*a): raise OSError("synthetic seam failure")
    monkeypatch.setattr("state_store.hh_cookies.atomic_write_json", fail)
    with pytest.raises(OSError): repo.save(cookies("new"))
    repo.path.write_bytes(b'{}')
    with pytest.raises(HHCookieStateError): repo.snapshot()


@pytest.mark.parametrize("expiry", [True, float('nan'), float('inf'), 10**400])
def test_invalid_expiry_is_rejected_without_exposing_cookie_value(expiry):
    payload = cookies("synthetic-sensitive-marker")
    payload[0]["expires"] = expiry
    with pytest.raises(CookieStateError) as caught: validate_cookies(payload)
    assert "synthetic-sensitive-marker" not in str(caught.value)
