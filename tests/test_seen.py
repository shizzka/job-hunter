import json
import pytest

import config
import seen


def test_stats_from_data_counts_already_applied_as_skipped():
    stats = seen.stats_from_data(
        {
            "123": {"action": "already_applied"},
            "124": {"action": "applied"},
            "125": {"action": "manual_hh"},
        }
    )

    assert stats["total"] == 3
    assert stats["applied"] == 1
    assert stats["manual"] == 1
    assert stats["skipped"] == 2
    assert stats["by_source"]["hh"]["skipped"] == 2


def test_mark_seen_preserves_corrupt_file_and_requires_repair(tmp_path, monkeypatch):
    path = tmp_path / "seen.json"
    path.write_text("{broken", encoding="utf-8")
    monkeypatch.setattr(config, "SEEN_VACANCIES_FILE", str(path))

    with pytest.raises(RuntimeError):
        seen.mark_seen("hh:123", {"title": "QA", "company": "Example"})
    assert path.read_text(encoding="utf-8") == "{broken"
    with pytest.raises(RuntimeError):
        seen.is_seen("hh:123")


def test_mark_seen_reloads_state_for_each_atomic_update(tmp_path, monkeypatch):
    path = tmp_path / "seen.json"
    monkeypatch.setattr(config, "SEEN_VACANCIES_FILE", str(path))

    seen.mark_seen("hh:1", {"title": "First", "company": "A"})
    current = json.loads(path.read_text(encoding="utf-8"))
    path.write_text(json.dumps({**current, "hh:2": {"action": "applied"}}), encoding="utf-8")
    seen.mark_seen("hh:3", {"title": "Third", "company": "C"})

    assert set(json.loads(path.read_text(encoding="utf-8"))) == {"hh:1", "hh:2", "hh:3"}
