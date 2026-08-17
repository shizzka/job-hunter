import json

import hh_chat_responder as chat_responder
from runtime_context import RuntimePaths
from state_store.chat_responder import (
    ChatResponderStateRepository,
    get_google_form_previews,
    google_form_seen_key,
    remember_google_form_preview,
)


def test_chat_responder_repository_roundtrip(tmp_path):
    repository = ChatResponderStateRepository(tmp_path)
    state = {"42": {"last_replied_msg_id": "7", "answer": "Привет"}}

    repository.save(state)

    assert repository.load() == state
    assert json.loads(
        (tmp_path / "chat_responder_state.json").read_text(encoding="utf-8")
    ) == state


def test_google_form_seen_key_is_stable_and_message_specific():
    first = google_form_seen_key("https://forms.gle/example", "10")

    assert first == google_form_seen_key("https://forms.gle/example", "10")
    assert first != google_form_seen_key("https://forms.gle/example", "11")
    assert len(first) == 16


def test_remember_google_form_preview_keeps_newest_items():
    chat_state = {}

    for index in range(35):
        remember_google_form_preview(
            chat_state,
            f"key-{index}",
            {
                "ok": True,
                "token": f"token-{index}",
                "form_url": f"https://forms.gle/{index}",
                "message_id": str(index),
            },
            now=index,
            max_items=30,
        )

    previews = get_google_form_previews(chat_state)
    assert len(previews) == 30
    assert set(previews) == {f"key-{index}" for index in range(5, 35)}
    assert previews["key-34"]["status"] == "preview"


def test_remember_google_form_preview_repairs_invalid_schema():
    chat_state = {"google_form_previews": ["broken"]}

    remember_google_form_preview(
        chat_state,
        "key",
        {
            "ok": False,
            "message": "failed",
            "original_form_url": "https://forms.gle/example",
        },
        now=123,
    )

    assert chat_state["google_form_previews"] == {
        "key": {
            "created_at": 123,
            "ok": False,
            "status": "failed",
            "token": "",
            "form_url": "https://forms.gle/example",
            "message_id": "",
        }
    }


def test_chat_responder_compatibility_wrappers_use_resume_home(tmp_path, monkeypatch):
    monkeypatch.setattr(
        chat_responder.config,
        "RESUME_FILE",
        str(tmp_path / "resume.md"),
    )
    state = {"42": {"replies_count": 2}}

    chat_responder.save_state(state)

    assert chat_responder._state_path() == str(
        tmp_path / "chat_responder_state.json"
    )
    assert chat_responder.load_state() == state


def test_chat_responder_state_wrappers_honor_explicit_runtime_paths(tmp_path, monkeypatch):
    paths = RuntimePaths(
        home_dir=str(tmp_path / "profile-a"),
        hh_state_dir=str(tmp_path / "profile-a" / "state"),
        resume_file=str(tmp_path / "profile-a" / "resume.md"),
    )
    monkeypatch.setattr(
        chat_responder.config,
        "RESUME_FILE",
        str(tmp_path / "profile-b" / "resume.md"),
    )
    state = {"42": {"replies_count": 2}}

    chat_responder.save_state(state, paths)

    assert chat_responder._state_path(paths) == str(
        tmp_path / "profile-a" / "chat_responder_state.json"
    )
    assert chat_responder.load_state(paths) == state
