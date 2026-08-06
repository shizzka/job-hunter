import json

import pytest

from state_store.json_store import JsonStore


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


def test_invalid_or_non_object_json_returns_schema_default(tmp_path):
    path = tmp_path / "state.json"
    store = JsonStore(path, default_factory=lambda: {"items": {}})

    path.write_text("{broken", encoding="utf-8")
    assert store.load() == {"items": {}}

    path.write_text("[1, 2, 3]", encoding="utf-8")
    assert store.load() == {"items": {}}


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
