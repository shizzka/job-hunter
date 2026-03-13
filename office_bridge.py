"""Интеграция с AI Office — задачи, логи, статусы."""
import json
import sqlite3
import uuid
import logging
import aiohttp

import config

log = logging.getLogger("office_bridge")

_session: aiohttp.ClientSession | None = None


async def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=10))
    return _session


async def office_log(action: str, message: str, status: str | None = None):
    """Логировать активность агента в AI Office."""
    if not config.OFFICE_URL:
        return
    try:
        payload = {"agentId": config.AGENT_ID, "action": action, "message": message}
        if status:
            payload["status"] = status
        session = await _get_session()
        await session.post(f"{config.OFFICE_URL}/api/activity/log", json=payload, timeout=6)
    except Exception as e:
        log.debug("Office log failed: %s", e)


def create_task(title: str, description: str, priority: str = "medium") -> str:
    """Создать задачу в AI Office (прямой доступ к БД)."""
    if not config.OFFICE_DB:
        return ""
    task_id = str(uuid.uuid4())
    activity_id = str(uuid.uuid4())
    log_id = str(uuid.uuid4())

    try:
        with sqlite3.connect(config.OFFICE_DB) as conn:
            conn.execute(
                """
                INSERT INTO tasks (id, title, description, status, priority, assigned_to, created_at, updated_at)
                VALUES (?, ?, ?, 'assigned', ?, ?, datetime('now'), datetime('now'))
                """,
                (task_id, title, description, priority, config.AGENT_ID),
            )
            conn.execute(
                """
                INSERT INTO activity_logs (id, entity_type, entity_id, action, details, created_at)
                VALUES (?, 'task', ?, 'created', ?, datetime('now'))
                """,
                (
                    activity_id,
                    task_id,
                    json.dumps({
                        "title": title,
                        "assignedTo": config.AGENT_ID,
                        "priority": priority,
                        "source": "job-hunter",
                    }, ensure_ascii=False),
                ),
            )
            conn.execute(
                """
                INSERT INTO task_logs (id, task_id, agent_id, message, created_at)
                VALUES (?, ?, ?, ?, datetime('now'))
                """,
                (log_id, task_id, config.AGENT_ID, f"🔍 Автоматически: {title}"),
            )
            conn.commit()
    except Exception as e:
        log.error("Failed to create task: %s", e)
        return ""

    return task_id


async def task_progress(task_id: str, message: str, percent: int | None = None):
    """Обновить прогресс задачи."""
    if not config.OFFICE_URL:
        return
    try:
        payload: dict = {"message": message, "agentId": config.AGENT_ID}
        if percent is not None:
            payload["percent"] = percent
        session = await _get_session()
        await session.post(
            f"{config.OFFICE_URL}/api/tasks/{task_id}/progress",
            json=payload, timeout=10,
        )
    except Exception as e:
        log.debug("Task progress failed: %s", e)


async def task_complete(task_id: str, message: str):
    """Завершить задачу."""
    if not config.OFFICE_URL:
        return
    try:
        payload = {"message": message, "agentId": config.AGENT_ID}
        session = await _get_session()
        await session.post(
            f"{config.OFFICE_URL}/api/tasks/{task_id}/complete",
            json=payload, timeout=10,
        )
    except Exception as e:
        log.debug("Task complete failed: %s", e)


async def close_session():
    """Закрыть HTTP сессию."""
    global _session
    if _session and not _session.closed:
        await _session.close()
        _session = None
