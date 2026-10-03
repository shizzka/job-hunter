"""Usage and soft-limit registry for Telegram AI resume analysis."""
from __future__ import annotations

from datetime import datetime

import config
from state_store.registry import RegistryStore

MAX_EVENTS = 100


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _default_user(user_id: int) -> dict:
    return {
        "user_id": int(user_id),
        "free_total": max(0, int(config.TELEGRAM_AI_FREE_ANALYSES or 0)),
        "free_used": 0,
        "bonus_total": 0,
        "profiles": {},
        "updated_at": _now(),
    }


def _normalize_profile_bucket(bucket: dict | None) -> dict:
    if not isinstance(bucket, dict):
        bucket = {}
    try:
        analysis_count = max(0, int(bucket.get("analysis_count") or 0))
    except (TypeError, ValueError):
        analysis_count = 0
    return {
        "analysis_count": analysis_count,
        "last_used_at": str(bucket.get("last_used_at") or "").strip(),
    }


def _normalize_user(entry: dict | None) -> dict | None:
    if not isinstance(entry, dict):
        return None
    try:
        user_id = int(entry.get("user_id") or 0)
    except (TypeError, ValueError):
        return None
    if user_id <= 0:
        return None

    try:
        free_total = max(0, int(entry.get("free_total", config.TELEGRAM_AI_FREE_ANALYSES)))
    except (TypeError, ValueError):
        free_total = max(0, int(config.TELEGRAM_AI_FREE_ANALYSES or 0))
    try:
        free_used = max(0, int(entry.get("free_used") or 0))
    except (TypeError, ValueError):
        free_used = 0
    try:
        bonus_total = max(0, int(entry.get("bonus_total") or 0))
    except (TypeError, ValueError):
        bonus_total = 0

    profiles: dict[str, dict] = {}
    raw_profiles = entry.get("profiles") or {}
    if isinstance(raw_profiles, dict):
        for profile_name, bucket in raw_profiles.items():
            name = str(profile_name or "").strip()
            if not name:
                continue
            profiles[name] = _normalize_profile_bucket(bucket)

    return {
        "user_id": user_id,
        "free_total": free_total,
        "free_used": free_used,
        "bonus_total": bonus_total,
        "profiles": profiles,
        "updated_at": str(entry.get("updated_at") or _now()),
    }


def _normalize_event(event: dict | None) -> dict | None:
    if not isinstance(event, dict):
        return None
    action = str(event.get("action") or "").strip()
    if not action:
        return None
    try:
        user_id = int(event.get("user_id") or 0)
    except (TypeError, ValueError):
        user_id = 0
    try:
        actor_user_id = int(event.get("actor_user_id") or 0)
    except (TypeError, ValueError):
        actor_user_id = 0
    try:
        amount = int(event.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0
    return {
        "action": action,
        "user_id": user_id,
        "actor_user_id": actor_user_id,
        "profile_name": str(event.get("profile_name") or "").strip(),
        "amount": amount,
        "created_at": str(event.get("created_at") or _now()),
    }


def _normalize_registry(payload: dict | None) -> dict:
    registry = {
        "users": [],
        "events": [],
        "updated_at": _now(),
    }
    if not isinstance(payload, dict):
        return registry

    seen_user_ids = set()
    raw_users = payload.get("users") or []
    if isinstance(raw_users, list):
        for item in raw_users:
            normalized = _normalize_user(item)
            if not normalized or normalized["user_id"] in seen_user_ids:
                continue
            seen_user_ids.add(normalized["user_id"])
            registry["users"].append(normalized)

    raw_events = payload.get("events") or []
    if isinstance(raw_events, list):
        for item in raw_events[-MAX_EVENTS:]:
            normalized = _normalize_event(item)
            if normalized:
                registry["events"].append(normalized)

    registry["updated_at"] = str(payload.get("updated_at") or registry["updated_at"])
    return registry


def _store() -> RegistryStore:
    return RegistryStore(config.TELEGRAM_AI_LIMITS_FILE, normalize=_normalize_registry, collections=("users", "events"))


def load_registry() -> dict:
    return _store().load()


def save_registry(registry: dict) -> None:
    normalized = _normalize_registry(registry)
    normalized["updated_at"] = _now()
    _store().save(normalized)


def _ensure_user_entry(registry: dict, user_id: int) -> dict:
    target_id = int(user_id)
    for item in registry["users"]:
        if item["user_id"] == target_id:
            return item
    entry = _default_user(target_id)
    registry["users"].append(entry)
    return entry


def _append_event(registry: dict, *, action: str, user_id: int, actor_user_id: int = 0, profile_name: str = "", amount: int = 0) -> None:
    registry["events"].append({
        "action": action,
        "user_id": int(user_id),
        "actor_user_id": int(actor_user_id or 0),
        "profile_name": str(profile_name or "").strip(),
        "amount": int(amount or 0),
        "created_at": _now(),
    })
    registry["events"] = registry["events"][-MAX_EVENTS:]


def _snapshot(entry: dict) -> dict:
    profiles = entry.get("profiles") or {}
    analysis_total = sum((bucket or {}).get("analysis_count", 0) for bucket in profiles.values())
    available_soft = max(0, int(entry.get("free_total", 0)) + int(entry.get("bonus_total", 0)) - int(entry.get("free_used", 0)))
    return {
        "user_id": int(entry["user_id"]),
        "free_total": int(entry.get("free_total", 0)),
        "free_used": int(entry.get("free_used", 0)),
        "bonus_total": int(entry.get("bonus_total", 0)),
        "available_soft": available_soft,
        "analysis_total": analysis_total,
        "profiles": profiles,
        "updated_at": str(entry.get("updated_at") or ""),
    }


def get_user_snapshot(user_id: int) -> dict:
    return _update_user(user_id, lambda registry, entry: None)


def list_user_snapshots(user_ids: list[int] | None = None) -> list[dict]:
    if user_ids is None:
        return [_snapshot(item) for item in load_registry()["users"]]
    snapshots = []

    def mutate(registry):
        for user_id in user_ids:
            snapshots.append(_snapshot(_ensure_user_entry(registry, int(user_id))))
        registry["updated_at"] = _now()

    _store().update(mutate)
    return snapshots


def recent_events(limit: int = 10) -> list[dict]:
    registry = load_registry()
    limit = max(0, int(limit))
    if limit == 0:
        return []
    return list(reversed(registry["events"][-limit:]))


def _update_user(user_id: int, mutator) -> dict:
    snapshot = None

    def mutate(registry):
        nonlocal snapshot
        entry = _ensure_user_entry(registry, user_id)
        mutator(registry, entry)
        registry["updated_at"] = _now()
        snapshot = _snapshot(entry)

    _store().update(mutate)
    return snapshot


def record_resume_analysis(user_id: int, *, profile_name: str) -> dict:
    def mutate(registry, entry):
        entry["free_used"] = int(entry.get("free_used", 0)) + 1
        profile_key = str(profile_name or "").strip() or "default"
        profile_bucket = entry.setdefault("profiles", {}).setdefault(profile_key, _normalize_profile_bucket({}))
        profile_bucket["analysis_count"] = int(profile_bucket.get("analysis_count", 0)) + 1
        profile_bucket["last_used_at"] = _now()
        entry["updated_at"] = _now()
        _append_event(registry, action="analysis_used", user_id=user_id, profile_name=profile_key, amount=1)

    return _update_user(user_id, mutate)


def grant_bonus(user_id: int, *, amount: int = 1, actor_user_id: int = 0) -> dict:
    grant = max(0, int(amount))

    def mutate(registry, entry):
        entry["bonus_total"] = int(entry.get("bonus_total", 0)) + grant
        entry["updated_at"] = _now()
        _append_event(registry, action="grant_bonus", user_id=user_id, actor_user_id=actor_user_id, amount=grant)

    return _update_user(user_id, mutate)


def reset_free_limit(user_id: int, *, actor_user_id: int = 0, free_total: int | None = None) -> dict:
    def mutate(registry, entry):
        if free_total is None:
            entry["free_total"] = max(0, int(config.TELEGRAM_AI_FREE_ANALYSES or 0))
        else:
            entry["free_total"] = max(0, int(free_total))
        entry["free_used"] = 0
        entry["updated_at"] = _now()
        _append_event(
            registry,
            action="reset_free_limit",
            user_id=user_id,
            actor_user_id=actor_user_id,
            amount=int(entry["free_total"]),
        )

    return _update_user(user_id, mutate)
