"""Atomic, profile-local analytics checkpoints and journal recovery."""
import builtins
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

import analytics
import config
from state_store.json_store import JsonStore


@pytest.fixture
def analytics_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))
    monkeypatch.setattr(config, "ANALYTICS_STATE_FILE", str(tmp_path / "state.json"))
    monkeypatch.setattr(config, "ANALYTICS_EVENTS_FILE", str(tmp_path / "events.jsonl"))
    monkeypatch.setattr(config, "ANALYTICS_ENABLED", True)
    return tmp_path / "state.json", tmp_path / "events.jsonl"


def _events(path):
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def _historical(vacancy_id="1"):
    return {vacancy_id: {"action": "applied", "title": "Manual QA", "date": "2026-10-02T12:00:00"}}


def test_switching_profiles_keeps_markers_and_events_separate(analytics_paths, monkeypatch):
    root = analytics_paths[0].parent
    for name in ("a", "b", "a", "b"):
        directory = root / name
        monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(directory))
        monkeypatch.setattr(config, "ANALYTICS_STATE_FILE", str(directory / "state.json"))
        monkeypatch.setattr(config, "ANALYTICS_EVENTS_FILE", str(directory / "events.jsonl"))
        analytics.record_invitations([{"id": "same", "company": name}])
        analytics.record_negotiation_statuses([{"id": "same", "status": name}])
        analytics.backfill_seen_decisions(_historical())
        state = analytics._load_state()
        assert state["negotiation_status_by_vacancy"] == {"same": name}
    for name in ("a", "b"):
        rows = _events(root / name / "events.jsonl")
        assert len([row for row in rows if row["event"] == "invitation"]) == 1
        assert len([row for row in rows if row["event"] == "decision"]) == 1
        assert {row["company"] for row in rows if row["event"] == "invitation"} == {name}


def test_reader_and_mutator_see_external_state_updates(analytics_paths):
    state_path, _events_path = analytics_paths
    analytics.record_invitations([{"id": "local"}])
    store = JsonStore(state_path)
    store.update(lambda state: {**state, "invitation_keys": state["invitation_keys"] + ["hh:external"]})
    assert "hh:external" in analytics._load_state()["invitation_keys"]
    analytics.record_invitations([{"id": "external"}, {"id": "new"}])
    assert set(analytics._load_state()["invitation_keys"]) == {"hh:local", "hh:external", "hh:new"}


@pytest.mark.parametrize("broken", ['{"truncated":', "[]", '{"invitation_keys":[{}]}', '{"negotiation_status_by_vacancy":[]}'])
def test_corrupt_state_recovers_dedup_and_statuses_without_old_duplicates(analytics_paths, broken):
    state_path, events_path = analytics_paths
    analytics.record_invitations([{"id": "1"}])
    assert analytics.backfill_seen_decisions(_historical())["added"] == 1
    analytics.record_negotiation_statuses([{"id": "1", "status": "Приглашение"}])
    last_poll = analytics._load_state()["last_poll_by_vacancy"]["1"]
    state_path.write_text(broken)
    analytics.record_invitations([{"id": "1"}])
    assert analytics.backfill_seen_decisions(_historical())["added"] == 0
    analytics.record_negotiation_statuses([{"id": "1", "status": "Приглашение"}])
    rows = _events(events_path)
    for kind in ("invitation", "decision", "negotiation_status"):
        assert len([row for row in rows if row["event"] == kind]) == 1
    polls = [row for row in rows if row["event"] == "negotiation_observation"]
    assert polls[-1]["previous_poll_at"] == last_poll
    backups = list(state_path.parent.glob("state.json.corrupt-*"))
    assert len(backups) == 1 and backups[0].read_text() == broken
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600
    assert stat.S_IMODE(events_path.stat().st_mode) == 0o600


def test_missing_state_is_rebuilt_from_existing_profile_journal(analytics_paths):
    state_path, events_path = analytics_paths
    analytics.record_invitations([{"id": "1"}])
    state_path.unlink()  # Only this test's disposable checkpoint.
    analytics.record_invitations([{"id": "1"}])
    assert len(_events(events_path)) == 1
    assert analytics._load_state()["invitation_keys"] == ["hh:1"]


@pytest.mark.parametrize("kind", ["invitation", "historical"])
def test_failed_checkpoint_save_replays_tail_without_duplicate_event(analytics_paths, monkeypatch, kind):
    import state_store.json_store as storage

    state_path, events_path = analytics_paths
    analytics._update_state(lambda state: None)
    original = state_path.read_bytes()

    def record():
        if kind == "invitation":
            analytics.record_invitations([{"id": "1"}])
        else:
            analytics.backfill_seen_decisions(_historical())

    def fail_replace(*args):
        raise OSError("injected checkpoint failure")

    with monkeypatch.context() as patch:
        patch.setattr(storage.os, "replace", fail_replace)
        record()
    assert state_path.read_bytes() == original
    assert len(_events(events_path)) == 1
    record()
    assert len(_events(events_path)) == 1
    state = json.loads(state_path.read_text())
    assert state["_journal_checkpoint"]["offset"] == events_path.stat().st_size
    assert not list(state_path.parent.glob(".state.json.*.tmp"))


def test_failed_event_append_does_not_mark_invitation_or_backfill_done(analytics_paths, monkeypatch):
    _state_path, events_path = analytics_paths
    original_open = os.open

    def unavailable(path, *args, **kwargs):
        if str(path) == str(events_path):
            raise PermissionError("injected journal write failure")
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(analytics.os, "open", unavailable)
        analytics.record_invitations([{"id": "1"}])
        assert analytics.backfill_seen_decisions(_historical())["added"] == 0
    state = analytics._load_state()
    assert not state["invitation_keys"] and not state["historical_decision_keys"]
    analytics.record_invitations([{"id": "1"}])
    assert analytics.backfill_seen_decisions(_historical())["added"] == 1
    assert len(_events(events_path)) == 2


def test_unreadable_recovery_journal_is_not_treated_as_empty(analytics_paths, monkeypatch):
    state_path, events_path = analytics_paths
    analytics.record_invitations([{"id": "1"}])
    original_journal = events_path.read_bytes()
    state_path.write_text("{broken")
    original_open = builtins.open

    def unreadable(path, *args, **kwargs):
        if str(path) == str(events_path):
            raise PermissionError("injected journal read failure")
        return original_open(path, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(builtins, "open", unreadable)
        analytics.record_invitations([{"id": "1"}])
    assert events_path.read_bytes() == original_journal
    analytics.record_invitations([{"id": "1"}])
    assert events_path.read_bytes() == original_journal
    assert analytics._load_state()["invitation_keys"] == ["hh:1"]


def test_corruption_without_journal_requires_manual_recovery(analytics_paths):
    state_path, events_path = analytics_paths
    state_path.write_text("{broken")
    analytics.record_invitations([{"id": "old"}])
    assert not events_path.exists()
    assert analytics._load_state()["_recovery_required"]
    # An unrelated new LLM event is not proof that the old dedupe history exists.
    analytics._append_event({"event": "llm_call"})
    analytics.record_invitations([{"id": "old"}])
    assert [row["event"] for row in _events(events_path)] == ["llm_call"]
    assert analytics.backfill_seen_decisions(_historical())["added"] == 0


def test_repeated_checks_seek_to_checkpoint_instead_of_replaying_full_journal(analytics_paths, monkeypatch):
    _state_path, events_path = analytics_paths
    analytics.record_invitations([{"id": "1"}])
    expected_end = events_path.stat().st_size
    original_open = builtins.open
    offsets = []
    read_chars = []

    class TrackedReader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def __getattr__(self, key):
            return getattr(self.stream, key)

        def seek(self, offset):
            offsets.append(offset)
            return self.stream.seek(offset)

        def readline(self):
            value = self.stream.readline()
            read_chars.append(len(value))
            return value

    def tracked_open(path, *args, **kwargs):
        stream = original_open(path, *args, **kwargs)
        return TrackedReader(stream) if str(path) == str(events_path) else stream

    monkeypatch.setattr(builtins, "open", tracked_open)
    for _ in range(3):
        analytics.record_invitations([{"id": "1"}])
    assert offsets and all(offset == expected_end for offset in offsets)
    assert sum(read_chars) == 0


def test_journal_rotation_invalidates_checkpoint_without_forgetting_existing_keys(analytics_paths):
    _state_path, events_path = analytics_paths
    analytics.record_invitations([{"id": "old"}])
    replacement = events_path.with_name("replacement.jsonl")
    replacement.write_text(json.dumps({"event": "invitation", "vacancy_id": "new", "source": "hh"}) + "\n")
    replacement.replace(events_path)
    analytics.record_invitations([{"id": "new"}])
    assert len(_events(events_path)) == 1
    assert set(analytics._load_state()["invitation_keys"]) == {"hh:old", "hh:new"}


@pytest.mark.parametrize("tail", ['{"unfinished":', json.dumps({"event": "invitation", "source": "hh", "vacancy_id": "old"})])
def test_append_after_non_newline_legacy_tail_keeps_new_record_parseable(analytics_paths, tail):
    _state_path, events_path = analytics_paths
    events_path.write_text(tail)
    analytics.record_invitations([{"id": "new"}])
    rows = analytics._iter_events()
    assert any(row.get("vacancy_id") == "new" for row in rows)
    assert events_path.read_text().startswith(tail + "\n")


def test_concurrent_processes_preserve_keys_and_emit_shared_events_once(analytics_paths):
    state_path, events_path = analytics_paths
    script = """
import sys
import config
import analytics
config.ANALYTICS_ENABLED = True
config.ANALYTICS_STATE_FILE, config.ANALYTICS_EVENTS_FILE, config.JOB_HUNTER_HOME = sys.argv[1:4]
prefix = sys.argv[4]
ids = ['shared'] + [prefix + ':' + str(i) for i in range(3)]
analytics.record_invitations([{'id': item} for item in ids])
analytics.backfill_seen_decisions({item: {'action': 'applied'} for item in ids})
analytics.record_negotiation_statuses([{'id': item, 'status': 'Приглашение'} for item in ids])
"""
    processes = [
        subprocess.Popen(
            [sys.executable, "-B", "-c", script, str(state_path), str(events_path), str(state_path.parent), str(worker)],
            cwd=Path(analytics.__file__).parent, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
        ) for worker in range(4)
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
    rows = _events(events_path)
    for kind in ("invitation", "decision", "negotiation_status"):
        assert len([row for row in rows if row["event"] == kind]) == 13
    assert len([row for row in rows if row["event"] == "negotiation_observation"]) == 16
    state = analytics._load_state()
    assert len(state["invitation_keys"]) == 13
    assert len(state["historical_decision_keys"]) == 13
    assert len(state["negotiation_status_by_vacancy"]) == 13
    assert len(state["last_poll_by_vacancy"]) == 13
