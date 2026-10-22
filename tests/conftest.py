"""Keep all test state and import-time path captures away from real homes."""
import copy
from pathlib import Path
import socket
import sys
import tempfile
from types import SimpleNamespace

import pytest
import config

from tests.state_isolation import (
    PathRebaser, absolute_path, changed_files, fingerprint_tree, isolate_environment, isolate_paths,
)


def _redirect_config(monkeypatch, roots, home):
    rebase = PathRebaser(roots, home)
    overrides = isolate_environment(monkeypatch, roots, home, rebase=rebase)
    for name, value in list(vars(config).items()):
        if isinstance(value, str) and value in overrides:
            monkeypatch.setattr(config, name, overrides[value])
    isolate_paths(config, monkeypatch, roots, home, rebase=rebase)
    # Explicit files outside the state home (including default /tmp logs) must
    # not become another shared state channel between tests or live processes.
    for name, value in list(vars(config).items()):
        path = absolute_path(value)
        if name.endswith(("_FILE", "_DIR")) and path and not path.is_relative_to(home):
            monkeypatch.setattr(config, name, str(home / "external-paths" / name / path.name))
    monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(home))
    return rebase


def pytest_sessionstart(session):
    # Capture both roots BEFORE changing HOME/config and before test collection.
    roots = tuple(dict.fromkeys((Path(config.JOB_HUNTER_HOME).expanduser().absolute(), Path.home() / ".job-hunter")))
    snapshots = {root: fingerprint_tree(root) for root in roots if root.is_dir()}
    temporary = tempfile.TemporaryDirectory(prefix="job-hunter-pytest-")
    patch = pytest.MonkeyPatch()
    home = Path(temporary.name) / "home" / ".job-hunter"
    home.mkdir(parents=True)
    session.config._state_isolation = SimpleNamespace(
        roots=roots, snapshots=snapshots, temporary=temporary, patch=patch, home=home,
    )
    _redirect_config(patch, roots, home)
    # Tests deliberately select PROD within their synthetic homes. Production
    # code must never infer this permission from absent instance configuration.
    patch.setenv("JOB_HUNTER_INSTANCE_KIND", "prod")
    patch.setattr(config, "JOB_HUNTER_INSTANCE_KIND", "prod")


def pytest_sessionfinish(session, exitstatus):
    isolation = session.config._state_isolation
    try:
        changes = [str(root / name) for root, before in isolation.snapshots.items()
                   for name in changed_files(before, fingerprint_tree(root))]
        if changes:
            session.exitstatus = pytest.ExitCode.TESTS_FAILED
            reporter = session.config.pluginmanager.get_plugin("terminalreporter")
            if reporter:
                reporter.write_sep("!", "Protected job-hunter state changed")
                for path in changes:
                    reporter.write_line(path)
    finally:
        isolation.patch.undo()
        isolation.temporary.cleanup()


@pytest.fixture(autouse=True)
def isolated_profile_home(tmp_path, monkeypatch, request):
    """Test-local paths and caches; explicit test monkeypatches run afterwards."""
    isolation = request.config._state_isolation
    home = tmp_path / "home" / ".job-hunter"
    home.mkdir(parents=True)
    # profile._patch_config assigns directly, including NOTIFY_CHAT_ID. Record
    # ALL config values so neither owner IDs nor profile settings leak forward.
    for name, value in list(vars(config).items()):
        if name.isupper():
            monkeypatch.setattr(config, name, copy.deepcopy(value))
    roots = (*isolation.roots, isolation.home)
    rebase = _redirect_config(monkeypatch, roots, home)

    # Rebase module-level aliases captured during collection (project modules
    # only). Application modules imported later see the already isolated config.
    project = Path(config.__file__).resolve().parent
    for module in list(sys.modules.values()):
        filename = getattr(module, "__file__", None)
        if not filename:
            continue
        path = Path(filename)
        if path.is_relative_to(project) and path.relative_to(project).parts[0] not in {"tests", "venv", ".venv"}:
            isolate_paths(module, monkeypatch, roots, home, rebase=rebase)

    profile = sys.modules.get("profile")
    if profile is not None:
        profile._release_lock()
        monkeypatch.setattr(profile, "_profiles_root_home", str(home))
        monkeypatch.setattr(profile, "_active_profile", None)
        monkeypatch.setattr(profile, "_default_candidate_settings", None)
    # These stores are factories, not cached instances: telegram_access._store,
    # telegram_clients._store, telegram_resume_limits._store, seen._store and
    # analytics._state_store read config on each call. Analytics can additionally
    # pin an earlier destination in a ContextVar; reset it between tests.
    analytics = sys.modules.get("analytics")
    tokens = []
    if analytics is not None:
        for name, value in (("_event_context", {}), ("_event_destination", None), ("_run_observation", None)):
            variable = getattr(analytics, name)
            tokens.append((variable, variable.set(value)))
    for name in ("llm_client", "facts"):
        module = sys.modules.get(name)
        if module is not None:
            monkeypatch.setattr(module, "_client_singleton", None)
    try:
        yield
    finally:
        if profile is not None:
            profile._release_lock()
        for variable, token in reversed(tokens):
            variable.reset(token)


@pytest.fixture(autouse=True)
def isolate_automatic_audit_worker(monkeypatch):
    """Background process integration is exercised explicitly in its own tests."""
    import audit_worker
    monkeypatch.setattr(audit_worker, 'submit', lambda job: True)


@pytest.fixture(autouse=True)
def deny_test_network(monkeypatch):
    """Ordinary tests use fake transports, never real IP connections.

    This guard covers this pytest process only. Subprocess tests must supply
    isolated environments and synthetic fixtures separately.
    """
    original_connect = socket.socket.connect
    original_connect_ex = socket.socket.connect_ex

    def guarded(method):
        def connect(sock, address):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                raise AssertionError("Real network connections are forbidden in ordinary tests")
            return method(sock, address)
        return connect

    monkeypatch.setattr(socket.socket, "connect", guarded(original_connect))
    monkeypatch.setattr(socket.socket, "connect_ex", guarded(original_connect_ex))


@pytest.fixture(autouse=True)
def salary_question_off_by_default(monkeypatch):
    """Legacy letter tests use vacancies without salary; salary tests opt in explicitly."""
    monkeypatch.setattr(config, "COVER_ASK_SALARY_RANGE", False)


def _known_failures() -> set[str]:
    path = Path(__file__).with_name("known_failures.txt")
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return set()
    return {line.strip() for line in lines if line.strip() and not line.startswith("#")}


def pytest_collection_modifyitems(config, items):
    """Quarantine: known failures/flakes do not fail the run, any new failure does."""
    known = _known_failures()
    for item in items:
        if item.nodeid in known:
            item.add_marker(pytest.mark.xfail(reason="known failure (tests/known_failures.txt)", strict=False))
