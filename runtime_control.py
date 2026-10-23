"""Helpers for Job Hunter process control and runtime snapshots."""
from __future__ import annotations

import asyncio
from collections.abc import Callable
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import config
import profile_access
from state_store.json_store import JsonStore, atomic_write_text, file_lock
from state_store.private_journal import open_private_append, read_json_records

PROJECT_ROOT = Path(__file__).resolve().parent
AGENT_DAEMON_TOKENS = ("agent.py", "--daemon")
BOT_TOKENS = ("telegram_bot.py",)


def _ensure_parent(path: str) -> None:
    parent = os.path.dirname(path)
    if parent:
        os.makedirs(parent, exist_ok=True)


def read_json_file(path: str) -> dict | None:
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            payload = json.load(f)
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def write_json_file(path: str, payload: dict) -> None:
    JsonStore(path).save(payload)


def read_pid_file(path: str) -> int | None:
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read().strip()
    except FileNotFoundError:
        return None
    if not raw.isdigit():
        return None
    pid = int(raw)
    return pid if pid > 0 else None


def write_pid_file(path: str, pid: int) -> None:
    with file_lock(path):
        atomic_write_text(path, f"{int(pid)}\n")


def remove_pid_file(path: str, pid: int | None = None) -> None:
    with file_lock(path):
        existing = read_pid_file(path)
        if pid is not None and existing is not None and existing != pid:
            return
        try:
            os.remove(path)
        except FileNotFoundError:
            pass


def is_pid_running(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def read_process_argv(pid: int | None) -> list[str]:
    if not pid or pid <= 0:
        return []
    path = Path(f"/proc/{pid}/cmdline")
    try:
        raw = path.read_bytes()
    except OSError:
        return []
    return [os.fsdecode(arg) for arg in raw.rstrip(b"\0").split(b"\0")] if raw else []


def read_process_cmdline(pid: int | None) -> str:
    return " ".join(read_process_argv(pid))


def _cmdline_matches(cmdline: str, expected_tokens: tuple[str, ...] | list[str] | None) -> bool:
    if not expected_tokens:
        return True
    return all(token in cmdline for token in expected_tokens)


def _lifecycle_lock(pid_file: str):
    # Separate from the PID writer lock: shutdown cleanup must remain usable
    # while another controller waits for that process to exit.
    return file_lock(f"{pid_file}.lifecycle")


def describe_process(
    pid_file: str,
    *,
    expected_tokens: tuple[str, ...] | list[str] | None = None,
    fallback_pid: int | None = None,
) -> dict:
    with _lifecycle_lock(pid_file):
        return _describe_process_unlocked(pid_file, expected_tokens=expected_tokens, fallback_pid=fallback_pid)


def _describe_process_unlocked(
    pid_file: str,
    *,
    expected_tokens: tuple[str, ...] | list[str] | None = None,
    fallback_pid: int | None = None,
) -> dict:
    pid_from_file = read_pid_file(pid_file)
    running_pid = None
    cmdline = ""

    for candidate in (pid_from_file, fallback_pid):
        if not candidate or not is_pid_running(candidate):
            continue
        current_cmdline = read_process_cmdline(candidate)
        if not _cmdline_matches(current_cmdline, expected_tokens):
            continue
        running_pid = candidate
        cmdline = current_cmdline
        break

    if running_pid and pid_from_file != running_pid:
        write_pid_file(pid_file, running_pid)

    stale = bool(pid_from_file and pid_from_file != running_pid)
    if stale:
        remove_pid_file(pid_file, pid_from_file)

    return {
        "pid_file": pid_file,
        "pid": running_pid or pid_from_file or 0,
        "running": bool(running_pid),
        "stale": stale,
        "cmdline": cmdline,
    }


def register_current_process(
    pid_file: str,
    *,
    expected_tokens: tuple[str, ...] | list[str] | None = None,
    wait_timeout_sec: float = 10.0,
    retry_interval_sec: float = 1.0,
) -> int:
    pid = os.getpid()
    deadline = time.monotonic() + max(0.0, float(wait_timeout_sec))
    while True:
        with _lifecycle_lock(pid_file):
            current = _describe_process_unlocked(pid_file, expected_tokens=expected_tokens)
            if not (current["running"] and current["pid"] != pid):
                write_pid_file(pid_file, pid)
                return pid
        if time.monotonic() >= deadline:
            raise RuntimeError(f"Process already running: pid={current['pid']}")
        time.sleep(max(0.1, float(retry_interval_sec)))


def unregister_current_process(pid_file: str) -> None:
    remove_pid_file(pid_file, os.getpid())


def latest_run_entry(path: str | None = None) -> dict | None:
    run_history_file = path or config.RUN_HISTORY_FILE
    try:
        records = read_json_records(run_history_file)
    except OSError:
        return None
    return records[-1] if records else None


def has_completed_search(path: str) -> bool:
    """A successful search anywhere in this profile's history completes setup."""
    try:
        records = read_json_records(path)
    except OSError:
        return False
    for record in reversed(records):
        kind = record.get("kind")
        is_search = kind == "search" or (not kind and record.get("mode") in {"search", "dry-run"})
        # Legacy terminal search records have ok but no status/finished_at.
        if (is_search and record.get("ok") is True
                and record.get("status") in {None, "finished"}
                and ("finished_at" not in record or bool(record["finished_at"]))):
            return True
    return False


def tail_file(path: str, lines: int = 40, chars: int = 3500) -> str:
    if lines <= 0 or not os.path.exists(path):
        return ""
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            chunk = f.readlines()[-lines:]
    except OSError:
        return ""
    text = "".join(chunk).strip()
    if chars and len(text) > chars:
        return text[-chars:]
    return text


AGENT_TASK_FLAGS: tuple[str, ...] = (
    "--search",
    "--dry-run",
    "--check",
    "--digest",
    "--analyze-resume",
    "--analytics-backfill",
    "--grab-resume",
    "--refresh-resume",
    "--daemon",
)

AGENT_FLAG_LABELS: dict[str, str] = {
    "--search": "поиск",
    "--dry-run": "dry-run",
    "--check": "проверка инвайтов",
    "--digest": "дайджест",
    "--analyze-resume": "анализ резюме",
    "--analytics-backfill": "backfill аналитики",
    "--grab-resume": "скачивание резюме",
    "--refresh-resume": "обновление резюме HH",
    "--daemon": "демон",
}


def find_agent_pids_for_profile(
    profile_name: str,
    *,
    flags: tuple[str, ...] | list[str] | None = None,
) -> list[dict]:
    """Найти agent.py-процессы для профиля. Сканирует /proc, без pid-файлов.

    Возвращает [{"pid": int, "cmdline": str, "flag": str}, ...] для процессов,
    запущенных как через бот, так и снаружи (cron, shell).
    """
    profile_access.require_profile(profile_name or "default")
    target_flags = tuple(flags) if flags else AGENT_TASK_FLAGS
    found: list[dict] = []
    proc_dir = Path("/proc")
    if not proc_dir.exists():
        return found
    self_pid = os.getpid()
    for entry in proc_dir.iterdir():
        if not entry.name.isdigit():
            continue
        pid = int(entry.name)
        if pid <= 0 or pid == self_pid:
            continue
        argv = read_process_argv(pid)
        matched_flag = _agent_process_flag(argv, profile_name or "default", target_flags)
        if not matched_flag:
            continue
        found.append({"pid": pid, "cmdline": " ".join(argv), "argv": argv, "flag": matched_flag})
    return found


def _agent_process_flag(argv, profile_name, flags=AGENT_TASK_FLAGS):
    script = next((i for i, arg in enumerate(argv) if Path(arg).name == "agent.py"), None)
    if script is None or script == 0 or any(not arg.startswith("-") for arg in argv[1:script]):
        return ""
    args = argv[script + 1:]
    profiles = []
    for i, arg in enumerate(args):
        if arg == "--profile":
            profiles.append(args[i + 1] if i + 1 < len(args) else "")
        elif arg.startswith("--profile="):
            profiles.append(arg.partition("=")[2])
    if profiles != [profile_name] and not (not profiles and profile_name == "default"):
        return ""
    return next((flag for flag in flags if flag in args), "")


def agent_command_argv(profile_name: str, *flags: str) -> list[str]:
    profile_access.require_profile(profile_name or "default")
    argv = [sys.executable, str(PROJECT_ROOT / "agent.py")]
    if profile_name:
        argv.extend(["--profile", profile_name])
    argv.extend(flags)
    return argv


def bot_command_argv(profile_name: str) -> list[str]:
    profile_access.require_profile(profile_name or "default")
    argv = [sys.executable, str(PROJECT_ROOT / "telegram_bot.py")]
    if profile_name:
        argv.extend(["--profile", profile_name])
    return argv


def client_hh_auth_command_argv(
    profile_name: str,
    *,
    timeout_sec: int = 900,
    import_resumes: bool = False,
) -> list[str]:
    profile_access.require_profile(profile_name)
    argv = [sys.executable, str(PROJECT_ROOT / "client_hh_auth.py"), "--profile", profile_name]
    if timeout_sec > 0:
        argv.extend(["--timeout", str(int(timeout_sec))])
    if import_resumes:
        argv.append("--import-resumes")
    return argv


def start_background_process(
    argv: list[str],
    *,
    pid_file: str,
    log_file: str,
    expected_tokens: tuple[str, ...] | list[str] | None = None,
    fallback_pid: int | None = None,
    cwd: str | None = None,
    start_delay: float = 0.8,
) -> dict:
    with _lifecycle_lock(pid_file):
        current = _describe_process_unlocked(pid_file, expected_tokens=expected_tokens, fallback_pid=fallback_pid)
        if current["running"]:
            return {"ok": False, "already_running": True, "pid": current["pid"], "log_file": log_file}

        _ensure_parent(log_file)
        with os.fdopen(open_private_append(log_file), "ab") as log:
            child_env = profile_access.worker_environment()
            child_env["JOB_HUNTER_BACKGROUND"] = "1"
            proc = subprocess.Popen(
                argv,
                cwd=cwd or str(PROJECT_ROOT),
                stdout=log,
                stderr=log,
                env=child_env,
                start_new_session=True,
            )
        try:
            write_pid_file(pid_file, proc.pid)
        except Exception:
            # Only this just-created child is in scope; do not leave a worker
            # unregistered if PID publication fails.
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            except ProcessLookupError:
                pass
            raise

    # The child can now register itself; never hold lifecycle while waiting
    # for startup or it would be unable to acquire the registration lock.
    time.sleep(max(0.0, start_delay))
    current = describe_process(pid_file, expected_tokens=expected_tokens, fallback_pid=proc.pid)
    return {
        "ok": current["running"],
        "already_running": False,
        "pid": current["pid"],
        "log_file": log_file,
    }


def stop_process(
    pid_file: str,
    *,
    expected_tokens: tuple[str, ...] | list[str] | None = None,
    fallback_pid: int | None = None,
    timeout: float = 15.0,
) -> dict:
    with _lifecycle_lock(pid_file):
        return _stop_process_unlocked(pid_file, expected_tokens=expected_tokens, fallback_pid=fallback_pid, timeout=timeout)


def _stop_process_unlocked(
    pid_file: str,
    *,
    expected_tokens: tuple[str, ...] | list[str] | None = None,
    fallback_pid: int | None = None,
    timeout: float = 15.0,
) -> dict:
    current = _describe_process_unlocked(pid_file, expected_tokens=expected_tokens, fallback_pid=fallback_pid)
    pid = current["pid"]
    if not pid:
        return {"ok": True, "already_stopped": True, "pid": 0}
    if not current["running"]:
        remove_pid_file(pid_file, pid)
        return {"ok": True, "already_stopped": True, "pid": pid}

    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        remove_pid_file(pid_file, pid)
        return {"ok": True, "already_stopped": True, "pid": pid}

    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        if not is_pid_running(pid):
            remove_pid_file(pid_file, pid)
            return {"ok": True, "stopped": True, "signal": "SIGTERM", "pid": pid}
        time.sleep(0.2)

    try:
        os.kill(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    time.sleep(0.2)
    stopped = not is_pid_running(pid)
    if stopped:
        remove_pid_file(pid_file, pid)
    return {"ok": stopped, "stopped": stopped, "signal": "SIGKILL", "pid": pid}


async def run_command_capture(
    argv: list[str],
    *,
    cwd: str | None = None,
    timeout: float | None = None,
    on_start: Callable[[int], None] | None = None,
) -> dict:
    proc = await asyncio.create_subprocess_exec(
        *argv,
        env=profile_access.worker_environment(),
        cwd=cwd or str(PROJECT_ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        start_new_session=True,
    )
    if on_start:
        on_start(proc.pid)
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=5)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
        return {
            "ok": False,
            "timeout": True,
            "returncode": proc.returncode,
            "stdout": "",
            "stderr": f"Command timed out after {timeout} seconds",
            "argv": argv,
        }

    return {
        "ok": proc.returncode == 0,
        "timeout": False,
        "returncode": proc.returncode,
        "stdout": stdout.decode("utf-8", errors="replace").strip(),
        "stderr": stderr.decode("utf-8", errors="replace").strip(),
        "argv": argv,
    }


def stop_pid(pid: int, *, timeout: float = 15.0, process_group: bool = False,
             expected_profile: str | None = None, expected_argv: list[str] | None = None) -> dict:
    if not pid or pid <= 0:
        return {"ok": True, "already_stopped": True, "pid": 0}
    if not is_pid_running(pid):
        return {"ok": True, "already_stopped": True, "pid": pid}

    def _send(sig: int) -> bool:
        if expected_profile is not None:
            argv = read_process_argv(pid)
            if (not profile_access.profile_allowed(expected_profile)
                    or not _agent_process_flag(argv, expected_profile)
                    or expected_argv is None or argv != expected_argv):
                return False
        if process_group:
            os.killpg(pid, sig)
        else:
            os.kill(pid, sig)
        return True

    try:
        if not _send(signal.SIGTERM):
            return {"ok": False, "ownership_changed": True, "pid": pid}
    except ProcessLookupError:
        return {"ok": True, "already_stopped": True, "pid": pid}

    deadline = time.monotonic() + max(0.0, timeout)
    while time.monotonic() < deadline:
        if not is_pid_running(pid):
            return {"ok": True, "stopped": True, "signal": "SIGTERM", "pid": pid}
        time.sleep(0.2)

    try:
        if not _send(signal.SIGKILL):
            return {"ok": False, "ownership_changed": True, "pid": pid}
    except ProcessLookupError:
        pass
    time.sleep(0.2)
    return {"ok": not is_pid_running(pid), "stopped": True, "signal": "SIGKILL", "pid": pid}
