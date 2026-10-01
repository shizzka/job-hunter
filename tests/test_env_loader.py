import os
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).parent.parent


def test_env_loader_emits_literal_values_without_shell_evaluation(tmp_path):
    marker = tmp_path / "must-not-exist"
    env_file = tmp_path / "job-hunter.env"
    env_file.write_text(
        "HH_SEARCH_QUERIES=QA engineer||SDET||automation tester\n"
        f"SHELL_PAYLOAD=$(touch {marker})\n",
        encoding="utf-8",
    )

    result = subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "env_loader.py"), str(env_file)],
        cwd=PROJECT_ROOT,
        capture_output=True,
        check=True,
    )

    fields = result.stdout.split(b"\0")
    assert fields[:-1] == [
        b"HH_SEARCH_QUERIES",
        b"QA engineer||SDET||automation tester",
        b"SHELL_PAYLOAD",
        f"$(touch {marker})".encode(),
    ]
    assert not marker.exists()


def test_run_sh_exports_literal_env_values_without_sourcing(tmp_path):
    marker = tmp_path / "must-not-exist"
    env_file = tmp_path / "job-hunter.env"
    env_file.write_text(
        f"SHELL_PAYLOAD=$(touch {marker})\n",
        encoding="utf-8",
    )
    wrapper = tmp_path / "python-wrapper"
    wrapper.write_text(
        "#!/bin/sh\n"
        "if [ \"$1\" = \"env_loader.py\" ]; then\n"
        f"  exec {sys.executable} \"$@\"\n"
        "fi\n"
        "printf '%s\\n' \"$SHELL_PAYLOAD\"\n",
        encoding="utf-8",
    )
    wrapper.chmod(0o755)
    env = os.environ.copy()
    env["JOB_HUNTER_PYTHON"] = str(wrapper)
    env["JOB_HUNTER_ENV_FILE"] = str(env_file)

    result = subprocess.run(
        [str(PROJECT_ROOT / "run.sh"), "profiles"],
        cwd=PROJECT_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"$(touch {marker})"
    assert not marker.exists()
