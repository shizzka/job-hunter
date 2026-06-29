"""Стадированный pipeline резюме для hh.ru: normal -> fun -> ats-heavy."""
from __future__ import annotations

import json
import os
from datetime import datetime, timedelta

import config
from outcome import STATUS_PENDING, STATUS_POSITIVE, STATUS_REJECTED, status_bucket as _status_bucket


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


def _config_int(name: str, default: int) -> int:
    try:
        return int(getattr(config, name, default) or default)
    except (TypeError, ValueError):
        return default


_RETRY_ALLOWED_TITLE_MARKERS = (
    "qa",
    "тест",
    "test",
    "quality assurance",
    "manual qa",
    "sdet",
)

_RETRY_BLOCKED_TITLE_MARKERS = (
    "техническая поддерж",
    "технической поддерж",
    "техподдерж",
    "поддержк",
    "support",
    "helpdesk",
    "сервисный инженер",
    "сервис-инженер",
    "сервисн",
    "техник",
    "рэа",
    "радиоэлектрон",
    "разработчик",
    "developer",
    "devops",
    "программист",
    "администратор",
    "оператор",
)


def _normal_text(value: str) -> str:
    return (value or "").casefold().replace("\xa0", " ").replace("ё", "е")


def retry_role_reject_reason(title: str) -> str | None:
    """Strict role filter for staged resume retries.

    Retry uses historic vacancies that were already applied to. Some old applies were
    intentionally broad, but sending another resume should stay QA/test-only.
    """
    title_norm = _normal_text(title)
    if not title_norm.strip():
        return "retry_empty_title"

    has_blocked_marker = any(marker in title_norm for marker in _RETRY_BLOCKED_TITLE_MARKERS)
    if has_blocked_marker:
        return "retry_blocked_role_title"

    has_allowed_marker = any(marker in title_norm for marker in _RETRY_ALLOWED_TITLE_MARKERS)
    if not has_allowed_marker:
        return "retry_not_qa_title"

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
            "retry_reason": "",
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
    entry["retry_reason"] = ""
    entry["completed_reason"] = ""
    _save()


def mark_terminal(vacancy_id: str, reason: str) -> None:
    entry = _entry(vacancy_id)
    if not entry:
        return
    entry["completed_reason"] = reason
    entry["next_retry_at"] = ""
    entry["retry_reason"] = ""
    _save()


def _retry_eta_from_last_attempt(entry: dict, delay_hours: int | None = None) -> datetime | None:
    attempts = entry.get("attempts") or []
    if not attempts:
        return None
    last_attempt_at = _from_iso(attempts[-1].get("applied_at"))
    if not last_attempt_at:
        return None
    if delay_hours is None:
        delay_hours = _config_int("HH_RESUME_RETRY_DELAY_HOURS", 24)
    return last_attempt_at + timedelta(hours=delay_hours)


def _retry_delay_for_bucket(bucket: str) -> int | None:
    if bucket == STATUS_REJECTED:
        return _config_int("HH_RESUME_RETRY_DELAY_HOURS", 24)
    if bucket == STATUS_PENDING and getattr(config, "HH_RESUME_RETRY_ON_SILENCE", False):
        return _config_int("HH_RESUME_SILENCE_RETRY_DELAY_HOURS", 72)
    return None


def _retry_reason_for_bucket(bucket: str) -> str:
    if bucket == STATUS_REJECTED:
        return "rejected"
    if bucket == STATUS_PENDING:
        return "silence"
    return ""


def _set_retry_state(entry: dict, vacancy_id: str, bucket: str) -> None:
    delay_hours = _retry_delay_for_bucket(bucket)
    if delay_hours is None:
        entry["next_retry_at"] = ""
        entry["retry_reason"] = ""
        return

    if get_next_variant(vacancy_id) is None:
        entry["completed_reason"] = "pipeline_exhausted"
        entry["next_retry_at"] = ""
        entry["retry_reason"] = ""
        return

    eta = _retry_eta_from_last_attempt(entry, delay_hours)
    entry["next_retry_at"] = _to_iso(eta)
    entry["retry_reason"] = _retry_reason_for_bucket(bucket)


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
        if bucket == STATUS_POSITIVE:
            entry["completed_reason"] = "positive_response"
            entry["next_retry_at"] = ""
            entry["retry_reason"] = ""
            continue

        if bucket in {STATUS_REJECTED, STATUS_PENDING}:
            _set_retry_state(entry, vacancy_id, bucket)
            continue

        entry["next_retry_at"] = ""
        entry["retry_reason"] = ""
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
        bucket = _status_bucket(entry.get("last_status", ""))
        delay_hours = _retry_delay_for_bucket(bucket)
        if delay_hours is None:
            entry["next_retry_at"] = ""
            entry["retry_reason"] = ""
            continue

        retry_eta = _from_iso(entry.get("next_retry_at"))
        if retry_eta is None:
            retry_eta = _retry_eta_from_last_attempt(entry, delay_hours)
        if retry_eta is None or retry_eta > now:
            continue

        next_variant = get_next_variant(vacancy_id)
        if not next_variant:
            entry["completed_reason"] = "pipeline_exhausted"
            entry["next_retry_at"] = ""
            continue

        role_reject_reason = retry_role_reject_reason(entry.get("title", ""))
        if role_reject_reason:
            entry["completed_reason"] = "retry_filtered_role"
            entry["next_retry_at"] = ""
            entry["retry_reason"] = ""
            entry["retry_filtered_reason"] = role_reject_reason
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
                "_hh_retry_reason": entry.get("retry_reason") or _retry_reason_for_bucket(bucket),
                "_hh_retry_after": _to_iso(retry_eta),
            }
        )

    _save()
    # Сначала пробуем свежих молчунов: старые pending чаще уже закрыты/архивны.
    items.sort(key=lambda item: item.get("_hh_retry_after") or "", reverse=True)
    items.sort(key=lambda item: 0 if item.get("_hh_retry_reason") == "silence" else 1)
    limit = max(0, _config_int("HH_RESUME_RETRY_MAX_CANDIDATES_PER_RUN", 5))
    if limit:
        return items[:limit]
    return items
