"""Process ownership tests use synthetic PIDs and never signal real services."""
import multiprocessing
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, get_ident
from types import SimpleNamespace

import pytest

import runtime_control


def _registration_worker(path, barrier, release, results):
    claimed = None
    barrier.wait(timeout=10)
    try:
        claimed = runtime_control.register_current_process(path, wait_timeout_sec=0)
    except RuntimeError:
        pass
    results.put(claimed)
    release.wait(timeout=15)
    if claimed is not None:
        runtime_control.unregister_current_process(path)


def test_registration_is_exclusive_across_processes(tmp_path):
    context = multiprocessing.get_context("spawn")
    path = str(tmp_path / "worker.pid")
    barrier = context.Barrier(4)
    release = context.Event()
    results = context.Queue()
    workers = [context.Process(target=_registration_worker, args=(path, barrier, release, results)) for _ in range(4)]
    try:
        for worker in workers:
            worker.start()
        claimed = [results.get(timeout=15) for _ in workers]
        owners = [pid for pid in claimed if pid is not None]
        assert len(owners) == 1
        assert runtime_control.read_pid_file(path) == owners[0]
    finally:
        release.set()
        for worker in workers:
            if worker.pid is not None:
                worker.join(timeout=5)
                if worker.is_alive():
                    worker.terminate()
                    worker.join(timeout=5)
        results.close()
        results.join_thread()
    assert all(worker.exitcode == 0 for worker in workers)
    assert runtime_control.read_pid_file(path) is None


def test_parallel_start_creates_only_one_child(tmp_path, monkeypatch):
    pid_file = str(tmp_path / "worker.pid")
    live = set()
    spawned = []
    barrier = Barrier(4)

    def popen(*args, **kwargs):
        time.sleep(0.04)
        pid = 900000 + len(spawned)
        spawned.append(pid)
        live.add(pid)
        return SimpleNamespace(pid=pid)

    monkeypatch.setattr(runtime_control.subprocess, "Popen", popen)
    monkeypatch.setattr(runtime_control, "is_pid_running", lambda pid: pid in live)
    monkeypatch.setattr(runtime_control, "read_process_cmdline", lambda pid: "synthetic-worker")

    def start(_):
        barrier.wait(timeout=10)
        return runtime_control.start_background_process(["synthetic-worker"], pid_file=pid_file,
            log_file=str(tmp_path / "worker.log"), expected_tokens=("synthetic-worker",), start_delay=0)

    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(start, range(4)))
    assert len(spawned) == 1
    assert sum(result["already_running"] for result in results) == 3


def test_parallel_registration_has_single_owner(tmp_path, monkeypatch):
    path = str(tmp_path / "worker.pid")
    barrier = Barrier(4)
    original_read = runtime_control.read_pid_file

    def slow_read(path):
        result = original_read(path)
        if result is None:
            time.sleep(0.04)
        return result

    monkeypatch.setattr(runtime_control.os, "getpid", get_ident)
    monkeypatch.setattr(runtime_control, "read_pid_file", slow_read)
    monkeypatch.setattr(runtime_control, "is_pid_running", lambda pid: bool(pid))
    monkeypatch.setattr(runtime_control, "read_process_cmdline", lambda pid: "synthetic-worker")

    def register(_):
        barrier.wait(timeout=10)
        try:
            return runtime_control.register_current_process(path, expected_tokens=("synthetic-worker",), wait_timeout_sec=0)
        except RuntimeError:
            return None

    with ThreadPoolExecutor(max_workers=4) as pool:
        owners = [pid for pid in pool.map(register, range(4)) if pid is not None]
    assert len(owners) == 1
    assert runtime_control.read_pid_file(path) == owners[0]


def test_pid_read_permission_error_does_not_trigger_spawn(tmp_path, monkeypatch):
    path = tmp_path / "worker.pid"
    path.write_text("900000\n")
    import builtins
    real_open = builtins.open

    def denied(file, *args, **kwargs):
        if str(file) == str(path):
            raise PermissionError("synthetic read error")
        return real_open(file, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", denied)
    spawned = []
    monkeypatch.setattr(runtime_control.subprocess, "Popen", lambda *args, **kwargs: spawned.append(True))
    with pytest.raises(PermissionError):
        runtime_control.start_background_process(["synthetic"], pid_file=str(path), log_file=str(tmp_path / "worker.log"))
    assert not spawned


def test_failed_stop_keeps_owner_record(tmp_path, monkeypatch):
    path = str(tmp_path / "worker.pid")
    runtime_control.write_pid_file(path, 900000)
    monkeypatch.setattr(runtime_control, "is_pid_running", lambda pid: True)
    monkeypatch.setattr(runtime_control, "read_process_cmdline", lambda pid: "synthetic-worker")
    signals = []
    monkeypatch.setattr(runtime_control.os, "kill", lambda pid, sig: signals.append((pid, sig)))
    monkeypatch.setattr(runtime_control.time, "sleep", lambda delay: None)
    result = runtime_control.stop_process(path, expected_tokens=("synthetic-worker",), timeout=0)
    assert result["ok"] is False
    assert runtime_control.read_pid_file(path) == 900000


def test_graceful_shutdown_can_unregister_during_stop(tmp_path, monkeypatch):
    path = str(tmp_path / "worker.pid")
    runtime_control.write_pid_file(path, 900000)
    live = {900000}
    signals = []
    monkeypatch.setattr(runtime_control.os, "getpid", lambda: 900000)
    monkeypatch.setattr(runtime_control, "is_pid_running", lambda pid: pid in live)
    monkeypatch.setattr(runtime_control, "read_process_cmdline", lambda pid: "synthetic-worker")

    with ThreadPoolExecutor(max_workers=1) as pool:
        def kill(pid, sig):
            signals.append(sig)
            # This simulates a child's finally block, not an actual signal.
            cleanup = pool.submit(runtime_control.unregister_current_process, path)
            cleanup.result(timeout=3)
            live.discard(pid)

        monkeypatch.setattr(runtime_control.os, "kill", kill)
        result = runtime_control.stop_process(path, expected_tokens=("synthetic-worker",), timeout=1)
    assert result["ok"] is True
    assert signals == [runtime_control.signal.SIGTERM]
    assert runtime_control.read_pid_file(path) is None


def test_pid_publication_failure_cleans_only_new_child(tmp_path, monkeypatch):
    actions = []
    proc = SimpleNamespace(pid=900000, terminate=lambda: actions.append("terminate"),
        wait=lambda timeout: actions.append("wait"))
    monkeypatch.setattr(runtime_control.subprocess, "Popen", lambda *args, **kwargs: proc)

    def failed_write(*args):
        raise OSError("synthetic publication failure")

    monkeypatch.setattr(runtime_control, "write_pid_file", failed_write)
    with pytest.raises(OSError, match="publication failure"):
        runtime_control.start_background_process(["synthetic"], pid_file=str(tmp_path / "worker.pid"),
            log_file=str(tmp_path / "worker.log"), start_delay=0)
    assert actions == ["terminate", "wait"]


def test_start_releases_lifecycle_before_child_registration(tmp_path, monkeypatch):
    path = str(tmp_path / "worker.pid")
    monkeypatch.setattr(runtime_control.subprocess, "Popen", lambda *args, **kwargs: SimpleNamespace(pid=900000))
    monkeypatch.setattr(runtime_control.os, "getpid", lambda: 900000)
    monkeypatch.setattr(runtime_control, "is_pid_running", lambda pid: pid == 900000)
    monkeypatch.setattr(runtime_control, "read_process_cmdline", lambda pid: "synthetic-worker")
    registered = []

    with ThreadPoolExecutor(max_workers=1) as pool:
        def startup_wait(delay):
            registered.append(pool.submit(runtime_control.register_current_process, path, wait_timeout_sec=0).result(timeout=3))

        monkeypatch.setattr(runtime_control.time, "sleep", startup_wait)
        result = runtime_control.start_background_process(["synthetic-worker"], pid_file=path,
            log_file=str(tmp_path / "worker.log"), expected_tokens=("synthetic-worker",), start_delay=0)
    assert result["ok"] is True
    assert registered == [900000]
