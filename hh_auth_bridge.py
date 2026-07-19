"""IPC bridge for hh.ru login codes between auth capture and Telegram bot.

Flow:
1. client_hh_auth.py detects a login/code prompt in the HH browser page.
2. It creates hh_auth_pending.json and sends a Telegram prompt.
3. telegram_bot.py accepts the user's reply and writes hh_auth_response.json.
4. client_hh_auth.py polls the response file, fills the browser form, and saves cookies.
"""
from __future__ import annotations

import contextlib
import json
import logging
import os
import time
import uuid
from typing import Optional

import config

log = logging.getLogger("hh_auth_bridge")


def _state_dir() -> str:
    state_dir = getattr(config, "HH_STATE_DIR", "") or os.path.expanduser("~/.job-hunter/state")
    os.makedirs(state_dir, exist_ok=True)
    return state_dir


def _pending_path() -> str:
    return os.path.join(_state_dir(), "hh_auth_pending.json")


def _response_path() -> str:
    return os.path.join(_state_dir(), "hh_auth_response.json")


def _atomic_write(path: str, data: dict) -> None:
    tmp = f"{path}.tmp.{os.getpid()}"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


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
    data = {
        "id": request_id,
        "kind": (kind or "code").strip() or "code",
        "profile_name": (profile_name or "default").strip() or "default",
        "prompt": (prompt or "").strip(),
        "page_url": (page_url or "").strip(),
        "started_at": started_at,
        "timeout_at": started_at + max(1, int(timeout_s or 1)),
    }
    _atomic_write(_pending_path(), data)
    with contextlib.suppress(Exception):
        if os.path.exists(_response_path()):
            os.remove(_response_path())
    log.info("hh auth request created: id=%s kind=%s profile=%s", request_id, data["kind"], data["profile_name"])
    return request_id


async def wait_for_response(request_id: str, timeout_s: int = 900, poll_interval_s: float = 2.0) -> Optional[str]:
    import asyncio

    deadline = time.time() + max(1, int(timeout_s or 1))
    while time.time() < deadline:
        data = _safe_read(_response_path())
        if data and data.get("id") == request_id:
            answer = str(data.get("answer") or "").strip()
            if answer:
                log.info("hh auth response received: id=%s kind=%s", request_id, data.get("kind"))
                return answer
        await asyncio.sleep(max(0.2, float(poll_interval_s or 2.0)))
    log.warning("hh auth response wait timed out: id=%s", request_id)
    return None


def complete_request(request_id: str) -> None:
    for path in (_pending_path(), _response_path()):
        with contextlib.suppress(Exception):
            if not os.path.exists(path):
                continue
            data = _safe_read(path)
            if data and data.get("id") and data.get("id") != request_id:
                continue
            os.remove(path)
    log.info("hh auth request completed: id=%s", request_id)


def peek_pending(profile_name: str | None = None) -> Optional[dict]:
    data = _safe_read(_pending_path())
    if not data:
        return None
    if profile_name and str(data.get("profile_name") or "") != str(profile_name):
        return None
    timeout_at = float(data.get("timeout_at") or 0)
    if timeout_at and timeout_at < time.time():
        with contextlib.suppress(Exception):
            os.remove(_pending_path())
        return None
    resp = _safe_read(_response_path())
    if resp and resp.get("id") == data.get("id"):
        return None
    return data


def write_response(request_id: str, answer: str) -> None:
    pending = _safe_read(_pending_path()) or {}
    _atomic_write(_response_path(), {
        "id": request_id,
        "kind": pending.get("kind") or "",
        "profile_name": pending.get("profile_name") or "",
        "answer": (answer or "").strip(),
        "received_at": time.time(),
    })
    log.info("hh auth response written: id=%s kind=%s", request_id, pending.get("kind"))
