"""Access registry for the standalone Telegram control bot."""
from __future__ import annotations

import json
import os
from datetime import datetime
from functools import wraps
import inspect

import config
import profile_access
from state_store.registry import RegistryStore

ROLE_ADMIN = "admin"
ROLE_USER = "user"
ROLE_TESTER = "tester"


def scoped_request(fn):
    """Bind all data access (including spawned tasks) to a Telegram principal."""
    signature = inspect.signature(fn)

    @wraps(fn)
    async def wrapped(self, *args, **kwargs):
        values = signature.bind(self, *args, **kwargs).arguments
        callback = values.get("callback") or (values.get("update") or {}).get("callback_query") or {}
        message = callback.get("message") or (values.get("update") or {}).get("message") or {}
        chat_id = values.get("chat_id") or (message.get("chat") or {}).get("id") or 0
        try:
            principal = values.get("principal")
            if principal is None:
                sender = callback.get("from") or message.get("from") or {}
                principal = resolve_user(int(sender.get("id") or 0))
            with profile_access.principal_scope(principal):
                if principal and principal.get("role") == ROLE_TESTER:
                    import profile as profile_mod
                    # Missing/changed bound selection blocks even commands which
                    # only inspect queues or cancel a running profile command.
                    try:
                        profile_mod.load_profile(principal["profile"])
                    except (OSError, ValueError) as exc:
                        raise PermissionError("Профиль tester или привязанное направление не подготовлены.") from exc
                return await fn(self, *args, **kwargs)
        except PermissionError as exc:
            if callback:
                await self._answer_callback_query(str(callback.get("id") or ""), f"🔒 {exc}", show_alert=True)
            elif chat_id:
                await self._send_text(int(chat_id), f"🔒 {exc}")
            return False

    return wrapped


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
    if role not in {ROLE_ADMIN, ROLE_USER, ROLE_TESTER}:
        role = ROLE_USER

    profile = str(entry.get("profile") or "default").strip() or "default"
    direction = str(entry.get("direction") or "").strip()
    if role == ROLE_TESTER:
        try:
            profile_access.tester_binding({"profile": profile, "direction": direction})
        except PermissionError:
            return None
    enabled = bool(entry.get("enabled", True))
    label = str(entry.get("label") or "").strip()
    result = {
        "user_id": user_id,
        "role": role,
        "profile": profile,
        "enabled": enabled,
        "label": label,
    }
    if role == ROLE_TESTER:
        result["direction"] = direction
    return result


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


def impersonation_path() -> str:
    """Dev-only «смотреть как клиент»: enabled only when JOB_HUNTER_IMPERSONATION_FILE is set."""
    return os.getenv("JOB_HUNTER_IMPERSONATION_FILE", "").strip()


def _load_impersonation() -> dict:
    path = impersonation_path()
    if not path:
        return {}
    try:
        with open(path, encoding="utf-8") as stream:
            data = json.load(stream)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def set_impersonation(admin_user_id: int, profile: str | None) -> None:
    path = impersonation_path()
    if not path:
        raise RuntimeError("Режим «смотреть как» доступен только в dev-боте.")
    if profile:
        profile_access.require_profile(profile)
    data = _load_impersonation()
    if profile:
        data[str(int(admin_user_id))] = {"profile": str(profile)}
    else:
        data.pop(str(int(admin_user_id)), None)
    temp = path + ".tmp"
    with open(temp, "w", encoding="utf-8") as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2)
    os.replace(temp, path)


def resolve_user(user_id: int) -> dict | None:
    registry = load_registry()
    for item in registry["users"]:
        if item["user_id"] == int(user_id) and item.get("enabled", True):
            result = dict(item)
            acting = _load_impersonation().get(str(int(user_id))) if result.get("role") == ROLE_ADMIN else None
            if acting and acting.get("profile"):
                # The admin sees the bot exactly as a client of that profile (dev bot only).
                result.update(role=ROLE_USER, profile=acting["profile"], impersonating=True)
            if result.get("role") != ROLE_ADMIN:
                profile_access.require_profile(result["profile"])
            if result.get("role") == ROLE_TESTER:
                profile_access.require_tester_binding(result)
            return result
    return None


def list_users() -> list[dict]:
    scoped_user = profile_access.current_scope()[2]
    users = []
    for item in load_registry()["users"]:
        if scoped_user and str(item["user_id"]) != scoped_user:
            continue
        if not scoped_user and item["role"] == ROLE_ADMIN and item["profile"] == "default":
            if profile_access.instance_kind() == "dev":
                import profile as profile_mod
                names = profile_mod.list_profiles()
                if not names:
                    continue
                # 'default' is the shared owner routing sentinel, not a DEV
                # profile. Present its effective binding without changing disk.
                item = {**item, "profile": names[0]}
        elif not profile_access.profile_allowed(item["profile"]):
            continue
        users.append(dict(item))
    return users


def require_user_access(user_id: int) -> None:
    scoped_user = profile_access.current_scope()[2]
    if scoped_user and scoped_user != str(int(user_id)):
        raise PermissionError("Пользователь недоступен.")
    for item in load_registry()["users"]:
        if item["user_id"] == int(user_id):
            if item["role"] == ROLE_ADMIN and item["profile"] == "default" and not scoped_user:
                return
            profile_access.require_profile(item["profile"])
            return


def notification_allowed(user_id: int, *, profile_name: str | None, direction: str | None) -> bool:
    """Authorize every recipient against the event profile and instance."""
    if profile_name is None:
        import profile as profile_mod
        try:
            profile_name = profile_mod.active().name
        except (OSError, ValueError, PermissionError):
            return False
    if not profile_access.profile_allowed(profile_name):
        return False
    for item in load_registry()["users"]:
        if item["user_id"] == int(user_id):
            if not item.get("enabled", True) or item["profile"] != profile_name:
                return False
            if item.get("role") != ROLE_TESTER:
                return True
            try:
                profile_access.require_tester_binding(item)
            except PermissionError:
                return False
            return direction == item["direction"]
    return False


def command_reply_allowed(user_id: int, *, profile_name: str, direction: str | None) -> bool:
    """Recheck an initiating actor's authority over a captured command target."""
    try:
        # A queue drainer can inherit a different actor's request scope.
        with profile_access.principal_scope(None):
            principal = resolve_user(user_id)
            if not principal:
                return False
            with profile_access.principal_scope(principal):
                if not profile_access.profile_allowed(profile_name):
                    return False
                if principal.get("role") == ROLE_ADMIN:
                    return True
                if principal.get("profile") != profile_name:
                    return False
                if principal.get("role") == ROLE_TESTER:
                    return direction == principal.get("direction")
                return True
    except PermissionError:
        return False


def owner_notification_allowed(user_id: int) -> bool:
    """A captured global owner route cannot be rebound to a client or tester."""
    for item in load_registry()["users"]:
        if item["user_id"] == int(user_id):
            return bool(item.get("enabled", True) and item.get("role") == ROLE_ADMIN)
    return True


def upsert_user(user_id: int, *, profile: str, role: str = ROLE_USER, label: str = "", enabled: bool = True,
                direction: str = "") -> dict:
    profile_access.require_profile(profile)
    normalized = _normalize_entry({
        "user_id": user_id,
        "profile": profile,
        "role": role,
        "label": label,
        "enabled": enabled,
        "direction": direction,
    })
    if not normalized:
        raise ValueError("Invalid telegram user id or tester binding (*-dev profile and stable direction name required)")
    if normalized["role"] == ROLE_TESTER:
        profile_access.require_tester_binding(normalized)

    def mutate(registry):
        for idx, item in enumerate(registry["users"]):
            if item["user_id"] == normalized["user_id"]:
                profile_access.require_profile(item["profile"])
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
        for item in registry["users"]:
            if item["user_id"] == int(user_id):
                profile_access.require_profile(item["profile"])
        before = len(registry["users"])
        registry["users"] = [item for item in registry["users"] if item["user_id"] != int(user_id)]
        removed = len(registry["users"]) != before
        if removed:
            registry["updated_at"] = datetime.now().isoformat(timespec="seconds")

    _store().update(mutate)
    return removed
