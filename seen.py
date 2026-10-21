"""Трекинг просмотренных вакансий — не откликаемся повторно."""
import contextlib
import contextvars
import json
import os
import logging
from datetime import datetime

import config
import direction_history
from state_store.protected import ProtectedJsonStore

log = logging.getLogger("seen")

def _valid_state(state: dict) -> bool:
    return all(
        isinstance(key, str) and bool(key) and isinstance(value, dict)
        and all(isinstance(value.get(field, ""), str) for field in ("title", "company", "action", "date"))
        for key, value in state.items()
    )


def _store(path: str | None = None) -> ProtectedJsonStore:
    return ProtectedJsonStore(
        path or config.SEEN_VACANCIES_FILE,
        default_factory=dict,
        validator=_valid_state,
        logger=log,
        read_error_message="seen state read failed",
    )


def _load() -> dict:
    return _store().load()


def _load_from_file(path: str) -> dict:
    if not path or not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# Dry-run keeps its marks in memory: a trial run must not consume vacancies.
_DRY_RUN_MARKS: contextvars.ContextVar[dict | None] = contextvars.ContextVar("seen_dry_run_marks", default=None)


@contextlib.contextmanager
def dry_run_overlay():
    token = _DRY_RUN_MARKS.set({})
    try:
        yield
    finally:
        _DRY_RUN_MARKS.reset(token)


def is_seen(vacancy_id: str) -> bool:
    """Уже видели эту вакансию?"""
    marks = _DRY_RUN_MARKS.get()
    entries = _visible_entries(_load(), include_safety=True)
    return (marks is not None and _key(vacancy_id) in marks) or str(vacancy_id) in entries


ACCOUNT_SAFETY_ACTIONS = {"applied", "already_applied", "apply_uncertain", "manual_hh_guard_stop"}


def _key(vacancy_id, metadata=None):
    direction = (metadata or direction_history.runtime_metadata()).get("direction")
    return f"direction:{direction}:{vacancy_id}" if direction else str(vacancy_id)


def _visible_entries(data, *, include_safety=False, scope=None):
    scope = scope or direction_history.HistoryScope(config.JOB_HUNTER_HOME)
    result = {}
    priority = {"applied": 1, "already_applied": 1, "manual_hh_guard_stop": 2, "apply_uncertain": 3}
    for key, entry in data.items():
        if not scope.matches(entry) and not (include_safety and entry.get("action") in ACCOUNT_SAFETY_ACTIONS):
            continue
        identity = str(entry.get("vacancy_id") or key)
        if include_safety and priority.get(result.get(identity, {}).get("action"), 0) > priority.get(entry.get("action"), 0):
            continue
        result[identity] = entry
    return result


def mark_seen(vacancy_id: str, vacancy: dict, action: str = "applied"):
    """Отметить вакансию как обработанную."""
    marks = _DRY_RUN_MARKS.get()
    metadata = direction_history.runtime_metadata(vacancy)
    key = _key(vacancy_id, metadata)
    entry = {"title": vacancy.get("title", ""), "company": vacancy.get("company", ""),
             "action": action, "date": datetime.now().isoformat(), "vacancy_id": str(vacancy_id),
             "search_query": vacancy.get("_search_query", ""), **metadata}
    if marks is not None:
        marks[key] = entry
        return
    def remember(data: dict) -> None:
        prior = _visible_entries(data, include_safety=True).get(str(vacancy_id), {}).get("action")
        if (prior == "apply_uncertain" and action != prior) or (
            prior == "manual_hh_guard_stop" and action not in {prior, "apply_uncertain"}
        ):
            raise RuntimeError("HH outcome requires manual verification before changing its outcome")
        data[key] = entry

    _store().update(remember)


def all_entries() -> dict:
    """Копия текущего seen-state для аналитики и внешних сервисов."""
    return _visible_entries(_load(), include_safety=True)


def raw_entries() -> dict:
    return dict(_load())


def stats() -> dict:
    """Статистика по обработанным вакансиям."""
    data = _visible_entries(_load())
    return stats_from_data(data)


def stats_from_file(path: str, *, scope="active") -> dict:
    """Статистика по произвольному seen-файлу."""
    from pathlib import Path
    view = direction_history.HistoryScope(Path(path).parent, scope)
    # Keep separate keys: totals can contain the same vacancy in two directions.
    data = {k: v for k, v in _load_from_file(path).items() if view.matches(v)}
    return stats_from_data(data)


def stats_from_data(data: dict) -> dict:
    """Статистика по обработанным вакансиям из уже загруженного словаря."""
    summary = {
        "total": len(data),
        "applied": 0,
        "skipped": 0,
        "manual": 0,
        "by_source": {},
        "by_action": {},
    }

    for vacancy_id, payload in data.items():
        vacancy_id = str(payload.get("vacancy_id") or vacancy_id)
        action = (payload.get("action") or "").strip()
        if ":" in vacancy_id:
            source = vacancy_id.split(":", 1)[0]
        elif vacancy_id.isdigit():
            # Исторически hh.ru хранился как голый numeric vacancy id без префикса.
            source = "hh"
        else:
            source = "unknown"

        bucket = summary["by_source"].setdefault(
            source,
            {
                "total": 0,
                "applied": 0,
                "skipped": 0,
                "manual": 0,
            },
        )
        bucket["total"] += 1
        summary["by_action"][action or "unknown"] = summary["by_action"].get(action or "unknown", 0) + 1

        if action == "applied":
            summary["applied"] += 1
            bucket["applied"] += 1
        elif action.startswith("manual_") or action == "apply_uncertain":
            summary["manual"] += 1
            summary["skipped"] += 1
            bucket["manual"] += 1
            bucket["skipped"] += 1
        elif action == "already_applied":
            summary["skipped"] += 1
            bucket["skipped"] += 1
        elif action.startswith(("skipped", "apply_failed")):
            summary["skipped"] += 1
            bucket["skipped"] += 1

    return summary
