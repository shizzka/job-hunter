import json
import stat

import pytest

from state_store.json_store import JsonStore, atomic_write_json


def test_missing_store_returns_fresh_default(tmp_path):
    store = JsonStore(
        tmp_path / "nested" / "state.json",
        default_factory=lambda: {"items": {}},
    )

    first = store.load()
    first["items"]["changed"] = True

    assert store.load() == {"items": {}}


def test_save_is_atomic_and_preserves_unicode(tmp_path):
    path = tmp_path / "nested" / "state.json"
    store = JsonStore(path, default_factory=dict)

    store.save({"message": "Привет", "items": {"a": 1}})

    assert json.loads(path.read_text(encoding="utf-8")) == {
        "message": "Привет",
        "items": {"a": 1},
    }
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []
    assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_atomic_write_json_supports_secret_lists(tmp_path):
    path = tmp_path / "nested" / "cookies.json"

    atomic_write_json(path, [{"name": "session", "value": "секрет"}])

    assert json.loads(path.read_text(encoding="utf-8")) == [
        {"name": "session", "value": "секрет"},
    ]
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []


def test_invalid_or_non_object_json_returns_schema_default(tmp_path):
    path = tmp_path / "state.json"
    store = JsonStore(path, default_factory=lambda: {"items": {}})

    path.write_text("{broken", encoding="utf-8")
    assert store.load() == {"items": {}}
    assert [item.read_text(encoding="utf-8") for item in tmp_path.glob("state.json.corrupt-*")] == ["{broken"]

    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert store.load() == {"items": {}}
    assert len(list(tmp_path.glob("state.json.corrupt-*"))) == 2


def test_failed_serialization_keeps_previous_state(tmp_path):
    path = tmp_path / "state.json"
    store = JsonStore(path)
    store.save({"version": 1})

    with pytest.raises(TypeError):
        store.save({"invalid": {1, 2, 3}})

    assert store.load() == {"version": 1}
    assert list(path.parent.glob(f".{path.name}.*.tmp")) == []


def test_save_rejects_non_dictionary_state(tmp_path):
    store = JsonStore(tmp_path / "state.json")

    with pytest.raises(TypeError, match="dictionary"):
        store.save(["not", "a", "dict"])


def test_update_reloads_state_inside_one_lock(tmp_path):
    path = tmp_path / "state.json"
    first = JsonStore(path)
    second = JsonStore(path)
    first.save({"first": 1})
    stale = first.load()
    second.update(lambda state: {**state, "second": 2})

    first.update(lambda state: {**state, "third": 3})

    assert stale == {"first": 1}
    assert first.load() == {"first": 1, "second": 2, "third": 3}
