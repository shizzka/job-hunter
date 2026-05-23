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

import config

log = logging.getLogger("captcha_bridge")


def _state_dir() -> str:
    """Папка состояния профиля; должна совпадать с config.HH_STATE_DIR при активном профиле."""
    state_dir = getattr(config, "HH_STATE_DIR", "") or os.path.expanduser("~/.job-hunter/state")
    os.makedirs(state_dir, exist_ok=True)
    return state_dir


def _pending_path() -> str:
    return os.path.join(_state_dir(), "captcha_pending.json")


def _response_path() -> str:
    return os.path.join(_state_dir(), "captcha_response.json")


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


# ---------- Search-side API ----------

def create_request(screenshot_path: str, page_url: str = "", timeout_s: int = 300) -> str:
    """Открыть pending captcha-запрос. Возвращает request_id (UUID)."""
    request_id = uuid.uuid4().hex[:8]
    started_at = time.time()
    data = {
        "id": request_id,
        "screenshot_path": screenshot_path,
        "page_url": page_url,
        "started_at": started_at,
        "timeout_at": started_at + timeout_s,
    }
    _atomic_write(_pending_path(), data)
    # очищаем старый response от предыдущего запроса
    try:
        if os.path.exists(_response_path()):
            os.remove(_response_path())
    except Exception:
        pass
    log.info("captcha request created: id=%s screenshot=%s", request_id, screenshot_path)
    return request_id


async def wait_for_response(request_id: str, timeout_s: int = 300, poll_interval_s: float = 2.0) -> Optional[str]:
    """Дождаться ответа в captcha_response.json. Возвращает текст ответа или None по таймауту/ошибке."""
    import asyncio
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        data = _safe_read(_response_path())
        if data and data.get("id") == request_id:
            answer = str(data.get("answer") or "").strip()
            if answer:
                log.info("captcha answer received: id=%s answer=%r", request_id, answer[:30])
                return answer
        await asyncio.sleep(poll_interval_s)
    log.warning("captcha wait timed out: id=%s", request_id)
    return None


def complete_request(request_id: str) -> None:
    """Снять pending-флаг (после успешного решения, таймаута или отказа)."""
    for path in (_pending_path(), _response_path()):
        try:
            if not os.path.exists(path):
                continue
            data = _safe_read(path)
            if data and data.get("id") and data.get("id") != request_id:
                # это другой свежий запрос, не наш — не трогаем
                continue
            os.remove(path)
        except Exception as exc:
            log.warning("cleanup %s failed: %s", path, exc)
    log.info("captcha request completed: id=%s", request_id)


# ---------- TG-bot side API ----------

def peek_pending() -> Optional[dict]:
    """Возвращает текущий pending-запрос (или None если нет / истёк)."""
    data = _safe_read(_pending_path())
    if not data:
        return None
    timeout_at = float(data.get("timeout_at") or 0)
    if timeout_at and timeout_at < time.time():
        # запрос истёк — чистим
        try:
            os.remove(_pending_path())
        except Exception:
            pass
        return None
    # пропустить если ответ уже записан
    resp = _safe_read(_response_path())
    if resp and resp.get("id") == data.get("id"):
        return None
    return data


def write_response(request_id: str, answer: str) -> None:
    """tg-бот пишет ответ от owner."""
    _atomic_write(_response_path(), {
        "id": request_id,
        "answer": answer,
        "received_at": time.time(),
    })
    log.info("captcha response written: id=%s answer=%r", request_id, answer[:30])
