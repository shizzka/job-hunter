import copy

import pytest

import config
import directions
import seen


def test_dry_run_marks_stay_in_memory(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "SEEN_VACANCIES_FILE", str(tmp_path / "seen.json"))
    with seen.dry_run_overlay():
        seen.mark_seen("1", {"title": "Электрик"}, "dry_run_hh")
        assert seen.is_seen("1")
    assert not seen.is_seen("1")
    assert "1" not in seen.all_entries()
    seen.mark_seen("2", {"title": "Электрик"}, "applied")
    assert seen.is_seen("2")


def _direction(**extra):
    value = {"name": "electrical", "title": "Электрика", "filter_policy": "generic",
             "search_queries": ["электрик"], "relevant_keywords": ["электр"], "exclude_keywords": [],
             "variants": [{"hh_resume_hash": "abc", "title": "Электромонтажник",
                           "text_file": "directions/electrical/x.md", "text_sha256": "0" * 64,
                           "fetched_at": "2026-10-17T10:00:00+00:00"}]}
    value.update(extra)
    return value


def test_search_profiles_are_validated_and_normalized():
    value = directions.validate_direction(_direction(search_profiles=[
        {"area": "2", "schedule": ""}, {"area": 113, "schedule": "flyInFlyOut"}]))
    assert value["search_profiles"] == [{"area": 2, "schedule": ""}, {"area": 113, "schedule": "flyInFlyOut"}]


@pytest.mark.parametrize("profiles", [[], [{"area": "x"}], [{"area": 2, "schedule": "hybrid"}],
                                      [{"area": 2, "extra": 1}], [{"area": 0}], "2"])
def test_bad_search_profiles_are_rejected(profiles):
    with pytest.raises(directions.DirectionError):
        directions.validate_direction(_direction(search_profiles=profiles))


def test_overlay_replaces_profile_regions_only_when_set(monkeypatch):
    import profile
    base = profile.load_default_profile()
    base.hh.search_profiles = [{"area": 113, "schedule": "remote"}]
    with_regions = directions.validate_direction(_direction(search_profiles=[{"area": 2, "schedule": ""}]))
    monkeypatch.setattr(directions, "active_direction", lambda home: copy.deepcopy(with_regions))
    assert directions.overlay_profile(base).hh.search_profiles == [{"area": 2, "schedule": ""}]
    without = directions.validate_direction(_direction())
    monkeypatch.setattr(directions, "active_direction", lambda home: copy.deepcopy(without))
    assert directions.overlay_profile(base).hh.search_profiles == [{"area": 113, "schedule": "remote"}]
