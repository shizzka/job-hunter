"""Local process identities for durable attempt ownership."""
import os
from pathlib import Path


def process_identity(pid: int) -> tuple[str, str]:
    stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
    boot = Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    return stat[0], f"{boot}:{stat[19]}"


def current_process_owner() -> dict:
    try:
        _, start = process_identity(os.getpid())
    except (OSError, IndexError):
        start = ""
    return {"owner_pid": os.getpid(), "owner_process_start": start}


def owner_alive(item: dict) -> bool:
    try:
        pid = int(item.get("owner_pid") or 0)
    except (ValueError, TypeError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except (ProcessLookupError, OverflowError):
        return False
    except OSError:
        return True  # inability to inspect a process is not proof of death
    try:
        state, identity = process_identity(pid)
    except (OSError, IndexError):
        return True
    return state not in {"Z", "X"} and (
        not item.get("owner_process_start") or item["owner_process_start"] == identity)
