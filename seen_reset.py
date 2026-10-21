"""Admin «🧹 Сброс просмотренных»: forget skipped vacancies so the next search re-evaluates them.

Only skip/dry-run marks are removed — never applied, manual, uncertain or
guard outcomes, so a reset can never cause a repeated application.
"""
from __future__ import annotations

import json
import os
import shutil
import time
from datetime import datetime, timedelta

SCOPES = {"all": "всё", "last_run": "последний прогон", "day": "за сутки"}
_RESETTABLE_PREFIXES = ("skipped_", "dry_run")


def _resettable(entry: dict) -> bool:
    return str(entry.get("action") or "").startswith(_RESETTABLE_PREFIXES)


def _since(home_dir: str, scope: str, direction_scope="active") -> str:
    if scope == "all":
        return ""
    if scope == "day":
        return (datetime.now() - timedelta(days=1)).isoformat(timespec="seconds")
    if scope == "last_run":
        try:
            with open(os.path.join(home_dir, "run_history.jsonl"), encoding="utf-8") as stream:
                lines = [line for line in stream if line.strip()]
        except OSError:
            lines = []
        runs = []
        for line in lines:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get("started_at"):
                runs.append(row)
        import analytics
        runs = analytics.scoped_runs(runs, events_file=os.path.join(home_dir, "analytics_events.jsonl"), scope=direction_scope)
        # A run still in progress (started by cron or by hand) is not «the last run».
        finished = [row for row in runs if row.get("finished_at")]
        if finished or runs:
            return str((finished or runs)[-1]["started_at"])
        raise ValueError("Прогонов ещё не было.")
    raise ValueError(f"Неизвестный период: {scope}")


def plan(home_dir: str, scope: str, *, direction_scope="active") -> list[str]:
    import direction_history
    view = direction_history.HistoryScope(home_dir, direction_scope)
    since = _since(home_dir, scope, direction_scope)
    try:
        with open(os.path.join(home_dir, "seen_vacancies.json"), encoding="utf-8") as stream:
            seen = json.load(stream)
    except (OSError, ValueError):
        return []
    return [key for key, entry in seen.items()
            if isinstance(entry, dict) and view.matches(entry) and _resettable(entry) and str(entry.get("date") or "") >= since]


def apply(home_dir: str, scope: str, *, direction_scope="active") -> int:
    keys = set(plan(home_dir, scope, direction_scope=direction_scope))
    since = _since(home_dir, scope, direction_scope)
    import evaluation_cache
    if not keys:
        evaluation_cache.reset(home_dir, direction_scope=direction_scope, since=since)
        return 0
    path = os.path.join(home_dir, "seen_vacancies.json")
    shutil.copy2(path, path + time.strftime(".bak-%Y%m%d-%H%M%S"))
    from seen import _store
    removed = 0
    def forget(data):
        nonlocal removed
        for key in keys:
            # A concurrent result can promote a skipped vacancy to applied.
            if key in data and _resettable(data[key]):
                del data[key]
                removed += 1
    _store(path).update(forget)
    # Re-evaluation also means forgetting remembered scores in this direction.
    evaluation_cache.reset(home_dir, direction_scope=direction_scope, since=since)
    return removed
