"""
Isolated smoke tests (B-003).

Прогоняют CLI и pipeline с временным JOB_HUNTER_HOME,
не трогая боевой state.
"""
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent


def _make_isolated_home(tmp: Path) -> Path:
    """Create synthetic state only; ordinary smoke tests never read real data."""
    home = tmp / "job-hunter-test"
    home.mkdir()
    (home / "state").mkdir()

    (home / "resume.md").write_text("# Synthetic candidate\nOffline smoke fixture.\n")

    # Пустой seen — чтобы не мешать
    (home / "seen_vacancies.json").write_text("{}")

    return home


@pytest.fixture()
def isolated_home(tmp_path):
    return _make_isolated_home(tmp_path)


def _isolated_env(home: Path) -> dict[str, str]:
    """Allowlist process essentials; never inherit credentials or runtime paths."""
    return {
        "PATH": os.defpath,
        "LANG": "C.UTF-8",
        "HOME": str(home),
        "JOB_HUNTER_HOME": str(home),
        "JOB_HUNTER_ENV_FILE": str(home / "nonexistent.env"),
        "JOB_HUNTER_LOG_FILE": str(home / "agent.log"),
        "JOB_HUNTER_BOT_LOG_FILE": str(home / "bot.log"),
        "HH_ENABLED": "0",
        "SUPERJOB_ENABLED": "0",
        "HABR_ENABLED": "0",
        "GEEKJOB_ENABLED": "0",
        "HEADLESS": "1",
    }


def _run_agent(args: list[str], home: Path, timeout: int = 30) -> subprocess.CompletedProcess:
    """Запустить agent.py с изолированными HOME и настройками."""

    return subprocess.run(
        [sys.executable, "agent.py"] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(PROJECT_ROOT),
        env=_isolated_env(home),
    )


def _run_python(script: str, args: list[str], home: Path, timeout: int = 30) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, script] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(PROJECT_ROOT),
        env=_isolated_env(home),
    )


class TestCLIHealth:
    """Smoke-S1: CLI запускается и не падает."""

    def test_help(self, isolated_home):
        r = _run_agent(["--help"], isolated_home)
        assert r.returncode == 0
        assert "Job Hunter" in r.stdout or "usage" in r.stdout.lower()

    def test_telegram_bot_help(self, isolated_home):
        r = _run_python("telegram_bot.py", ["--help"], isolated_home)
        assert r.returncode == 0
        assert "Telegram bot" in r.stdout or "usage" in r.stdout.lower()

    def test_control_help(self, isolated_home):
        r = _run_python("job_hunter_ctl.py", ["--help"], isolated_home)
        assert r.returncode == 0
        assert "process control" in r.stdout.lower() or "usage" in r.stdout.lower()

    def test_stats_empty(self, isolated_home):
        r = _run_agent(["--stats"], isolated_home)
        assert r.returncode == 0


class TestIsolatedDryRun:
    """Smoke-S3: dry-run с пустым state, все источники выключены."""

    def test_dry_run_no_sources(self, isolated_home):
        r = _run_agent(["--dry-run"], isolated_home, timeout=60)
        assert r.returncode == 0
        # Не должно быть необработанных исключений
        assert "Traceback" not in r.stderr

    def test_state_isolation(self, isolated_home):
        """A synthetic neighbouring profile is untouched by dry-run."""
        other_home = isolated_home.parent / "other-profile"
        other_home.mkdir()
        seen_file = other_home / "seen_vacancies.json"
        seen_file.write_text('{"synthetic": true}')
        before = seen_file.read_bytes()
        r = _run_agent(["--dry-run"], isolated_home, timeout=60)
        assert r.returncode == 0
        assert seen_file.read_bytes() == before

    def test_isolated_seen_created(self, isolated_home):
        _run_agent(["--dry-run"], isolated_home, timeout=60)
        seen_file = isolated_home / "seen_vacancies.json"
        assert seen_file.exists()


class TestStatsAndAnalytics:
    """Smoke-S5: stats и analytics на пустом state."""

    def test_stats_with_empty_history(self, isolated_home):
        # Создаём пустую историю
        (isolated_home / "run_history.jsonl").write_text("")
        r = _run_agent(["--stats"], isolated_home)
        assert r.returncode == 0
        assert "Traceback" not in r.stderr

    def test_stats_with_sample_history(self, isolated_home):
        entry = json.dumps({
            "timestamp": "2026-03-16T12:00:00",
            "mode": "dry-run",
            "ok": True,
            "found": 5,
            "applied": 0,
            "source_stats": {},
        })
        (isolated_home / "run_history.jsonl").write_text(entry + "\n")
        r = _run_agent(["--stats"], isolated_home)
        assert r.returncode == 0


@pytest.mark.parametrize("runner", ["agent", "python"])
def test_smoke_children_ignore_parent_secrets_and_source_flags(isolated_home, monkeypatch, runner):
    for key in (
        "HUNTER_CONTROL_BOT_TOKEN", "HUNTER_NOTIFY_BOT_TOKEN", "HUNTER_BOT_TOKEN",
        "NOTIFY_CHAT_ID", "GROQ_API_KEY", "OPENAI_API_KEY", "OFFICE_URL", "OFFICE_DB",
        "HH_ENABLED", "SUPERJOB_ENABLED", "HABR_ENABLED", "GEEKJOB_ENABLED",
        "JOB_HUNTER_LOG_FILE", "JOB_HUNTER_ENV_FILE", "HTTP_PROXY",
    ):
        monkeypatch.setenv(key, "synthetic-parent-value")
    captured = []

    def run(argv, **kwargs):
        captured.append(kwargs["env"])
        return subprocess.CompletedProcess(argv, 0, "", "")

    monkeypatch.setattr(subprocess, "run", run)
    if runner == "agent":
        _run_agent(["--dry-run"], isolated_home)
    else:
        _run_python("telegram_bot.py", ["--help"], isolated_home)
    env = captured[0]
    assert "synthetic-parent-value" not in env.values()
    assert env["HOME"] == env["JOB_HUNTER_HOME"] == str(isolated_home)
    assert all(env[source + "_ENABLED"] == "0" for source in ("HH", "SUPERJOB", "HABR", "GEEKJOB"))
    assert not any("TOKEN" in key or "API_KEY" in key for key in env)


def test_smoke_setup_does_not_read_candidate_home(tmp_path, monkeypatch):
    def forbidden_read(*args, **kwargs):
        pytest.fail("Offline fixture construction must not read candidate data")

    monkeypatch.setattr(Path, "read_text", forbidden_read)
    monkeypatch.setattr(Path, "read_bytes", forbidden_read)
    home = _make_isolated_home(tmp_path)
    assert not list(home.glob("*cookies*"))
    assert (home / "resume.md").exists()
