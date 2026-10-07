"""Synthetic cookies and fake browser lifecycle; never read a real profile."""
import asyncio
import concurrent.futures
import multiprocessing
import os
import stat
from types import SimpleNamespace

import pytest

import hh_client
from hh import browser
from state_store.hh_cookies import HHCookieRepository as Repository, HHCookieStateError
from state_store.json_store import atomic_write_json
from tests.test_hh_browser import (FakeLifecycleContext, FakeLifecycleBrowser, FakeChromium,
                             FakePlaywright, FakePlaywrightStarter, terminate_fake_browser)


def cookies(value):
    return [{"name": "hhtoken", "value": value, "domain": ".hh.ru", "path": "/"}]


def _cas_worker(args):
    path, revision, value = args
    try:
        Repository(path).save(cookies(value), expected_revision=revision)
        return True
    except HHCookieStateError:
        return False


@pytest.fixture
def lifecycle(tmp_path, monkeypatch):
    paths = browser.CookiePaths(str(tmp_path / "a" / "cookies.json"), str(tmp_path / "a" / "state"))
    monkeypatch.setattr(browser.config, "HH_COOKIES_FILE", paths.cookies_file)
    monkeypatch.setattr(browser.config, "HH_STATE_DIR", paths.state_dir)
    monkeypatch.delenv("HH_PROXY", raising=False)
    repo = Repository(paths.cookies_file)
    repo.save(cookies("original"))
    context = FakeLifecycleContext(cookies("rotated"))
    instance = FakeLifecycleBrowser(context)
    pw = FakePlaywright(FakeChromium(instance))
    session = SimpleNamespace(_context=None, _browser=None, _pw=None, _page=None, _cookie_paths=paths)
    settings = SimpleNamespace(HH_COOKIES_FILE=paths.cookies_file, HH_STATE_DIR=paths.state_dir,
                               HEADLESS=True, SLOW_MO=0, BROWSER_PROXY="")
    async def start():
        await browser.start_browser(session, settings=settings,
            playwright_factory=lambda: FakePlaywrightStarter(pw), stealth_available=False,
            proxy_env_builder=lambda _: {}, terminate=terminate_fake_browser)
    return SimpleNamespace(repo=repo, paths=paths, session=session, context=context,
                           instance=instance, pw=pw, settings=settings, start=start)


def test_client_captures_paths_at_construction(tmp_path, monkeypatch):
    monkeypatch.setattr(browser.config, "HH_COOKIES_FILE", str(tmp_path / "a.json"))
    monkeypatch.setattr(browser.config, "HH_STATE_DIR", str(tmp_path / "a-state"))
    client = hh_client.HHClient()
    monkeypatch.setattr(browser.config, "HH_COOKIES_FILE", str(tmp_path / "b.json"))
    assert client._cookie_paths.cookies_file == str(tmp_path / "a.json")


def test_start_loads_original_profile_before_first_await(lifecycle, tmp_path, monkeypatch):
    other = Repository(tmp_path / "b.json")
    other.save(cookies("other-profile"))
    async def first_await(*args):
        monkeypatch.setattr(browser.config, "HH_COOKIES_FILE", str(other.path))
        lifecycle.settings.HEADLESS = False
        return lifecycle.pw
    monkeypatch.setattr(FakePlaywrightStarter, "start", first_await)
    asyncio.run(lifecycle.start())
    assert lifecycle.context.loaded_cookies == cookies("original")
    assert lifecycle.pw.chromium.launch_options["headless"] is True
    asyncio.run(browser.save_session(lifecycle.session))
    assert lifecycle.repo.snapshot()[0] == cookies("rotated")
    assert other.snapshot()[0] == cookies("other-profile")


@pytest.mark.parametrize("shutdown", [False, True])
def test_save_after_profile_switch_keeps_original_destination(lifecycle, tmp_path, monkeypatch, shutdown):
    asyncio.run(lifecycle.start())
    other = Repository(tmp_path / "b.json")
    other.save(cookies("other-profile"))
    async def capture():
        monkeypatch.setattr(browser.config, "HH_COOKIES_FILE", str(other.path))
        return cookies("rotated")
    lifecycle.context.cookies = capture
    asyncio.run(browser.stop_browser(lifecycle.session, terminate=terminate_fake_browser) if shutdown else browser.save_session(lifecycle.session))
    assert lifecycle.repo.snapshot()[0] == cookies("rotated")
    assert other.snapshot()[0] == cookies("other-profile")


@pytest.mark.parametrize("shutdown", [False, True])
def test_stale_browser_cannot_replace_new_auth(lifecycle, shutdown):
    asyncio.run(lifecycle.start())
    lifecycle.repo.save(cookies("new-login"))
    if shutdown:
        asyncio.run(browser.stop_browser(lifecycle.session, terminate=terminate_fake_browser))
        assert lifecycle.instance.closed and lifecycle.pw.stopped
    else:
        with pytest.raises(HHCookieStateError, match="stale browser"):
            asyncio.run(browser.save_session(lifecycle.session))
        assert lifecycle.session._cookie_binding.revoked
    assert lifecycle.repo.snapshot()[0] == cookies("new-login")


def test_save_session_refuses_replaced_context(lifecycle):
    asyncio.run(lifecycle.start())
    async def capture():
        lifecycle.session._context = FakeLifecycleContext(cookies("new-context"))
        return cookies("stale-context")
    lifecycle.context.cookies = capture
    with pytest.raises(HHCookieStateError):
        asyncio.run(browser.save_session(lifecycle.session))
    assert lifecycle.repo.snapshot()[0] == cookies("original")


def test_binding_cannot_be_reused_for_a_different_context(lifecycle):
    asyncio.run(lifecycle.start())
    lifecycle.session._context = FakeLifecycleContext(cookies("unbound-context"))
    with pytest.raises(HHCookieStateError, match="ownership"):
        asyncio.run(browser.save_session(lifecycle.session))
    assert lifecycle.repo.snapshot()[0] == cookies("original")


def test_late_stop_does_not_clear_new_resources(lifecycle):
    asyncio.run(lifecycle.start())
    replacements = {field: object() for field in ("_context", "_browser", "_pw", "_page", "_cookie_binding")}
    async def capture():
        for field, value in replacements.items():
            setattr(lifecycle.session, field, value)
        return cookies("stale")
    lifecycle.context.cookies = capture
    asyncio.run(browser.stop_browser(lifecycle.session, terminate=terminate_fake_browser))
    assert lifecycle.instance.closed and lifecycle.pw.stopped
    assert all(getattr(lifecycle.session, field) is value for field, value in replacements.items())
    assert lifecycle.repo.snapshot()[0] == cookies("original")


def test_overlapping_cookie_reads_do_not_replay_older_result(lifecycle):
    async def scenario():
        await lifecycle.start()
        entered, release = asyncio.Event(), asyncio.Event()
        count = 0
        async def capture():
            nonlocal count
            count += 1
            if count == 1:
                entered.set()
                await release.wait()
                return cookies("old-result")
            return cookies("new-result")
        lifecycle.context.cookies = capture
        first = asyncio.create_task(browser.save_session(lifecycle.session))
        await entered.wait()
        await browser.save_session(lifecycle.session)
        release.set()
        with pytest.raises(HHCookieStateError):
            await first
    asyncio.run(scenario())
    assert lifecycle.repo.snapshot()[0] == cookies("new-result")


def test_shutdown_does_not_erase_cached_auth(lifecycle):
    asyncio.run(lifecycle.start())
    lifecycle.context.saved_cookies = []
    asyncio.run(browser.stop_browser(lifecycle.session, terminate=terminate_fake_browser))
    assert lifecycle.repo.snapshot()[0] == cookies("original")
    assert lifecycle.instance.closed and lifecycle.pw.stopped


def test_cancel_during_cookie_capture_preserves_cache_and_closes(lifecycle):
    asyncio.run(lifecycle.start())
    async def capture():
        raise asyncio.CancelledError
    lifecycle.context.cookies = capture
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(browser.stop_browser(lifecycle.session, terminate=terminate_fake_browser))
    assert lifecycle.repo.snapshot()[0] == cookies("original")
    assert lifecycle.instance.closed and lifecycle.pw.stopped
    assert lifecycle.session._context is None


def test_unconfirmed_termination_retains_driver_and_owned_references(lifecycle):
    asyncio.run(lifecycle.start())
    async def fail():
        raise RuntimeError("synthetic close failure")
    lifecycle.instance.close = fail
    with pytest.raises(RuntimeError):
        asyncio.run(browser.stop_browser(lifecycle.session, terminate=terminate_fake_browser, save_cookies=None))
    assert not lifecycle.pw.stopped
    assert lifecycle.session._context is lifecycle.context
    assert lifecycle.session._browser is lifecycle.instance


def test_failed_start_releases_resources_without_saving(lifecycle):
    async def fail():
        raise RuntimeError("synthetic page failure")
    lifecycle.context.new_page = fail
    with pytest.raises(RuntimeError):
        asyncio.run(lifecycle.start())
    assert lifecycle.instance.closed and lifecycle.pw.stopped
    assert lifecycle.repo.snapshot()[0] == cookies("original")
    assert lifecycle.session._context is None


def test_parallel_start_is_rejected_without_second_browser(lifecycle):
    async def scenario():
        entered, release = asyncio.Event(), asyncio.Event()
        async def start_pw():
            entered.set()
            await release.wait()
            return lifecycle.pw
        task = asyncio.create_task(browser.start_browser(lifecycle.session, settings=lifecycle.settings,
            playwright_factory=lambda: SimpleNamespace(start=start_pw), stealth_available=False,
            proxy_env_builder=lambda _: {}))
        await entered.wait()
        with pytest.raises(RuntimeError, match="starting"):
            await lifecycle.start()
        with pytest.raises(RuntimeError, match="startup"):
            await browser.stop_browser(lifecycle.session, terminate=terminate_fake_browser)
        with pytest.raises(RuntimeError, match="startup"):
            await browser.save_session(lifecycle.session)
        release.set()
        await task
    asyncio.run(scenario())


@pytest.mark.parametrize("contents", [b"{", b"\xff", b"{}", b"[null]", b'[{"name":"hhtoken"}]',
    b'[{"name":"hhtoken","value":"synthetic","expires":NaN}]'])
def test_corrupt_cookie_file_is_never_repaired(tmp_path, contents):
    repo = Repository(tmp_path / "cookies.json")
    repo.path.write_bytes(contents)
    for operation in (repo.snapshot, lambda: repo.save(cookies("new"))):
        with pytest.raises(HHCookieStateError):
            operation()
        assert repo.path.read_bytes() == contents


@pytest.mark.parametrize("failure", ["os.replace", "os.fsync", "json.dump"])
def test_failed_cookie_write_keeps_previous_file(tmp_path, monkeypatch, failure):
    repo = Repository(tmp_path / "cookies.json")
    repo.save(cookies("original"))
    before = repo.path.read_bytes()
    def fail(*args, **kwargs):
        raise OSError("synthetic write failure")
    monkeypatch.setattr("state_store.json_store." + failure, fail)
    with pytest.raises(OSError):
        repo.save(cookies("new"))
    assert repo.path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


def test_directory_fsync_failure_revokes_browser_binding(lifecycle, monkeypatch):
    asyncio.run(lifecycle.start())
    original = os.fsync
    def fail(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("synthetic directory fsync failure")
        original(fd)
    with monkeypatch.context() as scoped:
        scoped.setattr("state_store.json_store.os.fsync", fail)
        with pytest.raises(OSError):
            asyncio.run(browser.save_session(lifecycle.session))
    assert lifecycle.session._cookie_binding.revoked
    assert lifecycle.repo.snapshot()[0] == cookies("rotated")
    with pytest.raises(HHCookieStateError):
        asyncio.run(browser.save_session(lifecycle.session))


def test_cookie_compare_and_swap_one_thread_winner(tmp_path):
    repo = Repository(tmp_path / "cookies.json")
    repo.save(cookies("original"))
    revision = repo.snapshot()[1]
    with concurrent.futures.ThreadPoolExecutor(max_workers=30) as pool:
        results = list(pool.map(_cas_worker, [(str(repo.path), revision, str(i)) for i in range(30)]))
    assert sum(results) == 1


def test_cookie_compare_and_swap_one_process_winner(tmp_path):
    repo = Repository(tmp_path / "cookies.json")
    repo.save(cookies("original"))
    revision = repo.snapshot()[1]
    with concurrent.futures.ProcessPoolExecutor(max_workers=4,
            mp_context=multiprocessing.get_context("spawn")) as pool:
        results = list(pool.map(_cas_worker, [(str(repo.path), revision, str(i)) for i in range(12)]))
    assert sum(results) == 1


def test_missing_cookie_baseline_cannot_clobber_concurrent_creator(tmp_path):
    repo = Repository(tmp_path / "cookies.json")
    _, revision = repo.snapshot()
    repo.save(cookies("new-login"))
    with pytest.raises(HHCookieStateError):
        repo.save(cookies("stale"), expected_revision=revision)
    assert repo.snapshot()[0] == cookies("new-login")


def test_storage_state_wrapper_and_unknown_metadata_remain_readable(tmp_path):
    repo = Repository(tmp_path / "cookies.json")
    atomic_write_json(repo.path, {"cookies": cookies("original"), "origins": []})
    assert repo.snapshot()[0] == cookies("original")
    assert stat.S_IMODE(repo.path.stat().st_mode) == 0o600


def test_read_permission_failure_does_not_replace_cookies(tmp_path, monkeypatch):
    repo = Repository(tmp_path / "cookies.json")
    repo.save(cookies("original"))
    original = type(repo.path).open
    def denied(path, *args, **kwargs):
        if path == repo.path:
            raise PermissionError("synthetic read failure")
        return original(path, *args, **kwargs)
    with monkeypatch.context() as scoped:
        scoped.setattr(type(repo.path), "open", denied)
        with pytest.raises(PermissionError):
            repo.save(cookies("new"))
    assert repo.snapshot()[0] == cookies("original")


def test_identical_cookie_save_does_not_rewrite(lifecycle, monkeypatch):
    asyncio.run(lifecycle.start())
    lifecycle.context.saved_cookies = cookies("original")
    monkeypatch.setattr("state_store.json_store.os.replace", lambda *args: pytest.fail("No-op cookie write"))
    asyncio.run(browser.save_session(lifecycle.session))


def test_auth_probe_cannot_switch_context_in_fallback(lifecycle):
    asyncio.run(lifecycle.start())
    calls = []
    async def capture(*args):
        calls.append(args)
        if args:
            lifecycle.session._context = FakeLifecycleContext(cookies("replacement"))
            raise TypeError("Synthetic unscoped-only context")
        return cookies("old-context")
    lifecycle.context.cookies = capture
    assert asyncio.run(browser.has_auth_cookies(lifecycle.session)) is False
    assert len(calls) == 2


def test_cancelled_start_closes_partial_resources(lifecycle):
    async def cancel():
        raise asyncio.CancelledError
    lifecycle.context.new_page = cancel
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(lifecycle.start())
    assert lifecycle.instance.closed and lifecycle.pw.stopped
    assert lifecycle.session._context is None
    assert lifecycle.repo.snapshot()[0] == cookies("original")
