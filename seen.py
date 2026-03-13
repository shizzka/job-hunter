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
    applied = sum(1 for v in data.values() if v.get("action") == "applied")
    skipped = sum(
        1
        for v in data.values()
        if (
            action := (v.get("action") or "")
        ).startswith(("skipped", "apply_failed", "manual_"))
    )
    return {"total": len(data), "applied": applied, "skipped": skipped}
