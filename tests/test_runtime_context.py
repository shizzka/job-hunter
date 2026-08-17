from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from runtime_context import RuntimePaths


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
