"""Access registry for the standalone Telegram control bot."""
from __future__ import annotations

from datetime import datetime

import config
from state_store.registry import RegistryStore

ROLE_ADMIN = "admin"
ROLE_USER = "user"


def _default_registry() -> dict:
    users = []
    owner_id = int(config.NOTIFY_CHAT_ID or 0)
    if owner_id > 0:
        users.append({
            "user_id": owner_id,
            "role": ROLE_ADMIN,
            "profile": "default",
            "enabled": True,
            "label": "owner",
        })
    return {
        "users": users,
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }


def _normalize_entry(entry: dict) -> dict | None:
    try:
        user_id = int(entry.get("user_id") or 0)
    except (TypeError, ValueError):
        return None
    if user_id <= 0:
        return None

    role = str(entry.get("role") or ROLE_USER).strip().casefold()
    if role not in {ROLE_ADMIN, ROLE_USER}:
        role = ROLE_USER

    profile = str(entry.get("profile") or "default").strip() or "default"
    enabled = bool(entry.get("enabled", True))
    label = str(entry.get("label") or "").strip()
    return {
        "user_id": user_id,
        "role": role,
        "profile": profile,
        "enabled": enabled,
        "label": label,
    }


def _normalize_registry(payload: dict | None) -> dict:
    result = {
        "users": [],
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    if isinstance(payload, dict):
        result["updated_at"] = str(payload.get("updated_at") or result["updated_at"])
        raw_users = payload.get("users") or []
        if isinstance(raw_users, list):
            seen = set()
            for item in raw_users:
                if not isinstance(item, dict):
                    continue
                normalized = _normalize_entry(item)
                if not normalized:
                    continue
                if normalized["user_id"] in seen:
                    continue
                seen.add(normalized["user_id"])
                result["users"].append(normalized)

    owner_id = int(config.NOTIFY_CHAT_ID or 0)
    if owner_id > 0 and not any(item["user_id"] == owner_id for item in result["users"]):
        result["users"].insert(0, {
            "user_id": owner_id,
            "role": ROLE_ADMIN,
            "profile": "default",
            "enabled": True,
            "label": "owner",
        })
    return result


def _store() -> RegistryStore:
    return RegistryStore(config.TELEGRAM_ACCESS_FILE, normalize=_normalize_registry, collections=("users",))


def load_registry() -> dict:
    return _store().load()


def save_registry(registry: dict) -> None:
    registry = _normalize_registry(registry)
    registry["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _store().save(registry)


def resolve_user(user_id: int) -> dict | None:
    registry = load_registry()
    for item in registry["users"]:
        if item["user_id"] == int(user_id) and item.get("enabled", True):
            return dict(item)
    return None


def list_users() -> list[dict]:
    return list(load_registry()["users"])


def upsert_user(user_id: int, *, profile: str, role: str = ROLE_USER, label: str = "", enabled: bool = True) -> dict:
    normalized = _normalize_entry({
        "user_id": user_id,
        "profile": profile,
        "role": role,
        "label": label,
        "enabled": enabled,
    })
    if not normalized:
        raise ValueError("Invalid telegram user id")

    def mutate(registry):
        for idx, item in enumerate(registry["users"]):
            if item["user_id"] == normalized["user_id"]:
                registry["users"][idx] = normalized
                break
        else:
            registry["users"].append(normalized)
        registry["updated_at"] = datetime.now().isoformat(timespec="seconds")

    _store().update(mutate)
    return normalized


def remove_user(user_id: int) -> bool:
    removed = False

    def mutate(registry):
        nonlocal removed
        before = len(registry["users"])
        registry["users"] = [item for item in registry["users"] if item["user_id"] != int(user_id)]
        removed = len(registry["users"]) != before
        if removed:
            registry["updated_at"] = datetime.now().isoformat(timespec="seconds")

    _store().update(mutate)
    return removed
