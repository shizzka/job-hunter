"""Трекинг просмотренных вакансий — не откликаемся повторно."""
import json
import os
import logging
from datetime import datetime

import config

log = logging.getLogger("seen")

_seen: dict | None = None


def _load() -> dict:
    global _seen
    if _seen is not None:
        return _seen

    if os.path.exists(config.SEEN_VACANCIES_FILE):
        try:
            with open(config.SEEN_VACANCIES_FILE) as f:
                _seen = json.load(f)
        except Exception:
            _seen = {}
    else:
        _seen = {}

    return _seen


def _save():
    if _seen is None:
        return
    os.makedirs(os.path.dirname(config.SEEN_VACANCIES_FILE), exist_ok=True)
    with open(config.SEEN_VACANCIES_FILE, "w") as f:
        json.dump(_seen, f, ensure_ascii=False, indent=2)


def is_seen(vacancy_id: str) -> bool:
    """Уже видели эту вакансию?"""
    return vacancy_id in _load()


def mark_seen(vacancy_id: str, vacancy: dict, action: str = "applied"):
    """Отметить вакансию как обработанную."""
    data = _load()
    data[vacancy_id] = {
        "title": vacancy.get("title", ""),
        "company": vacancy.get("company", ""),
        "action": action,
        "date": datetime.now().isoformat(),
    }
    _save()


def stats() -> dict:
    """Статистика по обработанным вакансиям."""
    data = _load()
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
        elif action.startswith("manual_"):
            summary["manual"] += 1
            summary["skipped"] += 1
            bucket["manual"] += 1
            bucket["skipped"] += 1
        elif action.startswith(("skipped", "apply_failed")):
            summary["skipped"] += 1
            bucket["skipped"] += 1

    return summary
