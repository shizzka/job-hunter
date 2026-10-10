"""Трекинг просмотренных вакансий — не откликаемся повторно."""
import json
import os
import logging
from datetime import datetime

import config
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


def is_seen(vacancy_id: str) -> bool:
    """Уже видели эту вакансию?"""
    return vacancy_id in _load()


def mark_seen(vacancy_id: str, vacancy: dict, action: str = "applied"):
    """Отметить вакансию как обработанную."""
    def remember(data: dict) -> None:
        prior = data.get(vacancy_id, {}).get("action")
        if (prior == "apply_uncertain" and action != prior) or (
            prior == "manual_hh_guard_stop" and action not in {prior, "apply_uncertain"}
        ):
            raise RuntimeError("HH outcome requires manual verification before changing its outcome")
        data[vacancy_id] = {
            "title": vacancy.get("title", ""),
            "company": vacancy.get("company", ""),
            "action": action,
            "date": datetime.now().isoformat(),
        }

    _store().update(remember)


def all_entries() -> dict:
    """Копия текущего seen-state для аналитики и внешних сервисов."""
    return dict(_load())


def stats() -> dict:
    """Статистика по обработанным вакансиям."""
    data = _load()
    return stats_from_data(data)


def stats_from_file(path: str) -> dict:
    """Статистика по произвольному seen-файлу."""
    return stats_from_data(_load_from_file(path))


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
