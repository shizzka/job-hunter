"""Offline regression checks for private text and profile.env writers."""
import asyncio
import builtins
import multiprocessing
import os
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import agent
import client_hh_auth
import config
import hh_resume_pipeline
import migrate_profile_note
import profile
import resume_analyzer
import setup_profile
from state_store import json_store


def _fault(monkeypatch, operation):
    def fail(*args, **kwargs):
        raise OSError("synthetic write failure")
    monkeypatch.setattr(json_store.os, operation, fail)


def _resume_client(on_download=lambda: None):
    async def download(chosen):
        on_download()
        return {"raw": "Synthetic resume\n", "title": "Synthetic role", "sections": []}
    return SimpleNamespace(
        get_resume_ids=AsyncMock(return_value=[{"id": "synthetic", "title": "Synthetic role"}]),
        download_resume_by_id=download,
    )


def test_download_keeps_original_profile_path_after_await(tmp_path, monkeypatch):
    own = tmp_path / "own" / "resume.md"
    other = tmp_path / "other" / "resume.md"
    other.parent.mkdir()
    other.write_text("Other candidate\n")
    monkeypatch.setattr(config, "RESUME_FILE", str(own))
    monkeypatch.setattr(hh_resume_pipeline, "enabled", lambda: False)
    client = _resume_client(lambda: monkeypatch.setattr(config, "RESUME_FILE", str(other)))
    asyncio.run(agent._save_resume_from_client(client))
    assert own.read_text() == "Synthetic resume\n"
    assert other.read_text() == "Other candidate\n"


@pytest.mark.parametrize("by_title", [False, True])
def test_download_keeps_original_resume_selection_after_await(tmp_path, monkeypatch, by_title):
    own = tmp_path / "own.md"
    other = tmp_path / "other.md"
    other.write_text("Other candidate\n")
    monkeypatch.setattr(config, "RESUME_FILE", str(own))
    variants = [{"name": "normal", "title": "Own role", "id": "" if by_title else "own"}]
    monkeypatch.setattr(hh_resume_pipeline, "enabled", lambda: True)
    monkeypatch.setattr(hh_resume_pipeline, "get_variants", lambda: [dict(item) for item in variants])
    async def get_ids():
        monkeypatch.setattr(config, "RESUME_FILE", str(other))
        variants[:] = [{"name": "normal", "title": "Other role", "id": "other"}]
        return [{"id": "other", "title": "Other role"}, {"id": "own", "title": "Own role"}]
    async def download(chosen):
        return {"raw": f"Selected {chosen['id']}\n", "title": chosen["title"], "sections": []}
    client = SimpleNamespace(get_resume_ids=get_ids, download_resume_by_id=download)
    asyncio.run(agent._save_resume_from_client(client))
    assert own.read_text() == "Selected own\n"
    assert other.read_text() == "Other candidate\n"


@pytest.mark.parametrize("operation", ["fsync", "replace"])
def test_download_write_failure_keeps_old_resume(tmp_path, monkeypatch, operation):
    target = tmp_path / "resume.md"
    target.write_text("Old synthetic resume\n")
    monkeypatch.setattr(config, "RESUME_FILE", str(target))
    monkeypatch.setattr(hh_resume_pipeline, "enabled", lambda: False)
    _fault(monkeypatch, operation)
    with pytest.raises(OSError, match="synthetic write failure"):
        asyncio.run(agent._save_resume_from_client(_resume_client()))
    assert target.read_text() == "Old synthetic resume\n"
    assert not list(tmp_path.glob(".*.tmp"))


def test_download_creates_private_resume(tmp_path, monkeypatch):
    target = tmp_path / "resume.md"
    monkeypatch.setattr(config, "RESUME_FILE", str(target))
    monkeypatch.setattr(hh_resume_pipeline, "enabled", lambda: False)
    asyncio.run(agent._save_resume_from_client(_resume_client()))
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def _cli_analysis(tmp_path, monkeypatch, filename="resume.md"):
    target = tmp_path / filename
    target.write_text("Synthetic resume\n")
    monkeypatch.setattr(config, "RESUME_FILE", str(target))
    monkeypatch.setattr(agent.sys, "argv", ["agent.py", "--analyze-resume"])
    monkeypatch.setattr(profile, "activate", lambda *args: None)
    monkeypatch.setattr(agent, "_configure_logging", lambda **kwargs: None)
    monkeypatch.setattr(agent, "_apply_source_selection", lambda *args: None)
    for name in ("close_office_session", "close_notify_session", "close_llm_client"):
        monkeypatch.setattr(agent, name, AsyncMock())
    monkeypatch.setattr(resume_analyzer, "analyze_resume_file", AsyncMock(return_value="Synthetic analysis\n"))
    return target


@pytest.mark.parametrize("filename,analysis_name", [("resume.md", "resume_analysis.md"), ("resume.txt", "resume.txt.analysis.md")])
def test_cli_analysis_keeps_filename_and_private_mode(tmp_path, monkeypatch, filename, analysis_name):
    _cli_analysis(tmp_path, monkeypatch, filename)
    asyncio.run(agent.main())
    analysis = tmp_path / analysis_name
    assert analysis.read_text() == "Synthetic analysis\n"
    assert stat.S_IMODE(analysis.stat().st_mode) == 0o600


@pytest.mark.parametrize("operation", ["fsync", "replace"])
def test_cli_analysis_failure_preserves_previous_result(tmp_path, monkeypatch, operation):
    _cli_analysis(tmp_path, monkeypatch)
    analysis = tmp_path / "resume_analysis.md"
    analysis.write_text("Previous analysis\n")
    _fault(monkeypatch, operation)
    with pytest.raises(OSError, match="synthetic write failure"):
        asyncio.run(agent.main())
    assert analysis.read_text() == "Previous analysis\n"


def _wizard(tmp_path, monkeypatch, *, on_resume=lambda: None, analysis=False):
    answers = iter(["synthetic", "", "20"])
    monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))
    monkeypatch.setattr(config, "LLM_API_KEY", "synthetic-offline" if analysis else "")
    monkeypatch.setattr(setup_profile, "_ask", lambda *args: next(answers))
    monkeypatch.setattr(setup_profile, "_ask_list", lambda *args, **kwargs: ["Synthetic role"])
    def resume():
        on_resume()
        return "Synthetic resume\n"
    monkeypatch.setattr(setup_profile, "_ask_resume", resume)
    monkeypatch.setattr(setup_profile, "_ask_source", lambda key: {"enabled": key == "hh", "has_resume": True})
    monkeypatch.setattr(setup_profile, "_ask_yn", lambda prompt, **kwargs: prompt.startswith("Проанализировать"))
    monkeypatch.setattr(profile, "load_profile", lambda name: SimpleNamespace(name=name))
    monkeypatch.setattr(resume_analyzer, "analyze_resume", AsyncMock(return_value="Synthetic analysis\n"))
    monkeypatch.setattr(setup_profile.subprocess, "run", lambda *args, **kwargs: pytest.fail("Unexpected browser/login launch"))
    return tmp_path / "profiles" / "synthetic"


def test_wizard_creates_private_env_resume_and_analysis(tmp_path, monkeypatch):
    directory = _wizard(tmp_path, monkeypatch, analysis=True)
    setup_profile.run_wizard()
    for name in ("profile.env", "resume.md", "resume_analysis.md"):
        assert stat.S_IMODE((directory / name).stat().st_mode) == 0o600
    assert (directory / "resume.md").read_text() == "Synthetic resume\n"
    assert (directory / "resume_analysis.md").read_text() == "Synthetic analysis\n"


def test_wizard_refuses_profile_created_while_prompting(tmp_path, monkeypatch):
    target = tmp_path / "profiles" / "synthetic" / "profile.env"
    def competing_creation():
        target.parent.mkdir(parents=True)
        target.write_text("OTHER=preserve\n")
    _wizard(tmp_path, monkeypatch, on_resume=competing_creation)
    with pytest.raises(FileExistsError):
        setup_profile.run_wizard()
    assert target.read_text() == "OTHER=preserve\n"
    assert not (target.parent / "resume.md").exists()


def test_profile_creation_does_not_clobber_late_creator(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))
    monkeypatch.setattr(profile, "load_profile", lambda name: SimpleNamespace(name=name))
    target = tmp_path / "profiles" / "synthetic" / "profile.env"
    target.parent.mkdir(parents=True)
    original = os.makedirs
    def competing_creation(path, **kwargs):
        original(path, **kwargs)
        target.write_text("OTHER=preserve\n")
    monkeypatch.setattr(profile.os, "makedirs", competing_creation)
    with pytest.raises(FileExistsError):
        profile.create_profile("synthetic")
    assert target.read_text() == "OTHER=preserve\n"


def _env(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))
    directory = tmp_path / "profiles" / "synthetic"
    directory.mkdir(parents=True)
    target = directory / "profile.env"
    target.write_text("# Keep comment\nOTHER=keep\n")
    monkeypatch.setattr(client_hh_auth, "_resolve_profile", lambda name: SimpleNamespace(home_dir=str(directory)))
    return target


def test_parallel_env_updates_keep_every_key(tmp_path, monkeypatch):
    target = _env(tmp_path, monkeypatch)
    original = profile.atomic_write_text
    def delayed_write(*args):
        time.sleep(0.025)
        return original(*args)
    monkeypatch.setattr(profile, "atomic_write_text", delayed_write)
    monkeypatch.setattr(client_hh_auth, "atomic_write_text", delayed_write)
    barrier = threading.Barrier(6)
    def update(index):
        barrier.wait(timeout=5)
        if index == 0:
            return client_hh_auth._update_profile_resume_ids("synthetic", [{"id": "s1", "title": "Synthetic\nrole"}])
        return profile.update_profile_env("synthetic", {f"KEY_{index}": index})
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(update, range(6)))
    content = target.read_text()
    for index in range(1, 6):
        assert f"KEY_{index}={index}\n" in content
    assert "HH_PRIMARY_RESUME_ID=s1\n" in content
    assert "HH_PRIMARY_RESUME_TITLE=Synthetic role\n" in content
    assert "# Keep comment\nOTHER=keep\n" in content


@pytest.mark.parametrize("operation", ["fsync", "replace"])
def test_env_failure_preserves_original(tmp_path, monkeypatch, operation):
    target = _env(tmp_path, monkeypatch)
    before = target.read_bytes()
    _fault(monkeypatch, operation)
    with pytest.raises(OSError, match="synthetic write failure"):
        profile.update_profile_env("synthetic", {"NEW": "value"})
    assert target.read_bytes() == before
    assert not list(target.parent.glob(".*.tmp"))


def test_salary_migration_failure_does_not_append_partial_env(tmp_path, monkeypatch):
    target = _env(tmp_path, monkeypatch)
    source = tmp_path / "source.env"
    source.write_text("HH_AUTO_ANSWER_PROFILE_NOTE=Synthetic\nHH_AUTO_ANSWER_SALARY_RULE=Synthetic rule\n")
    migrate_profile_note.migrate_profile_note(str(source), str(target.parent))
    before = target.read_bytes()
    _fault(monkeypatch, "fsync")
    with pytest.raises(OSError, match="synthetic write failure"):
        migrate_profile_note.migrate_profile_note(str(source), str(target.parent), include_salary=True)
    assert target.read_bytes() == before


def test_note_creation_failure_leaves_no_partial_note(tmp_path, monkeypatch):
    target = _env(tmp_path, monkeypatch)
    source = tmp_path / "source.env"
    source.write_text("HH_AUTO_ANSWER_PROFILE_NOTE=Synthetic\n")
    _fault(monkeypatch, "fsync")
    with pytest.raises(OSError, match="synthetic write failure"):
        migrate_profile_note.migrate_profile_note(str(source), str(target.parent))
    assert not (target.parent / "knowledge" / "profile_note.md").exists()


def test_atomic_create_never_overwrites_existing_target(tmp_path):
    target = tmp_path / "note.md"
    target.write_text("Keep\n")
    with pytest.raises(FileExistsError):
        json_store.atomic_create_text(target, "New\n")
    assert target.read_text() == "Keep\n"
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize("operation", ["fsync", "link"])
def test_atomic_create_failure_leaves_no_target_or_temp(tmp_path, monkeypatch, operation):
    target = tmp_path / "note.md"
    _fault(monkeypatch, operation)
    with pytest.raises(OSError, match="synthetic write failure"):
        json_store.atomic_create_text(target, "Synthetic note\n")
    assert not target.exists()
    assert not list(tmp_path.glob(".*.tmp"))


def test_atomic_create_syncs_file_then_directory_and_sets_private_mode(tmp_path, monkeypatch):
    target = tmp_path / "note.md"
    original = os.fsync
    calls = []
    def sync(fd):
        calls.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        return original(fd)
    monkeypatch.setattr(json_store.os, "fsync", sync)
    json_store.atomic_create_text(target, "Synthetic note\n")
    assert calls == ["file", "directory"]
    assert stat.S_IMODE(target.stat().st_mode) == 0o600
    assert target.read_text() == "Synthetic note\n"


def test_atomic_create_does_not_follow_existing_symlink(tmp_path):
    other = tmp_path / "other.md"
    other.write_text("Other candidate\n")
    target = tmp_path / "note.md"
    target.symlink_to(other)
    with pytest.raises(FileExistsError):
        json_store.atomic_create_text(target, "Synthetic note\n")
    assert target.is_symlink()
    assert other.read_text() == "Other candidate\n"


def test_parallel_atomic_creators_have_one_winner(tmp_path, monkeypatch):
    target = tmp_path / "note.md"
    barrier = threading.Barrier(8)
    original = os.link
    def publish(*args):
        barrier.wait(timeout=5)
        return original(*args)
    monkeypatch.setattr(json_store.os, "link", publish)
    def create(index):
        try:
            json_store.atomic_create_text(target, f"Synthetic {index}\n")
        except FileExistsError:
            return None
        return index
    with ThreadPoolExecutor(max_workers=8) as pool:
        winners = [index for index in pool.map(create, range(8)) if index is not None]
    assert len(winners) == 1
    assert target.read_text() == f"Synthetic {winners[0]}\n"
    assert not list(tmp_path.glob(".*.tmp"))


@pytest.mark.parametrize("writer", ["profile", "auth"])
@pytest.mark.parametrize("failure", ["permission", "utf8"])
def test_env_read_failure_keeps_original(tmp_path, monkeypatch, writer, failure):
    target = _env(tmp_path, monkeypatch)
    if failure == "utf8":
        target.write_bytes(b"OTHER=\xff\n")
        expected = UnicodeDecodeError
    else:
        original = builtins.open
        def denied(path, *args, **kwargs):
            if os.fspath(path) == str(target):
                raise PermissionError("synthetic read denial")
            return original(path, *args, **kwargs)
        monkeypatch.setattr(builtins, "open", denied)
        expected = PermissionError
    before = target.read_bytes()
    with pytest.raises(expected):
        if writer == "auth":
            client_hh_auth._update_profile_resume_ids("synthetic", [])
        else:
            profile.update_profile_env("synthetic", {"NEW": "value"})
    assert target.read_bytes() == before


def test_env_preserves_comments_duplicates_and_normalizes_values(tmp_path, monkeypatch):
    target = _env(tmp_path, monkeypatch)
    target.write_text("# comment\nDUP=first\nDUP=last\nKEEP=value\n")
    assert profile.update_profile_env("synthetic", {"DUP": "new\n value", "NEW": 42}) == str(target)
    assert target.read_text() == "# comment\nDUP=first\nDUP=new value\nKEEP=value\n\nNEW=42\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_only_missing_keeps_empty_values_and_idempotent_bytes(tmp_path, monkeypatch):
    target = _env(tmp_path, monkeypatch)
    target.write_text("EMPTY=\nKEEP=own\n")
    profile.update_env_file(target, {"EMPTY": "global", "KEEP": "global", "NEW": "synthetic"}, only_missing=True, comment="Migration")
    before = target.read_bytes()
    inode = target.stat().st_ino
    profile.update_env_file(target, {"EMPTY": "global", "KEEP": "global", "NEW": "synthetic"}, only_missing=True, comment="Migration")
    assert target.read_bytes() == before
    assert target.stat().st_ino == inode
    assert before.count(b"# Migration") == 1
    assert b"EMPTY=\nKEEP=own\n" in before


def test_env_update_does_not_recreate_missing_file(tmp_path):
    target = tmp_path / "missing.env"
    with pytest.raises(FileNotFoundError):
        profile.update_env_file(target, {"NEW": "synthetic"})
    assert not target.exists()


def _env_process(path, index):
    for number in range(10):
        profile.update_env_file(path, {f"PROCESS_{index}_{number}": "synthetic"})


def test_separate_processes_preserve_all_env_updates(tmp_path, monkeypatch):
    target = _env(tmp_path, monkeypatch)
    context = multiprocessing.get_context("spawn")
    workers = [context.Process(target=_env_process, args=(str(target), index)) for index in range(4)]
    for worker in workers:
        worker.start()
    try:
        for worker in workers:
            worker.join(timeout=15)
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.is_alive():
                worker.terminate()
                worker.join(timeout=5)
    content = target.read_text()
    assert all(f"PROCESS_{index}_{number}=synthetic\n" in content for index in range(4) for number in range(10))
    assert stat.S_IMODE((target.parent / ".profile.env.lock").stat().st_mode) == 0o600
