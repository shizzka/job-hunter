import json
from datetime import datetime, timedelta

import pytest

import seen_reset


def setup(tmp_path):
    now = datetime.now()
    old = (now - timedelta(days=3)).isoformat()
    recent = (now - timedelta(hours=2)).isoformat()
    seen = {"1": {"action": "skipped_keyword_filter", "date": old}, "2": {"action": "skipped_low_score", "date": recent},
            "3": {"action": "applied", "date": recent}, "4": {"action": "manual_hh_confirm", "date": recent},
            "5": {"action": "dry_run_hh", "date": recent}, "6": {"action": "apply_uncertain", "date": recent}}
    (tmp_path / "seen_vacancies.json").write_text(json.dumps(seen))
    (tmp_path / "run_history.jsonl").write_text(json.dumps({"started_at": (now - timedelta(hours=3)).isoformat()}) + "\n")
    return tmp_path


def test_scopes_never_touch_applied_manual_or_uncertain(tmp_path):
    home = str(setup(tmp_path))
    assert sorted(seen_reset.plan(home, "all")) == ["1", "2", "5"]
    assert sorted(seen_reset.plan(home, "day")) == ["2", "5"]
    assert sorted(seen_reset.plan(home, "last_run")) == ["2", "5"]
    assert seen_reset.apply(home, "all") == 3
    left = json.loads((tmp_path / "seen_vacancies.json").read_text())
    assert sorted(left) == ["3", "4", "6"]


def test_unknown_scope_and_no_runs(tmp_path):
    with pytest.raises(ValueError):
        seen_reset.plan(str(tmp_path), "week")
    with pytest.raises(ValueError):
        seen_reset.plan(str(tmp_path), "last_run")


def test_last_run_ignores_a_run_still_in_progress(tmp_path):
    home = setup(tmp_path)
    now = datetime.now()
    with open(home / "run_history.jsonl", "w") as stream:
        stream.write(json.dumps({"started_at": (now - timedelta(hours=3)).isoformat(),
                                 "finished_at": (now - timedelta(hours=2, minutes=50)).isoformat()}) + "\n")
        stream.write(json.dumps({"started_at": now.isoformat(), "finished_at": None}) + "\n")
    assert sorted(seen_reset.plan(str(home), "last_run")) == ["2", "5"]
