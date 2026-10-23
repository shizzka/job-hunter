"""Bounded journal scans for legacy run ownership."""
import builtins
import json

import pytest

import analytics
from tests.direction_view_fakes import two_directions
from tests.test_run_summary import finished_run


@pytest.mark.parametrize("has_old_run", [False, True])
def test_legacy_candidate_ownership_uses_at_most_two_journal_passes(tmp_path, monkeypatch, has_old_run):
    two_directions(tmp_path)
    records, events = [], []
    for index in range(60):
        identity = f"legacy-{index}"
        records.append(finished_run(run_id=identity, funnel={}))
        events.extend([
            {"event": "search_started", "run_id": identity},
            {"event": "decision", "run_id": identity, "resumeId": 111 if has_old_run and index == 0 else 222,
             "vacancy_id": str(index), "decision": "applied_auto", "outcome": "applied"},
        ])
    # A receipt can arrive after newer runs: do not truncate the selected run.
    events.append({"event": "decision", "run_id": "legacy-0", "resumeId": 111 if has_old_run else 222,
                   "vacancy_id": "late", "decision": "applied_auto", "outcome": "applied"})
    path = tmp_path / "events.jsonl"
    history_path = tmp_path / "history.jsonl"
    path.write_text("\n".join(json.dumps(e) for e in events))
    history_path.write_text("\n".join(json.dumps(r) for r in records))
    original = builtins.open
    reads = []
    def opened(file, *args, **kwargs):
        if str(file) == str(path):
            reads.append(file)
        return original(file, *args, **kwargs)
    monkeypatch.setattr(builtins, "open", opened)
    summary = analytics.get_run_summary(history_file=str(history_path), events_file=str(path))
    if has_old_run:
        assert summary["run_id"] == "legacy-0"
        assert summary["counters"]["applied"] == 2
    else:
        assert summary is None
    assert len(reads) <= 2, f"Journal scanned {len(reads)} times for 60 legacy candidates"


