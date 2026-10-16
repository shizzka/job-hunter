"""Regressions for the test harness, using synthetic state only."""
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

import config
import telegram_access
from tests.state_isolation import HASH_LIMIT, changed_files, fingerprint_tree, rebase_path

PROJECT = Path(__file__).resolve().parents[1]


def test_default_registry_read_is_isolated(tmp_path):
    # RegistryStore.load() writes on first read and can bootstrap an admin owner.
    telegram_access.load_registry()
    assert Path(config.TELEGRAM_ACCESS_FILE).is_relative_to(tmp_path)


# Exercise collection and sentinel hooks in a fresh process, with synthetic homes.
@pytest.fixture
def harness(tmp_path):
    suite = tmp_path / "suite"
    suite.mkdir()
    shutil.copyfile(PROJECT / "tests/conftest.py", suite / "conftest.py")
    real_home = tmp_path / "synthetic-real-home"
    state = real_home / ".job-hunter"
    state.mkdir(parents=True)
    env = {name: value for name, value in os.environ.items()
           if not (name.startswith("JOB_HUNTER_") and name.endswith(("_FILE", "_DIR", "_HOME")))}
    env.update(HOME=str(real_home), JOB_HUNTER_HOME=str(state),
               PYTHONPATH=str(PROJECT), PYTEST_DISABLE_PLUGIN_AUTOLOAD="1")

    def run(source, *, environment=None):
        (suite / "test_probe.py").write_text(source)
        output = tmp_path / "child-pytest.log"
        with output.open("w") as stream:
            result = subprocess.run(
                [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
                 "--confcutdir", str(suite), str(suite)],
                cwd=PROJECT, env=environment or env, stdout=stream,
                stderr=subprocess.STDOUT, timeout=45,
            )
        return result.returncode, output.read_text()

    return state, env, run


def test_guard_fails_session_for_protected_file_change(harness):
    state, env, run = harness
    target = state / "telegram-users.json"
    target.write_text('{"users": []}')
    env["SYNTHETIC_REAL_STATE"] = str(state)
    code, output = run('''
import os
from pathlib import Path

def test_synthetic_accident():
    (Path(os.environ["SYNTHETIC_REAL_STATE"]) / "telegram-users.json").write_text('{"users": [777]}')
''')
    assert code == 1, output
    assert "1 passed" in output
    assert "Protected job-hunter state changed" in output
    assert str(target) in output


def test_guard_allows_live_runtime_updates_and_leaves_no_artifacts(harness):
    state, env, run = harness
    target = state / "telegram-users.json"
    target.write_text('{"users": []}')
    before = target.stat()
    env["SYNTHETIC_REAL_STATE"] = str(state)
    code, output = run('''
import os
from pathlib import Path

def test_live_runtime():
    root = Path(os.environ["SYNTHETIC_REAL_STATE"])
    for relative in ["telegram-bot-runtime.json", "telegram-bot-state.json", "runtime_status.json",
                     "job-hunter-errors.log", "job-hunter.log.1", ".telegram-users.json.lock",
                     "state/request.json", "profiles/alice/traces/run/trace.jsonl"]:
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("synthetic live update")
''')
    assert code == 0, output
    assert target.read_text() == '{"users": []}'
    assert (target.stat().st_size, target.stat().st_mtime_ns) == (before.st_size, before.st_mtime_ns)
    # No sentinel lock/cache/manifest gets written into the protected tree.
    assert sorted(p.name for p in state.iterdir()) == [
        ".telegram-users.json.lock", "job-hunter-errors.log", "job-hunter.log.1",
        "profiles", "runtime_status.json", "state", "telegram-bot-runtime.json",
        "telegram-bot-state.json", "telegram-users.json",
    ]


def test_import_time_paths_env_overrides_and_subsequent_test_are_isolated(harness):
    state, env, run = harness
    other = state.parent / "another-state-root"
    other.mkdir()
    env["JOB_HUNTER_HOME"] = str(other)
    env["JOB_HUNTER_TELEGRAM_ACCESS_FILE"] = str(state / "telegram-users.json")
    external = state.parent / "external-users.json"
    env["JOB_HUNTER_TELEGRAM_CLIENTS_FILE"] = str(external)
    env["SYNTHETIC_REAL_STATE"] = str(state)
    code, output = run('''
import importlib
import os
from pathlib import Path
import config
import profile
import telegram_access
import telegram_clients
import analytics
import llm_client
import facts
import sys
from types import ModuleType

cache = ModuleType("isolation_cache_probe")
cache.__file__ = config.__file__
cache.CAPTURED = config.TELEGRAM_ACCESS_FILE
sys.modules[cache.__name__] = cache

# This alias captures config before any function-scoped fixture runs.
COLLECTED_PATH = config.TELEGRAM_ACCESS_FILE
assert not Path(COLLECTED_PATH).is_relative_to(Path(os.environ["SYNTHETIC_REAL_STATE"]))
INITIAL_OWNER = config.NOTIFY_CHAT_ID

def test_first(tmp_path):
    assert Path(config.TELEGRAM_ACCESS_FILE).is_relative_to(tmp_path)
    telegram_access.load_registry()
    telegram_clients.load_registry()
    assert Path(cache.CAPTURED).is_relative_to(tmp_path)
    analytics._event_destination.set((str(Path(os.environ["SYNTHETIC_REAL_STATE"]) / "events.jsonl"), "stale", "stale"))
    llm_client._client_singleton = object()
    facts._client_singleton = object()
    config.NOTIFY_CHAT_ID = 777
    profile._active_profile = profile.Profile(home_dir=str(tmp_path / "stale"))

def test_second(tmp_path):
    assert config.NOTIFY_CHAT_ID == INITIAL_OWNER
    assert Path(profile.active().home_dir).is_relative_to(tmp_path)
    assert Path(cache.CAPTURED).is_relative_to(tmp_path)
    assert analytics._event_destination.get() is None
    assert llm_client._client_singleton is None
    assert facts._client_singleton is None
    importlib.reload(config)
    for value in (config.JOB_HUNTER_HOME, config.TELEGRAM_ACCESS_FILE, config.TELEGRAM_CLIENTS_FILE):
        assert Path(value).is_relative_to(tmp_path)
    telegram_access.load_registry()
    telegram_clients.load_registry()
''')
    assert code == 0, output
    assert not list(state.iterdir())
    assert not list(other.iterdir())
    assert not external.exists()


def test_rebase_uses_path_boundaries_preserves_layout_and_handles_symlinks(tmp_path):
    root = tmp_path / "real"
    root.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(root, target_is_directory=True)
    destination = tmp_path / "isolated"
    roots = (root,)
    assert rebase_path(str(root / "profiles/alice/hh_cookies.json"), roots, destination) == str(destination / "profiles/alice/hh_cookies.json")
    assert rebase_path(alias / "nested/state.json", roots, destination) == destination / "nested/state.json"
    sibling = str(tmp_path / "real-backup/users.json")
    assert rebase_path(sibling, roots, destination) == sibling
    assert rebase_path("https://example.test/file", roots, destination) == "https://example.test/file"


@pytest.mark.parametrize("relative", [
    "telegram-users.json", "telegram-clients.json", "telegram-ai-limits.json",
    ".env", "job-hunter.env", "profiles/alice/profile.env", "profiles/alice/hh_cookies.json",
    "profiles/alice/hh_apply_attempts.json", "seen_vacancies.json", "manual_apply_queue.json",
    "profiles/alice/matcher_deferred.json", "analytics_state.json", "future-unknown-state.json",
])
def test_guard_protects_persistent_files_even_with_restored_size_and_mtime(tmp_path, relative):
    path = tmp_path / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("before")
    before = fingerprint_tree(tmp_path)
    stat = path.stat()
    path.write_text("after!")
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    assert changed_files(before, fingerprint_tree(tmp_path)) == [relative]
    path.unlink()
    assert changed_files(before, fingerprint_tree(tmp_path)) == [relative]
    assert changed_files({}, before) == [relative]


def test_guard_keeps_metadata_for_large_non_log_files(tmp_path):
    path = tmp_path / "large-state.json"
    path.write_bytes(b"x" * (HASH_LIMIT + 1))
    before = fingerprint_tree(tmp_path)
    assert before[path.name][2] is None
    with path.open("ab") as stream:
        stream.write(b"x")
    assert changed_files(before, fingerprint_tree(tmp_path)) == [path.name]


def test_guard_missing_home_is_read_only_noop(tmp_path):
    missing = tmp_path / "missing"
    assert fingerprint_tree(missing) == {}
    assert not missing.exists()


def test_all_config_state_paths_are_local_including_future_constants(tmp_path, monkeypatch, request):
    from tests.state_isolation import isolate_paths
    from types import SimpleNamespace

    isolation = request.config._state_isolation
    for name, value in vars(config).items():
        if name.endswith(("_FILE", "_DIR", "_HOME")) and isinstance(value, str) and Path(value).is_absolute():
            assert Path(value).is_relative_to(tmp_path), (name, value)
    future = SimpleNamespace(UNEXPECTED_NAME=str(isolation.roots[0] / "future/nested/state.json"))
    isolate_paths(future, monkeypatch, isolation.roots, tmp_path)
    assert future.UNEXPECTED_NAME == str(tmp_path / "future/nested/state.json")


def test_guard_follows_linked_profiles_without_directory_cycles(tmp_path):
    root = tmp_path / "synthetic-home"
    profiles = root / "profiles"
    profiles.mkdir(parents=True)
    linked_profile = tmp_path / "profile-storage"
    linked_profile.mkdir()
    env = linked_profile / "profile.env"
    env.write_text("before")
    (profiles / "alice").symlink_to(linked_profile, target_is_directory=True)
    (linked_profile / "loop").symlink_to(root, target_is_directory=True)
    before = fingerprint_tree(root)
    env.write_text("after!")
    assert changed_files(before, fingerprint_tree(root)) == ["profiles/alice/profile.env"]
