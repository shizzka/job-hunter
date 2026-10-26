"""A profile-local, process-bound snapshot of the daemon's next search window."""
from datetime import datetime, timedelta
import logging
from pathlib import Path

import runtime_control
from state_store.json_store import JsonStore
from state_store.locking import StateLockBusy

log = logging.getLogger(__name__)


def publish(home, *, pid, interval_min, delay=None, status="waiting"):
    now = datetime.now().astimezone()
    state = {"pid": pid, "interval_min": interval_min, "status": status,
             "updated_at": now.isoformat(),
             "next_search_at": (now + timedelta(seconds=max(0, delay))).isoformat()
             if delay is not None else None}
    try:
        JsonStore(Path(home) / "daemon_schedule.json").save(state)
    except (OSError, ValueError, StateLockBusy) as exc:
        log.warning("Scheduler window unavailable: %s", type(exc).__name__)


def describe(home, daemon, interval_min, *, now=None):
    if not daemon.get("running"):
        return "расписание выключено"
    unknown = "расписание включено, время пока неизвестно"
    state = runtime_control.read_json_file(str(Path(home) / "daemon_schedule.json"))
    if (not state or not daemon.get("pid") or state.get("pid") != daemon["pid"]
            or state.get("interval_min") != interval_min):
        return unknown
    now = now or datetime.now().astimezone()
    try:
        updated = datetime.fromisoformat(state["updated_at"])
        if updated.tzinfo is None or not -5 <= (now - updated).total_seconds() <= 180:
            return unknown
        if state.get("status") == "searching":
            return "поиск уже выполняется; следующее время появится после завершения"
        due = datetime.fromisoformat(state["next_search_at"])
        if state.get("status") != "waiting" or due.tzinfo is None:
            return unknown
    except (TypeError, KeyError, ValueError):
        return unknown
    return ("окно наступило, ожидается запуск" if due <= now else
            "после " + due.astimezone().strftime("%d.%m %H:%M") + " (по расписанию)")
