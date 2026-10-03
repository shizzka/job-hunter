"""Registry mutations must preserve durable state across failures and writers."""
import multiprocessing
import stat
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

import config
import runtime_control
import telegram_access
import telegram_clients
import telegram_resume_limits


def _process_usage_worker(path, barrier):
    config.TELEGRAM_AI_LIMITS_FILE = str(path)
    barrier.wait(timeout=10)
    for _ in range(10):
        telegram_resume_limits.record_resume_analysis(42, profile_name="synthetic")


@pytest.fixture
def registry_paths(tmp_path, monkeypatch):
    paths = {}
    for module, setting in (
        (telegram_access, "TELEGRAM_ACCESS_FILE"),
        (telegram_clients, "TELEGRAM_CLIENTS_FILE"),
        (telegram_resume_limits, "TELEGRAM_AI_LIMITS_FILE"),
    ):
        path = tmp_path / (setting.lower() + ".json")
        monkeypatch.setattr(config, setting, str(path))
        paths[module] = path
    monkeypatch.setattr(config, "NOTIFY_CHAT_ID", 0)
    monkeypatch.setattr(config, "TELEGRAM_AI_FREE_ANALYSES", 1)
    return paths


@pytest.mark.parametrize("module", [telegram_access, telegram_clients, telegram_resume_limits])
@pytest.mark.parametrize("content", [
    b'{"broken":', b'[]', b'\xff', b'{"users":{},"clients":{}}', b'{}',
    b'{"users":[null],"clients":[null]}', b'{"users":[{}],"clients":[{}]}',
    b'{"users":[{"user_id":42},{"user_id":42}],"clients":[{"user_id":42},{"user_id":42}]}',
])
def test_corrupt_registry_is_not_replaced_with_empty_state(registry_paths, module, content):
    path = registry_paths[module]
    path.write_bytes(content)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="[Rr]estor|[Cc]orrupt|[Ss]chema"):
            module.load_registry()
        assert path.read_bytes() == content


@pytest.mark.parametrize("module", [telegram_access, telegram_clients, telegram_resume_limits])
def test_registry_mutation_preserves_permission_errors(registry_paths, module, monkeypatch):
    from pathlib import Path
    path = registry_paths[module]
    original = '{"users":[],"clients":[],"events":[]}'
    path.write_text(original)
    original_open = Path.open

    def denied(self, *args, **kwargs):
        if self == path:
            raise PermissionError("synthetic read failure")
        return original_open(self, *args, **kwargs)

    # Also intercept the original runtime reader, which uses builtin open.
    import builtins
    real_open = builtins.open

    def denied_builtin(file, mode="r", *args, **kwargs):
        if str(file) == str(path) and "r" in mode:
            raise PermissionError("synthetic read failure")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    monkeypatch.setattr(builtins, "open", denied_builtin)
    with pytest.raises(PermissionError):
        module.load_registry()
    with real_open(path) as stream:
        assert stream.read() == original


def test_runtime_json_serialization_failure_preserves_previous_file(tmp_path):
    path = tmp_path / "runtime.json"
    runtime_control.write_json_file(str(path), {"pid": 12, "status": "idle"})
    before = path.read_bytes()
    with pytest.raises(TypeError):
        runtime_control.write_json_file(str(path), {"status": "busy", "invalid": {1, 2}})
    assert path.read_bytes() == before


@pytest.mark.parametrize("kind", ["json", "pid"])
def test_runtime_files_are_private_and_atomic(tmp_path, monkeypatch, kind):
    from state_store import json_store
    path = tmp_path / kind
    writer = runtime_control.write_json_file if kind == "json" else runtime_control.write_pid_file
    old = {"pid": 42} if kind == "json" else 42
    new = {"pid": 84} if kind == "json" else 84
    writer(str(path), old)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    before = path.read_bytes()
    real_replace = json_store.os.replace

    def failed_replace(src, dst):
        if str(dst) == str(path):
            raise OSError("synthetic replace failure")
        return real_replace(src, dst)

    monkeypatch.setattr(json_store.os, "replace", failed_replace)
    with pytest.raises(OSError):
        writer(str(path), new)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob(f".{path.name}.*.tmp"))


def test_parallel_usage_and_bonus_updates_are_not_lost(registry_paths):
    telegram_resume_limits.get_user_snapshot(42)
    barrier = Barrier(6)

    def worker(index):
        barrier.wait(timeout=10)
        for _ in range(8):
            if index % 2:
                telegram_resume_limits.grant_bonus(42, amount=1, actor_user_id=index)
            else:
                telegram_resume_limits.record_resume_analysis(42, profile_name="synthetic")

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(worker, range(6)))
    snapshot = telegram_resume_limits.get_user_snapshot(42)
    assert snapshot["free_used"] == 24
    assert snapshot["bonus_total"] == 24
    assert snapshot["analysis_total"] == 24
    assert len(telegram_resume_limits.recent_events(100)) == 48


def test_usage_updates_are_locked_across_processes(registry_paths):
    telegram_resume_limits.get_user_snapshot(42)
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(4)
    workers = [context.Process(target=_process_usage_worker, args=(registry_paths[telegram_resume_limits], barrier)) for _ in range(4)]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=15)
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
            if worker.pid is not None:
                worker.join(timeout=5)
    snapshot = telegram_resume_limits.get_user_snapshot(42)
    assert snapshot["free_used"] == 40
    assert snapshot["analysis_total"] == 40
    assert len(telegram_resume_limits.recent_events(100)) == 40


@pytest.mark.parametrize("module", [telegram_access, telegram_clients, telegram_resume_limits])
def test_registry_replace_failure_preserves_existing_data(registry_paths, module, monkeypatch):
    from state_store import json_store
    path = registry_paths[module]

    def mutate():
        if module is telegram_access:
            return module.upsert_user(42, profile="synthetic")
        if module is telegram_clients:
            return module.upsert_client(42, full_name="Synthetic Candidate")
        return module.record_resume_analysis(42, profile_name="synthetic")

    mutate()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    before = path.read_bytes()
    real_replace = json_store.os.replace

    def failed_replace(src, dst):
        if str(dst) == str(path):
            raise OSError("synthetic replace failure")
        return real_replace(src, dst)

    monkeypatch.setattr(json_store.os, "replace", failed_replace)
    with pytest.raises(OSError):
        mutate()
    assert path.read_bytes() == before
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))


@pytest.mark.parametrize("module", [telegram_access, telegram_clients])
def test_parallel_user_insertions_are_not_lost(registry_paths, module):
    module.load_registry()
    barrier = Barrier(6)

    def worker(index):
        barrier.wait(timeout=10)
        for offset in range(6):
            user_id = 1000 + index * 10 + offset
            if module is telegram_access:
                module.upsert_user(user_id, profile="synthetic")
            else:
                module.upsert_client(user_id, full_name="Synthetic Candidate")

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(worker, range(6)))
    registry = module.load_registry()
    items = registry["users" if module is telegram_access else "clients"]
    assert len(items) == 36
    assert len({item["user_id"] for item in items}) == 36


def test_client_onboarding_keeps_approval_and_profile(registry_paths):
    telegram_clients.upsert_client(42, status="approved", profile_name="synthetic", auth_status="cookies_ready")
    result = telegram_clients.start_onboarding(42, username="synthetic_user")
    assert result["status"] == "approved"
    assert result["profile_name"] == "synthetic"
    assert result["auth_status"] == "cookies_ready"


@pytest.mark.parametrize("operation", ["onboarding", "application"])
def test_client_transition_reads_latest_profile_inside_transaction(registry_paths, monkeypatch, operation):
    from state_store.registry import RegistryStore
    telegram_clients.upsert_client(42, status="onboarding", profile_name="old")
    original_update = RegistryStore.update
    injected = False

    def update_after_admin_approval(store, mutator):
        nonlocal injected
        if not injected:
            injected = True
            telegram_clients.set_status(42, status="approved", profile_name="new", auth_status="cookies_ready")
        return original_update(store, mutator)

    monkeypatch.setattr(RegistryStore, "update", update_after_admin_approval)
    if operation == "onboarding":
        result = telegram_clients.start_onboarding(42, username="synthetic_user")
        assert result["status"] == "approved"
    else:
        result = telegram_clients.submit_application(42, full_name="Synthetic Candidate", target_role="QA")
        assert result["status"] == "pending_review"
    assert result["profile_name"] == "new"
    assert result["auth_status"] == "cookies_ready"


def test_access_owner_normalization_is_preserved(registry_paths, monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_CHAT_ID", 42)
    telegram_access.load_registry()
    telegram_access.remove_user(42)
    assert telegram_access.resolve_user(42)["role"] == "admin"


def test_access_owner_bootstrap_does_not_mask_invalid_identity(registry_paths, monkeypatch):
    monkeypatch.setattr(config, "NOTIFY_CHAT_ID", 42)
    path = registry_paths[telegram_access]
    before = b'{"users":[{"user_id":"invalid"}]}'
    path.write_bytes(before)
    with pytest.raises(RuntimeError, match="Corrupt registry"):
        telegram_access.load_registry()
    assert path.read_bytes() == before


def test_pid_cleanup_does_not_remove_a_different_owner(tmp_path):
    path = tmp_path / "process.pid"
    runtime_control.write_pid_file(str(path), 84)
    runtime_control.remove_pid_file(str(path), 42)
    assert runtime_control.read_pid_file(str(path)) == 84
    runtime_control.remove_pid_file(str(path), 84)
    assert not path.exists()
