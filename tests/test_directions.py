"""Direction storage/selection isolation; every writable path is pytest-local."""
import copy
import dataclasses
import hashlib
import json
from collections import Counter
from pathlib import Path

import pytest

import config
import profile
from directions import (DirectionError, active_direction, list_directions, load_direction,
                        overlay_profile, save_direction, select_variant, set_active_direction)


def draft(name="qa", count=2):
    return {"name": name, "title": name.upper(), "filter_policy": "generic",
            "search_queries": [name + " engineer"], "relevant_keywords": [name],
            "exclude_keywords": ["senior"], "selection_strategy": "single",
            "variants": [{"hh_resume_hash": f"hash-{index}", "hh_resume_numeric_id": 100 + index,
                          "title": f"Resume {index}"} for index in range(count)]}


def saved(home, name="qa", count=2):
    return save_direction(home, draft(name, count), texts={f"hash-{i}": f"# Resume {i}" for i in range(count)})


def test_no_directions_read_is_pure_and_overlay_preserves_dataclass(tmp_path):
    home = tmp_path / "nonexistent"
    person = profile.Profile(home_dir=str(home))
    original = dataclasses.asdict(person)
    assert list_directions(home) == [] and active_direction(home) is None
    assert overlay_profile(person) is person
    assert dataclasses.asdict(person) == original
    assert not home.exists()


def test_save_roundtrip_content_addressed_and_private(tmp_path):
    value = saved(tmp_path)
    assert list_directions(tmp_path) == [value]
    assert load_direction(tmp_path, "qa") == value
    variant = value["variants"][0]
    path = Path(variant["text_file"])
    assert path.name == variant["text_sha256"] + ".md"
    assert path.parent == tmp_path / "directions" / "qa"
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads((tmp_path / "directions" / "qa.json").read_text()) == value


def test_refetched_draft_does_not_overwrite_active_text(tmp_path):
    value = saved(tmp_path)
    set_active_direction(tmp_path, "qa")
    previous = Path(value["variants"][0]["text_file"])
    old_json = (tmp_path / "directions" / "qa.json").read_bytes()
    staged = copy.deepcopy(value)
    raw = "# Updated resume, only a staged draft"
    assert previous.read_text() == "# Resume 0"
    assert (tmp_path / "directions" / "qa.json").read_bytes() == old_json
    updated = save_direction(tmp_path, staged, texts={"hash-0": raw})
    assert previous.read_text() == "# Resume 0"
    assert Path(updated["variants"][0]["text_file"]).read_text() == raw
    assert previous != Path(updated["variants"][0]["text_file"])


@pytest.mark.parametrize("name", ["../qa", "/tmp/qa", "qa\n", "", ".", "..", "a/b", "a\\b"])
def test_invalid_name_cannot_write(tmp_path, name):
    value = draft(name)
    previous = set(tmp_path.iterdir())
    with pytest.raises(DirectionError):
        save_direction(tmp_path, value, texts={"hash-0": "text"})
    assert set(tmp_path.iterdir()) == previous


def test_text_traversal_and_symlink_escape_are_rejected(tmp_path):
    value = saved(tmp_path)
    value["variants"][0]["text_file"] = str(tmp_path.parent / "outside.md")
    with pytest.raises(DirectionError, match="вне"):
        save_direction(tmp_path, value)
    other = tmp_path / "other-profile"
    other.mkdir()
    home = tmp_path / "person"
    home.mkdir()
    (home / "directions").symlink_to(other, target_is_directory=True)
    with pytest.raises(DirectionError, match="вне"):
        save_direction(home, draft(), texts={"hash-0": "text", "hash-1": "second"})


def test_selection_checks_integrity_and_paused_variants(tmp_path):
    value = saved(tmp_path)
    value["variants"][0]["status"] = "paused"
    assert select_variant(value, "7")["hh_resume_hash"] == "hash-1"
    Path(value["variants"][1]["text_file"]).write_text("tampered")
    with pytest.raises(DirectionError, match="сумма"):
        select_variant(value, "7")
    value["variants"][1]["status"] = "paused"
    with pytest.raises(DirectionError, match="нет активных"):
        select_variant(value, "7")


def test_ab_uses_sha256_stable_balanced_and_only_active(tmp_path):
    value = saved(tmp_path, count=3)
    value["selection_strategy"] = "ab"
    value["variants"][1]["status"] = "paused"
    selected = [select_variant(value, str(i))["hh_resume_hash"] for i in range(1000)]
    for i, actual in enumerate(selected):
        expected = int(hashlib.sha256(str(i).encode()).hexdigest(), 16) % 2
        assert actual == ("hash-0", "hash-2")[expected]
    assert all(430 < n < 570 for n in Counter(selected).values())
    assert select_variant(value, 173) == select_variant(copy.deepcopy(value), "173")


def test_invalid_save_keeps_previous_json(tmp_path):
    value = saved(tmp_path)
    path = tmp_path / "directions" / "qa.json"
    original = path.read_bytes()
    value["variants"][0]["text_sha256"] = "bad"
    with pytest.raises(DirectionError):
        save_direction(tmp_path, value)
    assert path.read_bytes() == original


def test_overlay_is_profile_local_and_switch_changes_candidate_fields(tmp_path):
    first, second = tmp_path / "first", tmp_path / "second"
    saved(first, "qa")
    saved(first, "analyst")
    set_active_direction(first, "qa")
    person = profile.Profile(home_dir=str(first))
    person.hh.primary_resume_id = "legacy"
    person.hh.search_profiles = [{"area": 113}, {"area": 1, "schedule": "remote"}]
    overlaid = overlay_profile(person)
    assert person.hh.primary_resume_id == "legacy"
    assert overlaid.hh.primary_resume_id == "hash-0"
    assert overlaid.hh.search_queries == ["qa engineer"]
    assert overlaid.hh.search_profiles == [{"area": 113, "schedule": ""}, {"area": 1, "schedule": ""}]
    assert person.hh.search_profiles == [{"area": 113}, {"area": 1, "schedule": "remote"}]
    assert overlaid.filter_policy == "generic"
    assert overlaid.hh.secondary_resume_id == ""
    untouched = profile.Profile(home_dir=str(second))
    assert overlay_profile(untouched) is untouched
    set_active_direction(first, "analyst")
    assert overlay_profile(person).hh.search_queries == ["analyst engineer"]
    set_active_direction(first, None)
    assert overlay_profile(person) is person


def test_named_profile_never_inherits_default_direction(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_HUNTER_HOME", str(tmp_path))
    monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))
    monkeypatch.setattr(config, "ACTIVE_DIRECTION", None, raising=False)
    monkeypatch.setattr(config, "_DIRECTION_BASE_PROFILE", None, raising=False)
    home = tmp_path / "profiles" / "other"
    home.mkdir(parents=True)
    env = home / "profile.env"
    env.write_text("HH_SEARCH_QUERIES=legacy other\nHH_PRIMARY_RESUME_ID=other-hash\n")
    original = env.read_bytes()
    saved(tmp_path, "default-only")
    set_active_direction(tmp_path, "default-only")
    default = profile.load_default_profile()
    assert default.direction_name == "default-only"
    other = profile.load_profile("other")
    assert not hasattr(other, "direction")
    assert other.home_dir == str(home)
    assert other.hh.primary_resume_id == "other-hash"
    assert other.hh.search_queries == ["legacy other"]
    assert other.resume_file == str(home / "resume.md")
    assert env.read_bytes() == original


def test_active_config_overlay_does_not_leak_to_named_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("JOB_HUNTER_HOME", str(tmp_path))
    monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))
    monkeypatch.setattr(config, "ACTIVE_DIRECTION", None, raising=False)
    monkeypatch.setattr(config, "_DIRECTION_BASE_PROFILE", None, raising=False)
    home = tmp_path / "profiles" / "other"
    home.mkdir(parents=True)
    (home / "profile.env").write_text("# no direction overrides\n")
    legacy_queries = list(config.SEARCH_QUERIES)
    legacy_mode = config.HH_APPLICATION_MODE
    saved(tmp_path, "default-only")
    set_active_direction(tmp_path, "default-only")
    default = profile.load_default_profile()
    monkeypatch.setattr(config, "ACTIVE_DIRECTION", default.direction)
    monkeypatch.setattr(config, "_DIRECTION_BASE_PROFILE", default._direction_base)
    monkeypatch.setattr(config, "SEARCH_QUERIES", default.hh.search_queries)
    monkeypatch.setattr(config, "RESUME_FILE", default.resume_file)
    other = profile.load_profile("other")
    assert other.hh.search_queries == legacy_queries
    assert other.hh.application_mode == legacy_mode
    assert not hasattr(other, "direction")


def test_state_and_lock_symlinks_cannot_overwrite_other_profile_files(tmp_path):
    saved(tmp_path)
    protected = tmp_path / "profile.env"
    protected.write_text("HH_PRIMARY_RESUME_ID=legacy\n")
    state = tmp_path / "directions_state.json"
    state.symlink_to(protected)
    with pytest.raises(DirectionError):
        set_active_direction(tmp_path, "qa")
    assert protected.read_text() == "HH_PRIMARY_RESUME_ID=legacy\n"
    state.unlink()
    lock = tmp_path / ".directions_state.json.lock"
    lock.symlink_to(protected)
    with pytest.raises(DirectionError):
        set_active_direction(tmp_path, "qa")
    assert protected.read_text() == "HH_PRIMARY_RESUME_ID=legacy\n"


def test_corrupt_active_state_fails_closed_without_repair(tmp_path):
    saved(tmp_path)
    state = tmp_path / "directions_state.json"
    state.write_text('{"active_direction": "missing"}')
    original = state.read_bytes()
    with pytest.raises(FileNotFoundError):
        active_direction(tmp_path)
    assert state.read_bytes() == original
