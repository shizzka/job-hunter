"""Profile-local, atomic and fail-safe HH retry history."""
import json
import stat
import subprocess
import sys
from datetime import datetime, timedelta
from pathlib import Path

import pytest

import config
import hh_resume_pipeline as pipeline
from state_store.json_store import JsonStore


@pytest.fixture
def isolated_pipeline(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))
    monkeypatch.setattr(config, "HH_RESUME_PIPELINE_FILE", str(tmp_path / "history.json"))
    monkeypatch.setattr(config, "HH_RESUME_PIPELINE_ENABLED", True)
    monkeypatch.setattr(config, "HH_PRIMARY_RESUME_ID", "111")
    monkeypatch.setattr(config, "HH_PRIMARY_RESUME_TITLE", "Manual QA")
    monkeypatch.setattr(config, "HH_SECONDARY_RESUME_ID", "222")
    monkeypatch.setattr(config, "HH_SECONDARY_RESUME_TITLE", "QA second variant")
    monkeypatch.setattr(config, "HH_TERTIARY_RESUME_ID", "")
    monkeypatch.setattr(config, "HH_TERTIARY_RESUME_TITLE", "")
    monkeypatch.setattr(config, "HH_RESUME_RETRY_ON_SILENCE", True)
    monkeypatch.setattr(config, "HH_RESUME_CLUSTER_STRATEGY_ENABLED", False)
    monkeypatch.setattr(config, "HH_RESUME_RETRY_BLOCKED_COMPANIES", [])
    now = datetime(2026, 10, 2, 12)
    monkeypatch.setattr(pipeline, "_now", lambda: now)
    return tmp_path / "history.json"


def _vacancy(vacancy_id="same"):
    return {"id": vacancy_id, "title": "Manual QA Engineer", "company": "Acme", "url": "https://hh.ru/vacancy/123"}


def test_switching_profiles_isolates_attempts_resolved_ids_and_blocks(isolated_pipeline, monkeypatch):
    directory = isolated_pipeline.parent
    paths = {name: directory / name / "history.json" for name in ("admin", "client")}
    monkeypatch.setattr(config, "HH_RESUME_PIPELINE_FILE", str(paths["admin"]))
    pipeline.remember_resolved_variants([{"name": "normal", "id": "admin-resume", "title": "Manual QA"}])
    pipeline.record_successful_apply(_vacancy(), {"name": "normal", "id": "admin-resume"})
    pipeline.block_company_retry("Admin-only company")

    monkeypatch.setattr(config, "HH_RESUME_PIPELINE_FILE", str(paths["client"]))
    assert pipeline.all_entries() == {}
    assert pipeline.get_attempt_count("same") == 0
    assert pipeline.get_next_variant("same")["name"] == "normal"
    assert pipeline.list_blocked_companies() == []
    assert "admin-resume" not in str(pipeline.get_resolved_variants())
    pipeline.remember_resolved_variants([{"name": "normal", "id": "client-resume", "title": "Manual QA"}])
    pipeline.record_successful_apply(_vacancy("client-only"), {"name": "normal", "id": "client-resume"})
    pipeline.block_company_retry("Client-only company")

    for name, own_id, other_id in (("admin", "same", "client-only"), ("client", "client-only", "same"), ("admin", "same", "client-only")):
        monkeypatch.setattr(config, "HH_RESUME_PIPELINE_FILE", str(paths[name]))
        assert pipeline.get_attempt_count(own_id) == 1
        assert pipeline.get_attempt_count(other_id) == 0
        assert pipeline.get_resolved_variants()[0]["id"] == "111"
        assert pipeline.list_blocked_companies()[0]["company"] == f"{name.title()}-only company"


def test_mutations_do_not_overwrite_other_profiles(isolated_pipeline, monkeypatch):
    a = isolated_pipeline
    b = a.with_name("other-history.json")
    pipeline.record_successful_apply(_vacancy("a-only"), {"name": "normal"})
    before = a.read_bytes()
    monkeypatch.setattr(config, "HH_RESUME_PIPELINE_FILE", str(b))
    pipeline.record_successful_apply(_vacancy("b-only"), {"name": "normal"})
    pipeline.mark_terminal("a-only", "must-not-leak")
    assert a.read_bytes() == before
    assert "a-only" not in json.loads(b.read_text())
    assert "b-only" not in json.loads(a.read_text())


def test_fresh_reads_see_external_updates_without_cache_reset(isolated_pipeline):
    assert pipeline.all_entries() == {}
    JsonStore(isolated_pipeline).save({"external": {"id": "external", "attempts": [{"variant": "normal"}]}})
    assert pipeline.get_attempt_count("external") == 1
    pipeline.record_successful_apply(_vacancy("local"), {"name": "normal"})
    assert set(pipeline.all_entries()) == {"external", "local"}


@pytest.mark.parametrize("broken", ['{"truncated":', "[]", "null"])
def test_corrupt_history_is_preserved_and_retries_remain_suspended(isolated_pipeline, monkeypatch, broken):
    isolated_pipeline.write_text(broken)
    assert pipeline.get_retry_candidates() == []
    backups = list(isolated_pipeline.parent.glob("history.json.corrupt-*"))
    assert len(backups) == 1
    assert backups[0].read_text() == broken
    recovered = json.loads(isolated_pipeline.read_text())
    assert recovered["_recovery_required"]["reason"] == "state_corruption"
    assert stat.S_IMODE(isolated_pipeline.stat().st_mode) == 0o600

    # New explicitly confirmed applications can still be recorded, but losing
    # old attempts must not silently re-enable automatic retries later.
    pipeline.record_successful_apply(_vacancy(), {"name": "normal"})
    pipeline.sync_negotiation_statuses([{**_vacancy(), "status": "Отказ"}])
    monkeypatch.setattr(pipeline, "_now", lambda: datetime(2026, 10, 10, 12))
    assert pipeline.get_retry_candidates() == []
    assert json.loads(isolated_pipeline.read_text())["_recovery_required"] == recovered["_recovery_required"]
    assert len(list(isolated_pipeline.parent.glob("history.json.corrupt-*"))) == 1


def test_failed_atomic_replace_keeps_previous_history(isolated_pipeline, monkeypatch):
    import state_store.json_store as storage

    pipeline.record_successful_apply(_vacancy("original"), {"name": "normal"})
    original = isolated_pipeline.read_bytes()

    def fail_replace(*args):
        raise OSError("injected replace failure")

    with monkeypatch.context() as patch:
        patch.setattr(storage.os, "replace", fail_replace)
        with pytest.raises(OSError, match="injected"):
            pipeline.record_successful_apply(_vacancy("failed"), {"name": "normal"})
    assert isolated_pipeline.read_bytes() == original
    assert set(pipeline.all_entries()) == {"original"}
    assert not list(isolated_pipeline.parent.glob(".history.json.*.tmp"))


def test_concurrent_processes_preserve_all_attempts_and_company_blocks(isolated_pipeline):
    script = """
import sys
import config
import hh_resume_pipeline as pipeline
config.HH_RESUME_PIPELINE_FILE = sys.argv[1]
prefix = sys.argv[2]
for i in range(10):
    pipeline.record_successful_apply({'id': prefix + ':' + str(i), 'title': 'Manual QA'}, {'name': 'normal', 'id': prefix})
pipeline.block_company_retry('Company ' + prefix)
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-B", "-c", script, str(isolated_pipeline), str(worker)],
            cwd=Path(pipeline.__file__).parent, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        )
        for worker in range(4)
    ]
    try:
        for process in processes:
            _stdout, stderr = process.communicate(timeout=15)
            assert process.returncode == 0, stderr
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)
    entries = pipeline.all_entries()
    assert len(entries) == 40
    assert all(len(entry["attempts"]) == 1 for entry in entries.values())
    assert len(pipeline.list_blocked_companies()) == 4
    assert stat.S_IMODE(isolated_pipeline.stat().st_mode) == 0o600


def test_retry_candidate_processing_uses_one_transaction_snapshot(isolated_pipeline, monkeypatch):
    pipeline.record_successful_apply(_vacancy(), {"name": "normal"})
    pipeline.sync_negotiation_statuses([{**_vacancy(), "status": "Отказ"}])
    monkeypatch.setattr(pipeline, "_now", lambda: datetime(2026, 10, 2, 12) + timedelta(days=2))

    def unexpected_nested_load():
        pytest.fail("Nested state load would try to acquire the same lock again")

    monkeypatch.setattr(pipeline, "_load", unexpected_nested_load)
    candidates = pipeline.get_retry_candidates()
    assert len(candidates) == 1
    assert candidates[0]["_hh_resume_variant"] == "fun"
