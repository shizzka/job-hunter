import json

import google_form_filler as gforms
from runtime_context import RuntimePaths
from state_store.google_forms import (
    GoogleFormStateRepository,
    new_preview_token,
)


def test_google_form_repository_repairs_items_schema(tmp_path):
    path = tmp_path / "google_form_previews.json"
    path.write_text('{"items": ["broken"], "version": 1}', encoding="utf-8")

    state = GoogleFormStateRepository(tmp_path).load()

    assert state == {"items": {}, "version": 1}


def test_google_form_repository_trims_expired_previews(tmp_path):
    now = 1_000_000
    max_age = 100
    repository = GoogleFormStateRepository(
        tmp_path,
        clock=lambda: now,
        max_preview_age_seconds=max_age,
    )
    repository.save(
        {
            "items": {
                "expired": {"created_at": now - max_age - 1},
                "boundary": {"created_at": now - max_age},
                "fresh": {"created_at": now},
            }
        }
    )

    repository.remember(
        "new",
        {"created_at": now, "status": "preview"},
        trim_expired=True,
    )

    items = repository.load()["items"]
    assert set(items) == {"boundary", "fresh", "new"}


def test_google_form_repository_can_preserve_history_without_trim(tmp_path):
    repository = GoogleFormStateRepository(
        tmp_path,
        clock=lambda: 1_000_000,
        max_preview_age_seconds=100,
    )
    repository.save({"items": {"old": {"created_at": 1}}})

    repository.remember(
        "login-failed",
        {"created_at": 1_000_000, "status": "preview_failed_login_required"},
        trim_expired=False,
    )

    assert set(repository.load()["items"]) == {"old", "login-failed"}


def test_google_form_token_is_deterministic_for_explicit_entropy():
    token = new_preview_token(
        "https://forms.gle/example",
        "42",
        "7",
        now=123.5,
        pid=99,
    )

    assert token == new_preview_token(
        "https://forms.gle/example",
        "42",
        "7",
        now=123.5,
        pid=99,
    )
    assert len(token) == 12
    assert token.isalnum()


def test_google_form_compatibility_wrappers_use_active_home(tmp_path, monkeypatch):
    monkeypatch.setattr(gforms.config, "JOB_HUNTER_HOME", str(tmp_path))
    state = {"items": {"abc": {"status": "preview"}}}

    gforms._save_state(state)

    assert gforms._state_path() == str(tmp_path / "google_form_previews.json")
    assert gforms._load_state() == state
    assert json.loads(
        (tmp_path / "google_form_previews.json").read_text(encoding="utf-8")
    ) == state


def test_google_form_state_wrappers_honor_explicit_runtime_paths(tmp_path, monkeypatch):
    monkeypatch.setattr(gforms.config, "JOB_HUNTER_HOME", str(tmp_path))
    runtime_paths = RuntimePaths.from_config(gforms.config)
    monkeypatch.setattr(gforms.config, "JOB_HUNTER_HOME", str(tmp_path / "other"))
    state = {"items": {"abc": {"status": "preview"}}}

    gforms._save_state(state, runtime_paths)

    assert gforms._state_path(runtime_paths) == str(tmp_path / "google_form_previews.json")
    assert gforms._load_state(runtime_paths) == state
