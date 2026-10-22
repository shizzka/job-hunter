"""Instance and request boundaries shared by profile data access and workers."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache
import os
from pathlib import Path
import re

import config

_scope = ContextVar("profile_access_scope", default=None)


@lru_cache(maxsize=16)
def _instance_kind(value):
    if value in {"dev", "prod"}:
        return value
    raise PermissionError("Укажите JOB_HUNTER_INSTANCE_KIND: dev или prod. Доступ к профилям запрещён.")


def instance_kind():
    return _instance_kind(str(config.JOB_HUNTER_INSTANCE_KIND or "").strip().lower())


def current_scope():
    return _scope.get() or (
        os.getenv("JOB_HUNTER_SCOPED_PROFILE", ""),
        os.getenv("JOB_HUNTER_SCOPED_DIRECTION", ""),
        os.getenv("JOB_HUNTER_SCOPED_USER", ""),
    )


@contextmanager
def principal_scope(principal):
    if principal and principal.get("role") == "tester":
        profile, direction = tester_binding(principal)
        token = _scope.set((profile, direction, str(principal["user_id"])))
        try:
            require_tester_binding(principal)
            yield
        finally:
            _scope.reset(token)
    else:
        token = _scope.set(None)
        try:
            yield
        finally:
            _scope.reset(token)


def tester_binding(principal):
    """Validate registry identity without filtering other instances' entries."""
    profile, direction = principal.get("profile"), principal.get("direction")
    if (not isinstance(profile, str) or re.fullmatch(r"[A-Za-z0-9_-]+", profile) is None
            or not profile.endswith("-dev")
            or not isinstance(direction, str) or re.fullmatch(r"[A-Za-z0-9_-]{1,64}", direction) is None):
        raise PermissionError("Некорректная привязка tester.")
    return profile, direction


def require_tester_binding(principal):
    """Check the bound direction exists, independently of the active selection."""
    import directions
    import profile as profile_mod

    profile, direction = tester_binding(principal)
    require_profile(profile)
    try:
        env_path = Path(profile_mod.profile_env_path(profile))
        if not env_path.is_file():
            raise FileNotFoundError(env_path)
        directions.load_direction(env_path.parent, direction)
    except (OSError, ValueError) as exc:
        raise PermissionError("Профиль tester или привязанное направление не подготовлены.") from exc
    return profile, direction


def profile_allowed(name):
    if not isinstance(name, str) or re.fullmatch(r"[A-Za-z0-9_-]+", name) is None:
        return False
    scoped_profile, _, _ = current_scope()
    try:
        kind = instance_kind()
    except PermissionError:
        return False
    return (name.endswith("-dev") == (kind == "dev")
            and (not scoped_profile or name == scoped_profile))


def require_profile(name):
    if not profile_allowed(name):
        raise PermissionError("Профиль недоступен для этого бота или пользователя.")


def require_direction(name):
    _, direction, _ = current_scope()
    if direction and name != direction:
        raise PermissionError("Tester доступно только привязанное направление; переключение запрещено.")


def worker_environment():
    env = os.environ.copy()
    env["JOB_HUNTER_INSTANCE_KIND"] = instance_kind()
    for key, value in zip(("JOB_HUNTER_SCOPED_PROFILE", "JOB_HUNTER_SCOPED_DIRECTION",
                           "JOB_HUNTER_SCOPED_USER"), current_scope()):
        env[key] = value
    return env
