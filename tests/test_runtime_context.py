from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from runtime_context import ChatResponderLimits, RuntimePaths


def _settings(home: str) -> SimpleNamespace:
    return SimpleNamespace(
        JOB_HUNTER_HOME=home,
        HH_STATE_DIR=f"{home}/state",
        RESUME_FILE=f"{home}/resume.md",
    )


def test_runtime_paths_capture_active_settings() -> None:
    paths = RuntimePaths.from_config(_settings("/tmp/profile-a"))

    assert paths.home_dir == "/tmp/profile-a"
    assert paths.hh_state_dir == "/tmp/profile-a/state"
    assert paths.resume_file == "/tmp/profile-a/resume.md"


def test_runtime_paths_are_a_late_immutable_snapshot() -> None:
    settings = _settings("/tmp/profile-a")
    first = RuntimePaths.from_config(settings)
    settings.JOB_HUNTER_HOME = "/tmp/profile-b"

    second = RuntimePaths.from_config(settings)

    assert first.home_dir == "/tmp/profile-a"
    assert second.home_dir == "/tmp/profile-b"
    with pytest.raises(FrozenInstanceError):
        first.home_dir = "/tmp/changed"


def test_chat_responder_limits_capture_environment() -> None:
    limits = ChatResponderLimits.from_env(
        {
            "HH_CHAT_MAX_REPLIES_PER_CHAT": "7",
            "HH_CHAT_REPLY_COOLDOWN_S": "12",
        }
    )

    assert limits.max_replies_per_chat == 7
    assert limits.reply_cooldown_s == 12


def test_chat_responder_limits_preserve_defaults_and_validation() -> None:
    assert ChatResponderLimits.from_env({}) == ChatResponderLimits(
        max_replies_per_chat=5,
        reply_cooldown_s=30,
    )
    with pytest.raises(ValueError):
        ChatResponderLimits.from_env({"HH_CHAT_MAX_REPLIES_PER_CHAT": "invalid"})
