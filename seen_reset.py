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


def _since(home_dir: str, scope: str) -> str:
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
        # A run still in progress (started by cron or by hand) is not «the last run».
        finished = [row for row in runs if row.get("finished_at")]
        if finished or runs:
            return str((finished or runs)[-1]["started_at"])
        raise ValueError("Прогонов ещё не было.")
    raise ValueError(f"Неизвестный период: {scope}")


def plan(home_dir: str, scope: str) -> list[str]:
    since = _since(home_dir, scope)
    try:
        with open(os.path.join(home_dir, "seen_vacancies.json"), encoding="utf-8") as stream:
            seen = json.load(stream)
    except (OSError, ValueError):
        return []
    return [key for key, entry in seen.items()
            if isinstance(entry, dict) and _resettable(entry) and str(entry.get("date") or "") >= since]


def apply(home_dir: str, scope: str) -> int:
    keys = set(plan(home_dir, scope))
    if not keys:
        return 0
    path = os.path.join(home_dir, "seen_vacancies.json")
    shutil.copy2(path, path + time.strftime(".bak-%Y%m%d-%H%M%S"))
    with open(path, encoding="utf-8") as stream:
        seen = json.load(stream)
    for key in keys:
        seen.pop(key, None)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as stream:
        json.dump(seen, stream, ensure_ascii=False, indent=2)
    os.replace(temp, path)
    return len(keys)
