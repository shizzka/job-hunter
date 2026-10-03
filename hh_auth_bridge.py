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
from pathlib import Path

import config
from state_store.json_store import atomic_write_json
from state_store.prompt_mailbox import PromptMailbox, read_prompt

log = logging.getLogger("hh_auth_bridge")
_request_channels = {}


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
    atomic_write_json(path, data)


def _safe_read(path: str) -> Optional[dict]:
    return read_prompt(path)


def _mailbox(profile_name):
    pending = Path(_pending_path(profile_name)).absolute()
    return PromptMailbox(pending, pending.with_name(f"hh_auth_response.{_profile_file_key(profile_name)}.json"),
                         profile_name=_normalized_profile_name(profile_name))


def _request_mailbox(request_id, profile_name=""):
    cached = _request_channels.get(request_id)
    if cached:
        name, mailbox = cached
        if profile_name and _normalized_profile_name(profile_name) != name:
            raise ValueError("Auth request belongs to another profile")
        return mailbox
    if profile_name:
        return _mailbox(profile_name)
    pending = _find_pending_request(request_id)
    return _mailbox(pending["profile_name"]) if pending else None


def create_request(
    *,
    kind: str,
    profile_name: str,
    prompt: str,
    page_url: str = "",
    timeout_s: int = 900,
) -> str:
    request_id = uuid.uuid4().hex
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
    mailbox = _mailbox(request_profile)
    mailbox.create(data, writer=_atomic_write)
    _request_channels[request_id] = (request_profile, mailbox)
    log.info("hh auth request created: id=%s kind=%s profile=%s", request_id, data["kind"], data["profile_name"])
    return request_id


def _find_pending_request(request_id: str) -> Optional[dict]:
    for path in _pending_paths():
        data = _safe_read(path)
        if data and data.get("id") == request_id:
            if Path(path).name != f"hh_auth_pending.{_profile_file_key(data.get('profile_name'))}.json":
                raise ValueError("Auth prompt profile/path mismatch")
            return data
    return None


async def wait_for_response(
    request_id: str,
    timeout_s: int = 900,
    poll_interval_s: float = 2.0,
    profile_name: str = "",
) -> Optional[str]:
    import asyncio

    deadline = time.monotonic() + max(1, int(timeout_s or 1))
    mailbox = _request_mailbox(request_id, profile_name)
    while mailbox is not None and time.monotonic() < deadline:
        active, answer = mailbox.answer(request_id)
        if not active:
            return None
        if answer:
            log.info("hh auth response received: id=%s", request_id)
            return answer
        await asyncio.sleep(max(0.2, float(poll_interval_s or 2.0)))
    log.warning("hh auth response wait timed out: id=%s", request_id)
    return None


def complete_request(request_id: str, profile_name: str = "") -> None:
    mailbox = _request_mailbox(request_id, profile_name)
    if mailbox is not None:
        mailbox.complete(request_id)
    _request_channels.pop(request_id, None)
    log.info("hh auth request completed: id=%s", request_id)


def peek_pending(profile_name: str | None = None) -> Optional[dict]:
    paths = [_pending_path(profile_name)] if profile_name else _pending_paths()
    pending_items = []
    for path in paths:
        candidate = Path(path)
        mailbox = PromptMailbox(candidate, candidate.with_name(candidate.name.replace("hh_auth_pending.", "hh_auth_response.", 1)))
        data = mailbox.peek()
        if not data:
            continue
        request_profile = _normalized_profile_name(data.get("profile_name"))
        if candidate.name != f"hh_auth_pending.{_profile_file_key(request_profile)}.json":
            raise ValueError("Auth prompt profile/path mismatch")
        pending_items.append(data)
    if not pending_items:
        return None
    return min(pending_items, key=lambda item: float(item.get("started_at") or 0))


def write_response(request_id: str, answer: str, profile_name: str = "") -> None:
    mailbox = _request_mailbox(request_id, profile_name)
    if mailbox is None:
        raise ValueError("Unknown HH auth request")
    mailbox.respond(request_id, answer, writer=_atomic_write)
    log.info("hh auth response written: id=%s", request_id)
