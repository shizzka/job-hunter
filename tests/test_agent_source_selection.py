import os
import subprocess
from pathlib import Path

import pytest

import config
from agent import _apply_source_selection


PROJECT_ROOT = Path(__file__).parent.parent
SOURCE_FLAGS = {
    "hh": "HH_ENABLED",
    "superjob": "SUPERJOB_ENABLED",
    "habr": "HABR_ENABLED",
    "geekjob": "GEEKJOB_ENABLED",
}


@pytest.mark.parametrize("selected", SOURCE_FLAGS)
def test_apply_source_selection_enables_only_requested_source(selected, monkeypatch):
    for config_name in SOURCE_FLAGS.values():
        monkeypatch.setattr(config, config_name, True)

    _apply_source_selection(selected)

    assert {
        source: getattr(config, config_name)
        for source, config_name in SOURCE_FLAGS.items()
    } == {
        source: source == selected
        for source in SOURCE_FLAGS
    }


@pytest.mark.parametrize(
    ("mode", "source", "action"),
    (
        ("superjob-dry-run", "superjob", "--dry-run"),
        ("superjob-search", "superjob", "--search"),
        ("habr-dry-run", "habr", "--dry-run"),
        ("habr-search", "habr", "--search"),
        ("geekjob-dry-run", "geekjob", "--dry-run"),
        ("geekjob-search", "geekjob", "--search"),
    ),
)
def test_run_sh_routes_named_profile_source_commands(tmp_path, mode, source, action):
    fake_python = tmp_path / "python"
    fake_python.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n", encoding="utf-8")
    fake_python.chmod(0o755)
    env = os.environ.copy()
    env["JOB_HUNTER_PYTHON"] = str(fake_python)
    env["JOB_HUNTER_ENV_FILE"] = str(tmp_path / "missing.env")
    env.pop("JOB_HUNTER_DEFAULT_PROFILE", None)

    result = subprocess.run(
        [str(PROJECT_ROOT / "run.sh"), "--profile", "qa", mode],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == [
        "agent.py",
        "--profile",
        "qa",
        "--source",
        source,
        action,
    ]
