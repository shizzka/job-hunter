"""IPC bridge for hh.ru login codes between auth capture and Telegram bot.

Flow:
1. client_hh_auth.py detects a login/code prompt in the HH browser page.
2. It creates a profile-specific pending file and sends a Telegram prompt.
3. telegram_bot.py accepts the user's reply and writes a profile-specific response.
4. client_hh_auth.py polls the response file, fills the browser form, and saves cookies.
"""
from __future__ import annotations

import contextlib
import glob
import hashlib
import json
import logging
import os
import re
import time
import uuid
from typing import Optional

import config

log = logging.getLogger("hh_auth_bridge")


def _state_dir() -> str:
    # The bot normally runs in the default profile while the browser process
    # activates the client profile. This IPC directory must stay shared.
    state_dir = getattr(config, "HH_AUTH_BRIDGE_DIR", "") or getattr(config, "HH_STATE_DIR", "")
    state_dir = state_dir or os.path.expanduser("~/.job-hunter/state")
    os.makedirs(state_dir, exist_ok=True)
    return state_dir


def _normalized_profile_name(profile_name: str | None) -> str:
    return (profile_name or "default").strip() or "default"


def _profile_file_key(profile_name: str | None) -> str:
    name = _normalized_profile_name(profile_name)
    if re.fullmatch(r"[A-Za-z0-9_-]+", name):
        return name
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:24]


def _pending_path(profile_name: str | None = None) -> str:
    key = _profile_file_key(profile_name)
    return os.path.join(_state_dir(), f"hh_auth_pending.{key}.json")


def _response_path(profile_name: str | None = None) -> str:
    key = _profile_file_key(profile_name)
    return os.path.join(_state_dir(), f"hh_auth_response.{key}.json")


def _pending_paths() -> list[str]:
    return sorted(glob.glob(os.path.join(_state_dir(), "hh_auth_pending.*.json")))


def _atomic_write(path: str, data: dict) -> None:
    tmp = f"{path}.tmp.{os.getpid()}.{uuid.uuid4().hex}"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    os.chmod(path, 0o600)


def _safe_read(path: str) -> Optional[dict]:
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception as exc:
        log.warning("read %s failed: %s", path, exc)
    return None


def create_request(
    *,
    kind: str,
    profile_name: str,
    prompt: str,
    page_url: str = "",
    timeout_s: int = 900,
) -> str:
    request_id = uuid.uuid4().hex[:8]
    started_at = time.time()
    request_profile = _normalized_profile_name(profile_name)
    data = {
        "id": request_id,
        "kind": (kind or "code").strip() or "code",
        "profile_name": request_profile,
        "prompt": (prompt or "").strip(),
        "page_url": (page_url or "").strip(),
        "started_at": started_at,
        "timeout_at": started_at + max(1, int(timeout_s or 1)),
    }
    _atomic_write(_pending_path(request_profile), data)
    with contextlib.suppress(Exception):
        response_path = _response_path(request_profile)
        if os.path.exists(response_path):
            os.remove(response_path)
    log.info("hh auth request created: id=%s kind=%s profile=%s", request_id, data["kind"], data["profile_name"])
    return request_id


def _find_pending_request(request_id: str) -> Optional[dict]:
    for path in _pending_paths():
        data = _safe_read(path)
        if data and data.get("id") == request_id:
            return data
    return None


async def wait_for_response(
    request_id: str,
    timeout_s: int = 900,
    poll_interval_s: float = 2.0,
    profile_name: str = "",
) -> Optional[str]:
    import asyncio

    deadline = time.time() + max(1, int(timeout_s or 1))
    request_profile = _normalized_profile_name(profile_name) if profile_name else ""
    while time.time() < deadline:
        if not request_profile:
            pending = _find_pending_request(request_id)
            request_profile = str((pending or {}).get("profile_name") or "")
        data = _safe_read(_response_path(request_profile)) if request_profile else None
        if data and data.get("id") == request_id:
            answer = str(data.get("answer") or "").strip()
            if answer:
                log.info("hh auth response received: id=%s kind=%s", request_id, data.get("kind"))
                return answer
        await asyncio.sleep(max(0.2, float(poll_interval_s or 2.0)))
    log.warning("hh auth response wait timed out: id=%s", request_id)
    return None


def complete_request(request_id: str, profile_name: str = "") -> None:
    request_profile = _normalized_profile_name(profile_name) if profile_name else ""
    if not request_profile:
        pending = _find_pending_request(request_id)
        request_profile = str((pending or {}).get("profile_name") or "")
    if not request_profile:
        return
    for path in (_pending_path(request_profile), _response_path(request_profile)):
        with contextlib.suppress(Exception):
            if not os.path.exists(path):
                continue
            data = _safe_read(path)
            if data and data.get("id") and data.get("id") != request_id:
                continue
            os.remove(path)
    log.info("hh auth request completed: id=%s", request_id)


def peek_pending(profile_name: str | None = None) -> Optional[dict]:
    paths = [_pending_path(profile_name)] if profile_name else _pending_paths()
    pending_items = []
    for path in paths:
        data = _safe_read(path)
        if not data:
            continue
        request_profile = _normalized_profile_name(data.get("profile_name"))
        timeout_at = float(data.get("timeout_at") or 0)
        if timeout_at and timeout_at < time.time():
            with contextlib.suppress(Exception):
                os.remove(path)
            continue
        resp = _safe_read(_response_path(request_profile))
        if resp and resp.get("id") == data.get("id"):
            continue
        pending_items.append(data)
    if not pending_items:
        return None
    return min(pending_items, key=lambda item: float(item.get("started_at") or 0))


def write_response(request_id: str, answer: str, profile_name: str = "") -> None:
    request_profile = _normalized_profile_name(profile_name) if profile_name else ""
    pending = (
        _safe_read(_pending_path(request_profile))
        if request_profile
        else _find_pending_request(request_id)
    ) or {}
    if pending.get("id") != request_id:
        raise ValueError(f"unknown HH auth request: {request_id}")
    request_profile = _normalized_profile_name(pending.get("profile_name"))
    _atomic_write(_response_path(request_profile), {
        "id": request_id,
        "kind": pending.get("kind") or "",
        "profile_name": pending.get("profile_name") or "",
        "answer": (answer or "").strip(),
        "received_at": time.time(),
    })
    log.info("hh auth response written: id=%s kind=%s", request_id, pending.get("kind"))
