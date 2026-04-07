"""Стадированный pipeline резюме для hh.ru: normal -> fun -> ats-heavy."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

import config
from outcome import status_bucket as _status_bucket


def _now() -> datetime:
    return datetime.now()


def _to_iso(value: datetime | None) -> str:
    return value.isoformat() if value else ""


def _from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


_state: dict | None = None


def _merge_variant_lists(primary: list[dict] | None, fallback: list[dict] | None) -> list[dict]:
    fallback_by_name = {
        str(item.get("name") or "").strip(): item
        for item in (fallback or [])
        if str(item.get("name") or "").strip()
    }

    merged: list[dict] = []
    seen_names: set[str] = set()

    for item in primary or []:
        name = str(item.get("name") or "").strip()
        merged_item = dict(item)
        fallback_item = fallback_by_name.get(name, {})
        if not merged_item.get("title") and fallback_item.get("title"):
            merged_item["title"] = fallback_item["title"]
        if not merged_item.get("id") and fallback_item.get("id"):
            merged_item["id"] = fallback_item["id"]
        merged.append(merged_item)
        if name:
            seen_names.add(name)

    for item in fallback or []:
        name = str(item.get("name") or "").strip()
        if name and name in seen_names:
            continue
        merged.append(dict(item))

    return merged


def _load() -> dict:
    global _state
    if _state is not None:
        return _state

    if os.path.exists(config.HH_RESUME_PIPELINE_FILE):
        try:
            with open(config.HH_RESUME_PIPELINE_FILE) as f:
                _state = json.load(f)
        except Exception:
            _state = {}
    else:
        _state = {}
    return _state


def _save() -> None:
    if _state is None:
        return
    os.makedirs(os.path.dirname(config.HH_RESUME_PIPELINE_FILE), exist_ok=True)
    with open(config.HH_RESUME_PIPELINE_FILE, "w") as f:
        json.dump(_state, f, ensure_ascii=False, indent=2)


def enabled() -> bool:
    return config.HH_RESUME_PIPELINE_ENABLED and bool(get_variants())


def get_variants() -> list[dict]:
    variants = []
    entries = [
        ("normal", config.HH_PRIMARY_RESUME_TITLE, config.HH_PRIMARY_RESUME_ID),
        ("fun", config.HH_SECONDARY_RESUME_TITLE, config.HH_SECONDARY_RESUME_ID),
        ("ats_heavy", config.HH_TERTIARY_RESUME_TITLE, config.HH_TERTIARY_RESUME_ID),
    ]
    for name, title, resume_id in entries:
        if title or resume_id:
            variants.append(
                {
                    "name": name,
                    "title": title.strip(),
                    "id": str(resume_id or "").strip(),
                }
            )
    return variants


def get_variant_by_name(name: str) -> dict | None:
    for variant in get_variants():
        if variant["name"] == name:
            return variant
    return None


def resolve_variants(resumes: list[dict]) -> list[dict]:
    resolved = []
    for variant in get_variants():
        resolved_variant = dict(variant)
        if not resolved_variant.get("id") and resolved_variant.get("title"):
            title_cf = resolved_variant["title"].casefold()
            exact_match = None
            fuzzy_match = None
            for resume in resumes:
                resume_title = str(resume.get("title") or "")
                resume_lines = [line.strip() for line in resume_title.splitlines() if line.strip()]
                if resume_title.casefold() == title_cf:
                    exact_match = str(resume.get("id") or "").strip()
                    break
                if any(line.casefold() == title_cf for line in resume_lines):
                    exact_match = str(resume.get("id") or "").strip()
                    break
                if (
                    fuzzy_match is None
                    and len(title_cf) >= 18
                    and title_cf
                    and title_cf in resume_title.casefold()
                ):
                    fuzzy_match = str(resume.get("id") or "").strip()
            resolved_variant["id"] = exact_match or fuzzy_match or ""
        resolved.append(resolved_variant)
    return resolved


def remember_resolved_variants(resolved_variants: list[dict]) -> None:
    state = _load()
    state["_resolved_variants"] = _merge_variant_lists(
        resolved_variants,
        get_resolved_variants(),
    )
    state["_resolved_at"] = _to_iso(_now())
    _save()


def get_resolved_variants() -> list[dict]:
    state = _load()
    resolved = state.get("_resolved_variants")
    if isinstance(resolved, list) and resolved:
        return _merge_variant_lists(resolved, get_variants())
    return get_variants()


def all_entries() -> dict:
    """Копия всех реальных pipeline entries без служебных ключей."""
    return {
        vacancy_id: dict(entry)
        for vacancy_id, entry in _load().items()
        if not str(vacancy_id).startswith("_") and isinstance(entry, dict)
    }


def _entry(vacancy_id: str) -> dict | None:
    return _load().get(vacancy_id)


def _ensure_entry(vacancy: dict) -> dict:
    state = _load()
    vacancy_id = vacancy["id"]
    entry = state.setdefault(
        vacancy_id,
        {
            "id": vacancy_id,
            "title": vacancy.get("title", ""),
            "company": vacancy.get("company", ""),
            "url": vacancy.get("url", ""),
            "response_url": vacancy.get("response_url", ""),
            "source": "hh",
            "created_at": _to_iso(_now()),
            "attempts": [],
            "last_status": "",
            "last_status_at": "",
            "next_retry_at": "",
            "completed_reason": "",
        },
    )
    entry["title"] = vacancy.get("title", entry.get("title", ""))
    entry["company"] = vacancy.get("company", entry.get("company", ""))
    entry["url"] = vacancy.get("url", entry.get("url", ""))
    entry["response_url"] = vacancy.get("response_url", entry.get("response_url", ""))
    return entry


def get_attempt_count(vacancy_id: str) -> int:
    entry = _entry(vacancy_id)
    if not entry:
        return 0
    return len(entry.get("attempts", []))


def get_next_variant(vacancy_id: str) -> dict | None:
    variants = get_resolved_variants()
    index = get_attempt_count(vacancy_id)
    if 0 <= index < len(variants):
        return variants[index]
    return None


def record_successful_apply(vacancy: dict, variant: dict) -> None:
    entry = _ensure_entry(vacancy)
    attempts = entry.setdefault("attempts", [])
    if attempts and attempts[-1].get("variant") == variant["name"]:
        # Повторно ту же ступень не дублируем.
        attempts[-1]["applied_at"] = _to_iso(_now())
    else:
        attempts.append(
            {
                "variant": variant["name"],
                "resume_title": variant.get("title", ""),
                "resume_id": variant.get("id", ""),
                "applied_at": _to_iso(_now()),
            }
        )
    entry["next_retry_at"] = ""
    entry["completed_reason"] = ""
    _save()


def mark_terminal(vacancy_id: str, reason: str) -> None:
    entry = _entry(vacancy_id)
    if not entry:
        return
    entry["completed_reason"] = reason
    entry["next_retry_at"] = ""
    _save()


def _retry_eta_from_last_attempt(entry: dict) -> datetime | None:
    attempts = entry.get("attempts") or []
    if not attempts:
        return None
    last_attempt_at = _from_iso(attempts[-1].get("applied_at"))
    if not last_attempt_at:
        return None
    return last_attempt_at + timedelta(hours=config.HH_RESUME_RETRY_DELAY_HOURS)


def sync_negotiation_statuses(items: list[dict]) -> None:
    if not enabled():
        return

    state = _load()
    now = _now()
    for item in items:
        vacancy_id = str(item.get("id") or "").strip()
        if not vacancy_id or vacancy_id not in state:
            continue

        entry = state[vacancy_id]
        status_text = item.get("status", "")
        entry["last_status"] = status_text
        entry["last_status_at"] = _to_iso(now)
        entry["title"] = item.get("title", entry.get("title", ""))
        entry["company"] = item.get("company", entry.get("company", ""))
        entry["url"] = item.get("url", entry.get("url", ""))

        bucket = _status_bucket(status_text)
        if bucket == "positive":
            entry["completed_reason"] = "positive_response"
            entry["next_retry_at"] = ""
            continue

        if bucket == "rejected":
            if get_next_variant(vacancy_id) is None:
                entry["completed_reason"] = "pipeline_exhausted"
                entry["next_retry_at"] = ""
            else:
                eta = _retry_eta_from_last_attempt(entry)
                entry["next_retry_at"] = _to_iso(eta)
            continue

    _save()


def get_retry_candidates() -> list[dict]:
    if not enabled():
        return []

    state = _load()
    variants = get_resolved_variants()
    now = _now()
    items = []

    for vacancy_id, entry in state.items():
        if vacancy_id.startswith("_"):
            continue
        if entry.get("completed_reason"):
            continue

        attempts = entry.get("attempts") or []
        if not attempts:
            continue
        if len(attempts) >= len(variants):
            continue

        retry_eta = _from_iso(entry.get("next_retry_at"))
        if retry_eta is None:
            retry_eta = _retry_eta_from_last_attempt(entry)
        if retry_eta is None or retry_eta > now:
            continue

        next_variant = get_next_variant(vacancy_id)
        if not next_variant:
            entry["completed_reason"] = "pipeline_exhausted"
            entry["next_retry_at"] = ""
            continue

        items.append(
            {
                "id": entry.get("id") or vacancy_id,
                "title": entry.get("title", ""),
                "company": entry.get("company", ""),
                "url": entry.get("url", ""),
                "response_url": entry.get("response_url", ""),
                "snippet": "",
                "source": "hh",
                "source_label": "hh.ru",
                "apply_mode": "auto",
                "_search_profile": "hh_resume_pipeline",
                "_hh_retry": True,
                "_hh_resume_variant": next_variant["name"],
                "_hh_resume_title": next_variant.get("title", ""),
                "_hh_resume_id": next_variant.get("id", ""),
                "_hh_last_status": entry.get("last_status", ""),
            }
        )

    _save()
    return items
