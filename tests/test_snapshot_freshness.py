import json
from datetime import datetime, timedelta

from hh import negotiation_snapshot as ns


def write_summary(path, hours_ago, *, filter_name="active", complete=True):
    when = (datetime.now().astimezone() - timedelta(hours=hours_ago)).isoformat()
    with open(path, "a", encoding="utf-8") as stream:
        stream.write(json.dumps({"kind": "snapshot_summary", "filter": filter_name, "complete": complete,
                                 "fetched_at": when}) + "\n")


def test_ensure_fresh_collects_only_when_missing_or_stale(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(ns, "collect", lambda **kw: calls.append(kw) or {"complete": True})
    assert ns.ensure_fresh(str(tmp_path), "c.json", max_age_hours=6) == {"complete": True}  # missing
    path = tmp_path / "negotiation_snapshots.jsonl"
    write_summary(path, 1)
    assert ns.ensure_fresh(str(tmp_path), "c.json", max_age_hours=6) is None  # fresh
    write_summary(path, 0.5, complete=False)  # an incomplete pass does not count
    write_summary(path, 0.2, filter_name="archived")  # another filter does not count
    assert ns.latest_complete_age_hours(str(tmp_path)) < 2
    path.write_text("")
    write_summary(path, 10)
    ns.ensure_fresh(str(tmp_path), "c.json", max_age_hours=6)
    assert len(calls) == 2 and calls[-1]["archived"] is False
