"""Fault/concurrency tests for queued decisions, confirmed facts and exclusions."""
import json
import multiprocessing
import stat
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

import candidate_interview
import company_blacklist
import manual_apply_queue


def _state_worker(directory, barrier, index, kind):
    from pathlib import Path
    paths = {"queue": Path(directory) / "manual_apply_queue.json", "facts": Path(directory) / "candidate_interview.json",
        "blacklist": Path(directory) / "company_blacklist.json"}
    manual_apply_queue._queue_path = lambda profile_name=None: paths["queue"]
    company_blacklist._path = lambda profile_name=None: paths["blacklist"]
    barrier.wait(timeout=10)
    for offset in range(10):
        _mutate(kind, paths, index * 100 + offset)


@pytest.fixture
def paths(tmp_path, monkeypatch):
    queue = tmp_path / "manual_apply_queue.json"
    blacklist = tmp_path / "company_blacklist.json"
    monkeypatch.setattr(manual_apply_queue, "_queue_path", lambda profile_name=None: queue)
    monkeypatch.setattr(company_blacklist, "_path", lambda profile_name=None: blacklist)
    return {"queue": queue, "facts": tmp_path / "candidate_interview.json", "blacklist": blacklist}


def _mutate(kind, paths, index=0):
    if kind == "queue":
        return manual_apply_queue.create_candidate({"id": str(index), "company": "Synthetic"}, {}, profile_name="synthetic")
    if kind == "facts":
        return candidate_interview.add_fact(f"Synthetic fact {index}", profile_dir=str(paths[kind].parent))
    return company_blacklist.set_blocked(f"Synthetic Company {index}", profile_name="synthetic")


@pytest.mark.parametrize("kind", ["queue", "facts"])
@pytest.mark.parametrize("content", [b'{"broken":', b'[]', b'\xff', b'{}', b'{"items":[],"facts":{}}',
    b'{"items":{"token":null},"facts":[null]}', b'{"items":{"token":{"vacancy":[]}},"facts":[{"text":42}]}'])
def test_corrupt_data_blocks_repeated_mutations_without_reset(paths, kind, content):
    paths[kind].write_bytes(content)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="[Cc]orrupt|[Rr]estore"):
            _mutate(kind, paths)
        assert paths[kind].read_bytes() == content


@pytest.mark.parametrize("kind", ["queue", "facts", "blacklist"])
def test_read_permission_failure_does_not_overwrite_state(paths, kind, monkeypatch):
    from pathlib import Path
    import builtins
    _mutate(kind, paths)
    path = paths[kind]
    before = path.read_bytes()
    original = Path.open
    real_open = builtins.open

    def denied(self, *args, **kwargs):
        if self == path:
            raise PermissionError("synthetic read failure")
        return original(self, *args, **kwargs)

    def denied_builtin(file, mode="r", *args, **kwargs):
        if str(file) == str(path) and "r" in mode:
            raise PermissionError("synthetic read failure")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    monkeypatch.setattr(builtins, "open", denied_builtin)
    with pytest.raises(PermissionError):
        _mutate(kind, paths, 1)
    with real_open(path, "rb") as stream:
        assert stream.read() == before


@pytest.mark.parametrize("kind", ["queue", "facts", "blacklist"])
def test_private_files_and_fsync_backed_writes(paths, kind, monkeypatch):
    import os
    calls = []
    real_fsync = os.fsync

    def fsync(fd):
        calls.append(fd)
        return real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fsync)
    _mutate(kind, paths)
    assert stat.S_IMODE(paths[kind].stat().st_mode) == 0o600
    assert len(calls) >= 2  # data file and its parent directory


@pytest.mark.parametrize("kind", ["queue", "facts", "blacklist"])
def test_replace_failure_preserves_old_file_and_removes_temporary(paths, kind, monkeypatch):
    import os
    _mutate(kind, paths)
    path = paths[kind]
    before = path.read_bytes()
    original = os.replace

    def failed(src, dst):
        if str(dst) == str(path):
            raise OSError("synthetic replace failure")
        return original(src, dst)

    monkeypatch.setattr(os, "replace", failed)
    with pytest.raises(OSError):
        _mutate(kind, paths, 1)
    assert path.read_bytes() == before
    assert not list(path.parent.glob("*.tmp"))
    assert not list(path.parent.glob(".*.tmp"))
    assert not list(path.parent.glob("candidate-interview-*.json"))


def test_parallel_facts_are_not_lost(paths):
    barrier = Barrier(6)

    def worker(index):
        barrier.wait(timeout=10)
        for offset in range(6):
            _mutate("facts", paths, index * 10 + offset)

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(worker, range(6)))
    assert len(candidate_interview.facts(str(paths["facts"].parent))) == 36


def test_parallel_duplicate_fact_is_stored_once(paths):
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda _: _mutate("facts", paths), range(24)))
    assert len(candidate_interview.facts(str(paths["facts"].parent))) == 1


@pytest.mark.parametrize("kind", ["queue", "facts", "blacklist"])
def test_state_updates_are_locked_across_processes(paths, kind):
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(4)
    directory = str(paths["facts"].parent)
    workers = [context.Process(target=_state_worker, args=(directory, barrier, index, kind)) for index in range(4)]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=15)
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.pid is not None:
                if worker.is_alive():
                    worker.terminate()
                worker.join(timeout=5)
    saved = json.loads(paths[kind].read_text())
    collection = saved["items"] if kind == "queue" else saved["facts"] if kind == "facts" else saved
    assert len(collection) == 40


@pytest.mark.parametrize("kind", ["queue", "facts", "blacklist"])
def test_data_fsync_failure_keeps_previous_state(paths, kind, monkeypatch):
    import os
    _mutate(kind, paths)
    before = paths[kind].read_bytes()

    def failed(fd):
        raise OSError("synthetic data fsync failure")

    monkeypatch.setattr(os, "fsync", failed)
    with pytest.raises(OSError):
        _mutate(kind, paths, 1)
    assert paths[kind].read_bytes() == before
    assert not list(paths[kind].parent.glob(".*.tmp"))


def test_blacklist_mutation_reads_locked_profile_not_new_active_profile(tmp_path, monkeypatch):
    import fcntl
    first = tmp_path / "first" / "company_blacklist.json"
    second = tmp_path / "second" / "company_blacklist.json"
    for path, company in ((first, "First Only"), (second, "Second Only")):
        path.parent.mkdir()
        path.write_text(json.dumps([company]))
    selected = [first]
    original = fcntl.flock
    monkeypatch.setattr(company_blacklist, "_path", lambda profile_name=None: selected[0])

    def switch(fd, operation):
        if operation == fcntl.LOCK_EX:
            selected[0] = second
        return original(fd, operation)

    monkeypatch.setattr(fcntl, "flock", switch)
    company_blacklist.set_blocked("Synthetic New")
    assert set(json.loads(first.read_text())) == {"First Only", "Synthetic New"}
    assert json.loads(second.read_text()) == ["Second Only"]


def test_unknown_queue_mutations_do_not_create_json(paths):
    assert manual_apply_queue.mark_candidate("unknown", "applied") is None
    assert manual_apply_queue.record_feedback("unknown", "good") is None
    assert manual_apply_queue.snooze_candidate("unknown", profile_name="synthetic") is None
    assert not paths["queue"].exists()


def test_duplicate_fact_does_not_rewrite_json(paths, monkeypatch):
    from state_store.protected import ProtectedJsonStore
    _mutate("facts", paths)
    before = paths["facts"].read_bytes()

    def forbidden(*args):
        raise AssertionError("duplicate must not rewrite JSON")

    monkeypatch.setattr(ProtectedJsonStore, "_save_unlocked", forbidden)
    _mutate("facts", paths)
    assert paths["facts"].read_bytes() == before


@pytest.mark.parametrize("content", [b'{"broken":', b'{}', b'[null]', b'\xff'])
def test_corrupt_blacklist_blocks_mutation_and_keeps_bytes(paths, content):
    paths["blacklist"].write_bytes(content)
    for _ in range(2):
        with pytest.raises(ValueError):
            _mutate("blacklist", paths)
        assert paths["blacklist"].read_bytes() == content


def test_confirmed_facts_keep_deduplication_cap_and_metadata(paths):
    directory = str(paths["facts"].parent)
    candidate_interview._save({"facts": [], "metadata": {"synthetic": True}}, directory)
    for index in range(105):
        _mutate("facts", paths, index)
    candidate_interview.add_fact("SYNTHETIC FACT 104", profile_dir=directory)
    saved = candidate_interview.load(directory)
    assert len(saved["facts"]) == 100
    assert saved["facts"][0]["text"] == "Synthetic fact 5"
    assert saved["metadata"] == {"synthetic": True}


def test_queue_listing_does_not_rewrite_existing_data(paths, monkeypatch):
    import time
    from state_store.protected import ProtectedJsonStore
    _mutate("queue", paths)
    before = paths["queue"].read_bytes()
    monkeypatch.setattr(manual_apply_queue.time, "time", lambda: time.time_ns() / 1e9 + 8 * 24 * 3600)

    def forbidden(*args):
        raise AssertionError("listing must not rewrite JSON")

    monkeypatch.setattr(ProtectedJsonStore, "_save_unlocked", forbidden)
    assert manual_apply_queue.list_candidates("synthetic") == []
    assert paths["queue"].read_bytes() == before


@pytest.mark.parametrize("kind", ["queue", "blacklist"])
def test_waiting_for_legacy_lock_keeps_original_profile_path(tmp_path, monkeypatch, kind):
    from pathlib import Path
    import builtins
    import os
    import fcntl
    first = tmp_path / "first" / ("manual_apply_queue.json" if kind == "queue" else "company_blacklist.json")
    second = tmp_path / "second" / first.name
    selected = [first]
    monkeypatch.setattr(manual_apply_queue, "_queue_path", lambda profile_name=None: selected[0])
    monkeypatch.setattr(company_blacklist, "_path", lambda profile_name=None: selected[0])
    original_flock = fcntl.flock
    real_open = builtins.open
    original_path_open = Path.open
    original_os_open = os.open
    lock_paths = []

    def flock(fd, operation):
        if operation == fcntl.LOCK_EX:
            selected[0] = second
        return original_flock(fd, operation)

    def capture(path):
        if str(path).endswith(".lock"):
            lock_paths.append(Path(path))

    def builtin_open(path, *args, **kwargs):
        capture(path)
        return real_open(path, *args, **kwargs)

    def path_open(self, *args, **kwargs):
        capture(self)
        return original_path_open(self, *args, **kwargs)

    def os_open(path, *args, **kwargs):
        capture(path)
        return original_os_open(path, *args, **kwargs)

    monkeypatch.setattr(fcntl, "flock", flock)
    monkeypatch.setattr(builtins, "open", builtin_open)
    monkeypatch.setattr(Path, "open", path_open)
    monkeypatch.setattr(os, "open", os_open)
    if kind == "queue":
        manual_apply_queue.create_candidate({"id": "synthetic"}, {})
    else:
        company_blacklist.set_blocked("Synthetic Company")
    assert first.exists()
    assert not second.exists()
    assert lock_paths and set(lock_paths) == {first.with_suffix(".lock")}
