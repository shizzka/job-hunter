"""Telegram client onboarding registry."""
from __future__ import annotations

import os
from datetime import datetime

import config
import runtime_control

STATUS_NEW = "new"
STATUS_ONBOARDING = "onboarding"
STATUS_PENDING_REVIEW = "pending_review"
STATUS_APPROVED = "approved"
STATUS_REJECTED = "rejected"

AUTH_NOT_STARTED = "not_started"
AUTH_PENDING_WEB = "pending_web_auth"
AUTH_READY = "cookies_ready"

VALID_STATUSES = {
    STATUS_NEW,
    STATUS_ONBOARDING,
    STATUS_PENDING_REVIEW,
    STATUS_APPROVED,
    STATUS_REJECTED,
}
VALID_AUTH_STATUSES = {
    AUTH_NOT_STARTED,
    AUTH_PENDING_WEB,
    AUTH_READY,
}


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _default_registry() -> dict:
    return {
        "clients": [],
        "updated_at": _now(),
    }


def _normalize_entry(entry: dict) -> dict | None:
    if not isinstance(entry, dict):
        return None
    try:
        user_id = int(entry.get("user_id") or 0)
    except (TypeError, ValueError):
        return None
    if user_id <= 0:
        return None

    status = str(entry.get("status") or STATUS_NEW).strip()
    if status not in VALID_STATUSES:
        status = STATUS_NEW
    auth_status = str(entry.get("auth_status") or AUTH_NOT_STARTED).strip()
    if auth_status not in VALID_AUTH_STATUSES:
        auth_status = AUTH_NOT_STARTED

    return {
        "user_id": user_id,
        "username": str(entry.get("username") or "").strip(),
        "first_name": str(entry.get("first_name") or "").strip(),
        "last_name": str(entry.get("last_name") or "").strip(),
        "full_name": str(entry.get("full_name") or "").strip(),
        "target_role": str(entry.get("target_role") or "").strip(),
        "target_location": str(entry.get("target_location") or "").strip(),
        "notes": str(entry.get("notes") or "").strip(),
        "status": status,
        "auth_status": auth_status,
        "profile_name": str(entry.get("profile_name") or "").strip(),
        "admin_note": str(entry.get("admin_note") or "").strip(),
        "created_at": str(entry.get("created_at") or _now()),
        "updated_at": str(entry.get("updated_at") or _now()),
        "submitted_at": str(entry.get("submitted_at") or "").strip(),
    }


def _normalize_registry(payload: dict | None) -> dict:
    registry = _default_registry()
    if not isinstance(payload, dict):
        return registry

    seen_user_ids = set()
    raw_clients = payload.get("clients") or []
    if isinstance(raw_clients, list):
        for item in raw_clients:
            normalized = _normalize_entry(item)
            if not normalized or normalized["user_id"] in seen_user_ids:
                continue
            seen_user_ids.add(normalized["user_id"])
            registry["clients"].append(normalized)
    registry["updated_at"] = str(payload.get("updated_at") or registry["updated_at"])
    return registry


def load_registry() -> dict:
    payload = runtime_control.read_json_file(config.TELEGRAM_CLIENTS_FILE)
    if payload is None and not os.path.exists(config.TELEGRAM_CLIENTS_FILE):
        registry = _default_registry()
        save_registry(registry)
        return registry
    registry = _normalize_registry(payload)
    if payload != registry:
        save_registry(registry)
    return registry


def save_registry(registry: dict) -> None:
    normalized = _normalize_registry(registry)
    normalized["updated_at"] = _now()
    runtime_control.write_json_file(config.TELEGRAM_CLIENTS_FILE, normalized)


def list_clients() -> list[dict]:
    clients = list(load_registry()["clients"])
    return sorted(clients, key=lambda item: (item.get("updated_at", ""), item["user_id"]), reverse=True)


def get_client(user_id: int) -> dict | None:
    target_id = int(user_id)
    for item in load_registry()["clients"]:
        if item["user_id"] == target_id:
            return dict(item)
    return None


def upsert_client(user_id: int, **fields: object) -> dict:
    registry = load_registry()
    target_id = int(user_id)
    current = None
    index = -1
    for idx, item in enumerate(registry["clients"]):
        if item["user_id"] == target_id:
            current = item
            index = idx
            break

    base = current or {
        "user_id": target_id,
        "created_at": _now(),
    }
    base.update(fields)
    base["user_id"] = target_id
    base["updated_at"] = _now()
    normalized = _normalize_entry(base)
    if not normalized:
        raise ValueError("Invalid telegram client id")
    if index >= 0:
        registry["clients"][index] = normalized
    else:
        registry["clients"].append(normalized)
    save_registry(registry)
    return normalized


def start_onboarding(user_id: int, *, username: str = "", first_name: str = "", last_name: str = "") -> dict:
    current = get_client(user_id) or {}
    return upsert_client(
        user_id,
        username=username or current.get("username", ""),
        first_name=first_name or current.get("first_name", ""),
        last_name=last_name or current.get("last_name", ""),
        full_name=current.get("full_name", ""),
        target_role=current.get("target_role", ""),
        target_location=current.get("target_location", ""),
        notes=current.get("notes", ""),
        status=STATUS_ONBOARDING if current.get("status") != STATUS_APPROVED else current.get("status", STATUS_APPROVED),
        auth_status=current.get("auth_status", AUTH_NOT_STARTED),
        profile_name=current.get("profile_name", ""),
        admin_note=current.get("admin_note", ""),
        submitted_at=current.get("submitted_at", ""),
    )


def submit_application(
    user_id: int,
    *,
    username: str = "",
    first_name: str = "",
    last_name: str = "",
    full_name: str,
    target_role: str,
    target_location: str = "",
    notes: str = "",
) -> dict:
    current = get_client(user_id) or {}
    return upsert_client(
        user_id,
        username=username or current.get("username", ""),
        first_name=first_name or current.get("first_name", ""),
        last_name=last_name or current.get("last_name", ""),
        full_name=full_name,
        target_role=target_role,
        target_location=target_location,
        notes=notes,
        status=STATUS_PENDING_REVIEW,
        auth_status=current.get("auth_status", AUTH_NOT_STARTED),
        profile_name=current.get("profile_name", ""),
        admin_note=current.get("admin_note", ""),
        submitted_at=_now(),
    )


def set_status(
    user_id: int,
    *,
    status: str,
    auth_status: str | None = None,
    profile_name: str | None = None,
    admin_note: str | None = None,
) -> dict:
    current = get_client(user_id)
    if not current:
        raise KeyError(f"Client not found: {user_id}")
    payload: dict[str, object] = {"status": status}
    if auth_status is not None:
        payload["auth_status"] = auth_status
    if profile_name is not None:
        payload["profile_name"] = profile_name
    if admin_note is not None:
        payload["admin_note"] = admin_note
    return upsert_client(user_id, **payload)
