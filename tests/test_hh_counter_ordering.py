"""Counter sequencing and mocked HTTP; all cookies/counters are synthetic."""
import concurrent.futures
import json
import multiprocessing
import os
import stat
import threading
from contextlib import contextmanager

import pytest

import hh_response_counter as counter
from state_store.hh_cookies import HHCookieRepository
from state_store.hh_response_counter import HHCounterRepository
from state_store.json_store import atomic_write_json


def current(total=10, fetched_at="2026-10-03T10:00:00+03:00"):
    return {"profile": "synthetic", "fetched_at": fetched_at,
            "active": total, "archived": 2, "deleted": 1, "active_pages": 1,
            "archived_pages": 1, "total": total + 3}


def save(home, total=10, **kwargs):
    return counter.save_snapshot(profile_name="synthetic", home_dir=str(home),
        active={"total": total, "deleted": 1, "page_count": 1},
        archived={"total": 2, "page_count": 1}, **kwargs)


def _begin_worker(path):
    return HHCounterRepository(path).begin("synthetic")


def test_late_refresh_does_not_rollback_newer_success(tmp_path):
    repo = HHCounterRepository(tmp_path / counter.SNAPSHOT_FILENAME)
    old, new = repo.begin("synthetic"), repo.begin("synthetic")
    newer = repo.commit(current(20), new)
    assert repo.commit(current(10), old) == newer
    assert repo.snapshot.load() == newer


def test_delta_is_against_latest_committed_snapshot(tmp_path):
    repo = HHCounterRepository(tmp_path / counter.SNAPSHOT_FILENAME)
    old, new = repo.begin("synthetic"), repo.begin("synthetic")
    repo.commit(current(10), old)
    second = repo.commit(current(20), new)
    assert second["delta"] == {"active": 10, "archived": 0, "deleted": 0, "total": 10}


def test_failed_newer_fetch_does_not_starve_valid_older_result(tmp_path):
    repo = HHCounterRepository(tmp_path / counter.SNAPSHOT_FILENAME)
    old = repo.begin("synthetic")
    repo.begin("synthetic")  # Simulated provider/network failure, never committed.
    assert repo.commit(current(10), old)["active"] == 10


def test_sequence_wins_over_wall_clock_regression(tmp_path):
    repo = HHCounterRepository(tmp_path / counter.SNAPSHOT_FILENAME)
    repo.commit(current(10), repo.begin("synthetic"))
    result = repo.commit(current(20, "2026-10-03T09:00:00+03:00"), repo.begin("synthetic"))
    assert result["active"] == 20


def test_direct_save_rejects_older_timestamp_and_preserves_delta(tmp_path):
    newer = save(tmp_path, 20, fetched_at="2026-10-03T11:00:00+03:00")
    assert save(tmp_path, 10, fetched_at="2026-10-03T10:00:00+03:00") == newer


def test_thread_tickets_are_unique(tmp_path):
    path = str(tmp_path / counter.SNAPSHOT_FILENAME)
    with concurrent.futures.ThreadPoolExecutor(max_workers=30) as pool:
        tickets = list(pool.map(_begin_worker, [path] * 60))
    assert sorted(tickets) == list(range(1, 61))


def test_process_tickets_are_unique(tmp_path):
    path = str(tmp_path / counter.SNAPSHOT_FILENAME)
    with concurrent.futures.ProcessPoolExecutor(max_workers=4,
            mp_context=multiprocessing.get_context("spawn")) as pool:
        tickets = list(pool.map(_begin_worker, [path] * 20))
    assert sorted(tickets) == list(range(1, 21))


def test_concurrent_commits_keep_highest_sequence(tmp_path):
    repo = HHCounterRepository(tmp_path / counter.SNAPSHOT_FILENAME)
    tickets = [repo.begin("synthetic") for _ in range(30)]
    with concurrent.futures.ThreadPoolExecutor(max_workers=30) as pool:
        list(pool.map(lambda ticket: repo.commit(current(ticket), ticket), reversed(tickets)))
    assert repo.snapshot.load()["refresh_sequence"] == 30
    assert repo.snapshot.load()["active"] == 30


def test_unknown_metadata_retained(tmp_path):
    repo = HHCounterRepository(tmp_path / counter.SNAPSHOT_FILENAME)
    first = repo.commit(current(10), repo.begin("synthetic"))
    atomic_write_json(repo.path, {**first, "custom": {"keep": True}})
    result = repo.commit(current(11), repo.begin("synthetic"))
    assert result["custom"] == {"keep": True}


def test_counter_path_cannot_switch_profile_before_first_commit(tmp_path):
    repo = HHCounterRepository(tmp_path / counter.SNAPSHOT_FILENAME)
    repo.begin("synthetic")
    with pytest.raises(ValueError, match="profile"):
        repo.begin("other")


@pytest.mark.parametrize("contents", [b"{", b"\xff", b"[]", b'{"active":10}',
    json.dumps({**current(), "total": -1}).encode(),
    json.dumps({**current(), "active": True}).encode(),
    json.dumps({**current(), "fetched_at": "no-date"}).encode(),
    json.dumps({**current(), "delta": []}).encode()])
def test_corrupt_snapshot_is_retained_and_blocks_begin(tmp_path, contents):
    repo = HHCounterRepository(tmp_path / counter.SNAPSHOT_FILENAME)
    repo.path.write_bytes(contents)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="Corrupt state"):
            repo.begin("synthetic")
        assert repo.path.read_bytes() == contents
    assert not repo.order.path.exists()


@pytest.mark.parametrize("contents", [b"{", b"[]", b'{"issued":true}', b'{"issued":-1}', b'{"issued":2,"profile":[]}'])
def test_corrupt_order_is_retained(tmp_path, contents):
    repo = HHCounterRepository(tmp_path / counter.SNAPSHOT_FILENAME)
    repo.order.path.write_bytes(contents)
    with pytest.raises(RuntimeError):
        repo.begin("synthetic")
    assert repo.order.path.read_bytes() == contents
    assert not repo.path.exists()


@pytest.mark.parametrize("value", [True, -1, "10", 1.5])
def test_invalid_counter_input_does_not_create_state(tmp_path, value):
    with pytest.raises((ValueError, TypeError)):
        save(tmp_path, value)
    assert not (tmp_path / counter.SNAPSHOT_FILENAME).exists()
    assert not (tmp_path / "hh_response_counter_order.json").exists()


@pytest.mark.parametrize("stamp", ["wrong", "2026-10-03T10:00:00"])
def test_invalid_timestamp_does_not_create_state(tmp_path, stamp):
    with pytest.raises(ValueError):
        save(tmp_path, fetched_at=stamp)
    assert not (tmp_path / counter.SNAPSHOT_FILENAME).exists()


@pytest.mark.parametrize("failure", ["os.replace", "json.dump", "os.fsync"])
def test_failed_commit_keeps_previous_counter(tmp_path, monkeypatch, failure):
    repo = HHCounterRepository(tmp_path / counter.SNAPSHOT_FILENAME)
    repo.commit(current(10), repo.begin("synthetic"))
    new = repo.begin("synthetic")
    before = repo.path.read_bytes()
    def fail(*args, **kwargs):
        raise OSError("synthetic persistence failure")
    monkeypatch.setattr("state_store.json_store." + failure, fail)
    with pytest.raises(OSError):
        repo.commit(current(20), new)
    assert repo.path.read_bytes() == before


def test_post_replace_fsync_failure_cannot_replay_previous_ticket(tmp_path, monkeypatch):
    repo = HHCounterRepository(tmp_path / counter.SNAPSHOT_FILENAME)
    old, new = repo.begin("synthetic"), repo.begin("synthetic")
    original = os.fsync
    def fail(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("synthetic directory fsync failure")
        original(fd)
    with monkeypatch.context() as scoped:
        scoped.setattr("state_store.json_store.os.fsync", fail)
        with pytest.raises(OSError):
            repo.commit(current(20), new)
    assert repo.commit(current(10), old)["active"] == 20
    assert stat.S_IMODE(repo.path.stat().st_mode) == 0o600


def test_unissued_ticket_is_rejected(tmp_path):
    repo = HHCounterRepository(tmp_path / counter.SNAPSHOT_FILENAME)
    with pytest.raises(ValueError, match="not issued"):
        repo.commit(current(), 100)


def test_counter_read_permission_failure_retains_snapshot(tmp_path, monkeypatch):
    repo = HHCounterRepository(tmp_path / counter.SNAPSHOT_FILENAME)
    repo.commit(current(), repo.begin("synthetic"))
    before = repo.path.read_bytes()
    original = type(repo.path).open
    def denied(path, *args, **kwargs):
        if path == repo.path:
            raise PermissionError("synthetic read failure")
        return original(path, *args, **kwargs)
    with monkeypatch.context() as scoped:
        scoped.setattr(type(repo.path), "open", denied)
        with pytest.raises(PermissionError):
            repo.begin("synthetic")
    assert repo.path.read_bytes() == before


@pytest.fixture
def refresh_fixture(tmp_path, monkeypatch):
    path = tmp_path / "cookies.json"
    cookie_repo = HHCookieRepository(path)
    cookie_repo.save([{"name": "hhtoken", "value": "synthetic", "domain": ".hh.ru"}])
    calls = []
    @contextmanager
    def client(cookies_file, **kwargs):
        assert str(path) == str(cookies_file)
        assert kwargs["cookies"] == cookie_repo.snapshot()[0]
        yield object()
    def fetch(client, url, *, expected_filter):
        calls.append(expected_filter)
        return {"total": 10 if expected_filter == "active" else 2, "deleted": 1, "page_count": 1}
    monkeypatch.setattr(counter, "_authenticated_client", client)
    monkeypatch.setattr(counter, "_fetch_page", fetch)
    return tmp_path, path, calls, cookie_repo


def test_refresh_executes_two_mocked_requests_and_commits(refresh_fixture):
    home, path, calls, _ = refresh_fixture
    result = counter.refresh(profile_name="synthetic", home_dir=str(home), cookies_file=str(path))
    assert result["active"] == 10 and result["total"] == 13
    assert calls == ["active", "archived"]


def test_refresh_rejects_changed_cookie_session(refresh_fixture, monkeypatch):
    home, path, calls, cookie_repo = refresh_fixture
    def fetch(*args, **kwargs):
        cookie_repo.save([{"name": "hhtoken", "value": "new-synthetic-session"}])
        return {"total": 10, "deleted": 1, "page_count": 1}
    monkeypatch.setattr(counter, "_fetch_page", fetch)
    with pytest.raises(counter.HHResponseCounterError, match="изменилась"):
        counter.refresh(profile_name="synthetic", home_dir=str(home), cookies_file=str(path))
    assert not (home / counter.SNAPSHOT_FILENAME).exists()


def test_failed_begin_prevents_requests(refresh_fixture, monkeypatch):
    home, path, calls, _ = refresh_fixture
    def fail(*args, **kwargs):
        raise OSError("synthetic begin failure")
    monkeypatch.setattr("state_store.json_store.os.replace", fail)
    with pytest.raises(OSError):
        counter.refresh(profile_name="synthetic", home_dir=str(home), cookies_file=str(path))
    assert calls == []


def test_no_counter_or_cookie_lock_crosses_request(refresh_fixture, monkeypatch):
    home, path, calls, cookie_repo = refresh_fixture
    repo = HHCounterRepository(home / counter.SNAPSHOT_FILENAME)
    def fetch(*args, **kwargs):
        # A different thread must be able to take either stable sidecar while
        # the request is outstanding. Timeout only guards against deadlock.
        pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
        try:
            assert pool.submit(cookie_repo.snapshot).result(timeout=3)[0]
            assert pool.submit(repo.begin, "synthetic").result(timeout=3) > 1
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        return {"total": 10, "deleted": 1, "page_count": 1}
    monkeypatch.setattr(counter, "_fetch_page", fetch)
    assert counter.refresh(profile_name="synthetic", home_dir=str(home), cookies_file=str(path))["active"] == 10


def test_two_overlapping_refreshes_keep_newest_result(refresh_fixture, monkeypatch):
    home, path, calls, _ = refresh_fixture
    entered, release = threading.Event(), threading.Event()
    labels = []
    @contextmanager
    def client(*args, **kwargs):
        label = "old" if not labels else "new"
        labels.append(label)
        yield label
    def fetch(label, url, *, expected_filter):
        if label == "old" and expected_filter == "active":
            entered.set()
            assert release.wait(timeout=5)
        return {"total": (10 if label == "old" else 20), "deleted": 1, "page_count": 1}
    monkeypatch.setattr(counter, "_authenticated_client", client)
    monkeypatch.setattr(counter, "_fetch_page", fetch)
    pool = concurrent.futures.ThreadPoolExecutor(max_workers=2)
    try:
        old = pool.submit(counter.refresh, profile_name="synthetic", home_dir=str(home), cookies_file=str(path))
        assert entered.wait(timeout=5)
        new = pool.submit(counter.refresh, profile_name="synthetic", home_dir=str(home), cookies_file=str(path)).result(timeout=5)
        release.set()
        assert old.result(timeout=5) == new
        assert new["active"] == 20
        assert json.loads((home / counter.SNAPSHOT_FILENAME).read_text()) == new
    finally:
        release.set()
        pool.shutdown(wait=True)


def test_refresh_captures_absolute_destination_before_requests(refresh_fixture, monkeypatch):
    home, path, calls, _ = refresh_fixture
    original = home / "original"
    other = home / "other"
    original.mkdir()
    other.mkdir()
    monkeypatch.chdir(home)
    def fetch(*args, **kwargs):
        monkeypatch.chdir(other)
        return {"total": 10, "deleted": 1, "page_count": 1}
    monkeypatch.setattr(counter, "_fetch_page", fetch)
    assert counter.refresh(profile_name="synthetic", home_dir="original", cookies_file=str(path))["active"] == 10
    assert (original / counter.SNAPSHOT_FILENAME).exists()
    assert not (other / "original").exists()
