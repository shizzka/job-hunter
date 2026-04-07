"""
Isolated smoke tests (B-003).

Прогоняют CLI и pipeline с временным JOB_HUNTER_HOME,
не трогая боевой state.
"""
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parent.parent
REAL_HOME = Path.home() / ".job-hunter"


def _make_isolated_home(tmp: Path) -> Path:
    """Создать временный JOB_HUNTER_HOME с копией cookies и resume."""
    home = tmp / "job-hunter-test"
    home.mkdir()
    (home / "state").mkdir()

    # Копируем cookies (нужны для live smoke, но не для import/stats)
    for pattern in ["*.cookies.json", "superjob_auth.json"]:
        for f in REAL_HOME.glob(pattern):
            shutil.copy2(f, home / f.name)

    # Копируем resume
    resume = REAL_HOME / "resume.md"
    if resume.exists():
        shutil.copy2(resume, home / "resume.md")

    # Пустой seen — чтобы не мешать
    (home / "seen_vacancies.json").write_text("{}")

    return home


@pytest.fixture()
def isolated_home(tmp_path):
    return _make_isolated_home(tmp_path)


def _run_agent(args: list[str], home: Path, timeout: int = 30) -> subprocess.CompletedProcess:
    """Запустить agent.py с изолированным HOME."""
    env = os.environ.copy()
    env["JOB_HUNTER_HOME"] = str(home)
    # Отключаем все источники по умолчанию
    env.setdefault("HH_ENABLED", "0")
    env.setdefault("SUPERJOB_ENABLED", "0")
    env.setdefault("HABR_ENABLED", "0")
    env.setdefault("GEEKJOB_ENABLED", "0")
    # Тихий режим
    env.setdefault("HEADLESS", "1")

    # Подгружаем env file если есть
    env_file = REAL_HOME / "job-hunter.env"
    if env_file.exists():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip()
            # Не перезаписываем то, что мы уже выставили
            if key not in env or key not in ("HH_ENABLED", "SUPERJOB_ENABLED", "HABR_ENABLED", "GEEKJOB_ENABLED", "JOB_HUNTER_HOME"):
                env[key] = val

    return subprocess.run(
        [str(PROJECT_ROOT / "venv" / "bin" / "python"), "agent.py"] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(PROJECT_ROOT),
        env=env,
    )


def _run_python(script: str, args: list[str], home: Path, timeout: int = 30) -> subprocess.CompletedProcess:
    env = os.environ.copy()
    env["JOB_HUNTER_HOME"] = str(home)
    return subprocess.run(
        [str(PROJECT_ROOT / "venv" / "bin" / "python"), script] + args,
        capture_output=True,
        text=True,
        timeout=timeout,
        cwd=str(PROJECT_ROOT),
        env=env,
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
        """Боевой seen не изменился после dry-run."""
        real_seen = REAL_HOME / "seen_vacancies.json"
        if real_seen.exists():
            before = real_seen.read_text()

        _run_agent(["--dry-run"], isolated_home, timeout=60)

        if real_seen.exists():
            after = real_seen.read_text()
            assert before == after, "Боевой seen_vacancies.json изменился!"

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
