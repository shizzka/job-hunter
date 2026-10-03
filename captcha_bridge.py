"""IPC между search-процессом (agent.py --search) и tg-bot-процессом
(telegram_bot.py --profile qa) для интерактивного решения hh.ru captcha.

Поток:
1. Search видит captcha → screenshot → notifier.send_photo → create_request(...)
2. tg-bot подхватывает входящее текстовое сообщение от owner_chat_id и пишет
   ответ через write_response()
3. Search polling wait_for_response → получает ответ → вставляет в форму

Файлы (в profile state-dir, чтобы каждый профиль был изолирован):
- captcha_pending.json — текущий запрос
- captcha_response.json — ответ tg-бота
"""
from __future__ import annotations

import json
import logging
import os
import time
import uuid
from typing import Optional
from pathlib import Path

import config
from state_store.json_store import atomic_write_json
from state_store.prompt_mailbox import PromptMailbox, read_prompt

log = logging.getLogger("captcha_bridge")
_request_channels = {}


def _state_dir(profile_name: str | None = None) -> str:
    """Папка состояния активного или явно указанного профиля."""
    state_dir = ""
    if profile_name:
        import profile as profile_mod

        state_dir = str(profile_mod.load_profile(profile_name).state_dir or "")
        if not state_dir:
            raise ValueError("Explicit captcha profile has no state directory")
    state_dir = state_dir or getattr(config, "HH_STATE_DIR", "") or os.path.expanduser("~/.job-hunter/state")
    os.makedirs(state_dir, exist_ok=True)
    return state_dir


def _pending_path(profile_name: str | None = None) -> str:
    return os.path.join(_state_dir(profile_name), "captcha_pending.json")


def _response_path(profile_name: str | None = None) -> str:
    return os.path.join(_state_dir(profile_name), "captcha_response.json")


def _atomic_write(path: str, data: dict) -> None:
    atomic_write_json(path, data)


def _safe_read(path: str) -> Optional[dict]:
    return read_prompt(path)


def _mailbox(profile_name=""):
    pending = Path(_pending_path(profile_name)).absolute()
    return PromptMailbox(pending, pending.with_name("captcha_response.json"), profile_name=profile_name or None)


def _request_mailbox(request_id, profile_name=""):
    cached = _request_channels.get(request_id)
    if cached:
        name, mailbox = cached
        if profile_name and profile_name != name:
            raise ValueError("Captcha request belongs to another profile")
        return mailbox
    return _mailbox(profile_name)


# ---------- Search-side API ----------

def _active_profile_name() -> str:
    try:
        import profile as profile_mod

        return str(profile_mod.active().name or "").strip()
    except Exception:
        return ""


def create_request(
    screenshot_path: str,
    page_url: str = "",
    timeout_s: int = 300,
    profile_name: str = "",
) -> str:
    """Открыть pending captcha-запрос. Возвращает request_id (UUID)."""
    request_id = uuid.uuid4().hex
    started_at = time.time()
    data = {
        "id": request_id,
        "profile_name": profile_name or _active_profile_name(),
        "screenshot_path": screenshot_path,
        "page_url": page_url,
        "started_at": started_at,
        "timeout_at": started_at + timeout_s,
    }
    request_profile = str(data["profile_name"] or "").strip()
    mailbox = _mailbox(request_profile)
    mailbox.create(data, writer=_atomic_write)
    _request_channels[request_id] = (request_profile, mailbox)
    log.info("captcha request created: id=%s screenshot=%s", request_id, screenshot_path)
    return request_id


async def wait_for_response(
    request_id: str,
    timeout_s: int = 300,
    poll_interval_s: float = 2.0,
    profile_name: str = "",
) -> Optional[str]:
    """Дождаться ответа в captcha_response.json. Возвращает текст ответа или None по таймауту/ошибке."""
    import asyncio
    deadline = time.monotonic() + max(1, timeout_s)
    mailbox = _request_mailbox(request_id, profile_name)
    while time.monotonic() < deadline:
        active, answer = mailbox.answer(request_id)
        if not active:
            return None
        if answer:
            log.info("captcha answer received: id=%s", request_id)
            return answer
        await asyncio.sleep(max(0.2, poll_interval_s))
    log.warning("captcha wait timed out: id=%s", request_id)
    return None


def complete_request(request_id: str, profile_name: str = "") -> None:
    """Снять pending-флаг (после успешного решения, таймаута или отказа)."""
    _request_mailbox(request_id, profile_name).complete(request_id)
    _request_channels.pop(request_id, None)
    log.info("captcha request completed: id=%s", request_id)


# ---------- TG-bot side API ----------

def peek_pending(profile_name: str = "") -> Optional[dict]:
    """Возвращает текущий pending-запрос (или None если нет / истёк)."""
    return _mailbox(profile_name).peek()


def write_response(request_id: str, answer: str, profile_name: str = "") -> None:
    """tg-бот пишет ответ от owner."""
    _request_mailbox(request_id, profile_name).respond(request_id, answer, writer=_atomic_write)
    log.info("captcha response written: id=%s", request_id)
