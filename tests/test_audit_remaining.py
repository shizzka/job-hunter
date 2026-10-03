"""Offline regressions for remaining runtime state and pagination issues."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent
import captcha_bridge
import client_hh_auth
import config
import facts
import notifier
import search_pipeline
from state_store.json_store import JsonStore
import state_store.json_store as json_store


@pytest.mark.parametrize("source", ["superjob", "habr", "geekjob"])
@pytest.mark.parametrize("first_page", [[], [{"id": "seen-1"}]])
def test_collection_continues_to_reported_next_page(monkeypatch, source, first_page):
    calls = []

    async def search(*args, page, **kwargs):
        calls.append(page)
        first = 0 if source == "superjob" else 1
        return (first_page if page == first else [{"id": "new-2"}]), (page == 0 if source == "superjob" else 2)

    monkeypatch.setattr(config, "SUPERJOB_ENABLED", True)
    monkeypatch.setattr(config, "SUPERJOB_API_KEY", "test-key")
    monkeypatch.setattr(config, "SUPERJOB_SEARCH_PROFILES", [{}])
    monkeypatch.setattr(config, "SUPERJOB_SEARCH_QUERIES", ["QA"])
    monkeypatch.setattr(config, "SUPERJOB_SEARCH_PAGES", 10)
    monkeypatch.setattr(config, "HABR_ENABLED", True)
    monkeypatch.setattr(config, "HABR_SEARCH_PATHS", ["/vacancies"])
    monkeypatch.setattr(config, "HABR_SEARCH_PAGES", 10)
    monkeypatch.setattr(config, "GEEKJOB_ENABLED", True)
    monkeypatch.setattr(config, "GEEKJOB_SEARCH_PAGES", 10)
    monkeypatch.setattr(search_pipeline.seen, "is_seen", lambda vid: vid.startswith("seen"))
    collector = getattr(search_pipeline, f"collect_{source}_vacancies")

    result = asyncio.run(collector(SimpleNamespace(search_vacancies=search)))

    assert [v["id"] for v in result] == ["new-2"]
    assert calls == ([0, 1] if source == "superjob" else [1, 2])


def test_runtime_status_is_atomic_and_private(tmp_path, monkeypatch):
    target = tmp_path / "runtime.json"
    monkeypatch.setattr(config, "RUNTIME_STATUS_FILE", str(target))
    agent._write_runtime_status("search", "ready", "idle", "test")

    assert json.loads(target.read_text())["status"] == "idle"
    assert target.stat().st_mode & 0o777 == 0o600
    original = target.read_bytes()
    monkeypatch.setattr(json_store.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("failed")))
    agent._write_runtime_status("search", "busy", "busy", "test")
    assert target.read_bytes() == original


def test_facts_failed_replace_retains_previous_and_backup(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RESUME_FILE", str(tmp_path / "resume.md"))
    facts.save_facts({"confirmed": "previous"})
    target = tmp_path / "facts.json"
    original = target.read_bytes()
    replace = json_store.os.replace

    def fail_target(source, destination):
        if Path(destination) == target:
            raise OSError("failed")
        return replace(source, destination)

    monkeypatch.setattr(json_store.os, "replace", fail_target)
    with pytest.raises(OSError):
        facts.save_facts({"confirmed": "new"})

    assert target.read_bytes() == original
    backups = list(tmp_path.glob("facts.json.bak.*"))
    assert len(backups) == 1
    assert json.loads(backups[0].read_text()) == {"confirmed": "previous"}
    assert backups[0].stat().st_mode & 0o777 == 0o600


def test_corrupt_facts_are_preserved_on_explicit_reextraction(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "RESUME_FILE", str(tmp_path / "resume.md"))
    target = tmp_path / "facts.json"
    target.write_text("{broken")

    facts.save_facts({"confirmed": "new"})

    assert facts.load_facts() == {"confirmed": "new"}
    assert next(tmp_path.glob("facts.json.corrupt-*")).read_text() == "{broken"


def test_json_read_permission_error_does_not_reset_valid_state(tmp_path, monkeypatch):
    target = tmp_path / "state.json"
    target.write_text('{"valuable": true}')
    path_open = Path.open

    def denied(path, *args, **kwargs):
        if path == target:
            raise PermissionError("denied")
        return path_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(PermissionError):
        JsonStore(target).update(lambda state: {"replacement": True})
    monkeypatch.undo()
    assert target.read_text() == '{"valuable": true}'
    assert not list(tmp_path.glob("state.json.corrupt-*"))


def test_captcha_responses_are_private(tmp_path, monkeypatch):
    monkeypatch.setattr(captcha_bridge, "_state_dir", lambda profile_name=None: str(tmp_path))
    request_id = captcha_bridge.create_request("synthetic.png", profile_name="qa")
    captcha_bridge.write_response(request_id, "answer", profile_name="qa")
    assert (tmp_path / "captcha_response.json").stat().st_mode & 0o777 == 0o600


def test_resume_catalog_and_export_are_atomic_and_private(tmp_path, monkeypatch):
    target = tmp_path / "hh_resumes.json"
    monkeypatch.setattr(client_hh_auth, "hh_resume_catalog_path", lambda name: str(target))
    client_hh_auth._save_resume_catalog("qa", [{"id": "old"}])
    export = tmp_path / "resume.md"
    client_hh_auth._write_text(str(export), "private resume")
    assert target.stat().st_mode & 0o777 == 0o600
    assert export.stat().st_mode & 0o777 == 0o600
    original_catalog, original_export = target.read_bytes(), export.read_bytes()
    monkeypatch.setattr(json_store.os, "replace", lambda *args: (_ for _ in ()).throw(OSError("failed")))
    with pytest.raises(OSError):
        client_hh_auth._save_resume_catalog("qa", [{"id": "new"}])
    with pytest.raises(OSError):
        client_hh_auth._write_text(str(export), "new resume")
    assert target.read_bytes() == original_catalog
    assert export.read_bytes() == original_export


def test_invalid_explicit_captcha_profile_cannot_fall_back(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "HH_STATE_DIR", str(tmp_path / "default"))
    with pytest.raises(ValueError):
        captcha_bridge._state_dir("../invalid")
    assert not (tmp_path / "default").exists()


def test_named_profile_missing_auth_hints_never_uses_global_account(tmp_path, monkeypatch):
    global_env = tmp_path / "global.env"
    global_env.write_text("HH_AUTH_LOGIN=admin@example.test\n")
    own_env = tmp_path / "profile.env"
    own_env.write_text("# No login hints for this candidate\n")
    monkeypatch.setenv("JOB_HUNTER_ENV_FILE", str(global_env))
    monkeypatch.setenv("HH_AUTH_PHONE", "+79999999999")
    monkeypatch.setattr(client_hh_auth, "_resolve_profile", lambda name: SimpleNamespace(home_dir=str(tmp_path)))

    assert client_hh_auth._load_hh_auth_env("candidate") == {}


def test_cookie_notifications_are_isolated_and_retry_after_failed_delivery(tmp_path, monkeypatch):
    for setting in ("HH_COOKIES_FILE", "SUPERJOB_COOKIES_FILE", "HABR_COOKIES_FILE", "GEEKJOB_COOKIES_FILE"):
        monkeypatch.setattr(config, setting, str(tmp_path / "missing.json"))
    homes = [tmp_path / "qa", tmp_path / "other"]
    sent = []
    deliver = [False, True, True]

    async def send(text):
        sent.append(text)
        return deliver.pop(0)

    monkeypatch.setattr(notifier, "send_message", send)
    monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(homes[0]))
    for setting in ("HH_ENABLED", "SUPERJOB_ENABLED", "HABR_ENABLED", "GEEKJOB_ENABLED"):
        monkeypatch.setattr(config, setting, True)
    clock = [1_800_000_000.0]
    monkeypatch.setattr(notifier.time, "time", lambda: clock[0])
    asyncio.run(notifier.notify_stale_cookies())
    asyncio.run(notifier.notify_stale_cookies())
    assert len(sent) == 1
    clock[0] += notifier._COOKIE_WARN_RETRY_INTERVAL
    asyncio.run(notifier.notify_stale_cookies())
    asyncio.run(notifier.notify_stale_cookies())
    monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(homes[1]))
    asyncio.run(notifier.notify_stale_cookies())

    assert len(sent) == 3
    for home in homes:
        assert (home / "cookie_warn_sent.json").stat().st_mode & 0o777 == 0o600
