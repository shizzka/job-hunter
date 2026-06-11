#!/usr/bin/env python3
"""Standalone Telegram bot for Job Hunter control and status."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import os
import re
import signal
import time
import traceback
from collections import deque
from dataclasses import dataclass
from datetime import datetime

import aiohttp

import analytics
import client_hh_auth
import config
import profile as profile_mod
import runtime_control
import seen
import telegram_access
import telegram_clients
import telegram_resume_limits

log = logging.getLogger("telegram_bot")

ROLE_ADMIN = telegram_access.ROLE_ADMIN
ROLE_USER = telegram_access.ROLE_USER

BUTTON_STATUS = "📊 Статус"
BUTTON_STATS = "📈 Статистика"
BUTTON_RUNS = "🕓 Прогоны"
BUTTON_LOG = "📜 Лог поиска"
BUTTON_CHAT_LOG = "💬 Лог чатов"
BUTTON_SEARCH = "🔎 Поиск"
BUTTON_DRYRUN = "🧪 Тестовый прогон"
BUTTON_CHECK = "📬 Инвайты"
BUTTON_DIGEST = "📰 Дайджест"
BUTTON_ANALYZE = "🧠 Анализ резюме"
BUTTON_BACKFILL = "🗃 Пересчёт аналитики"
BUTTON_GRAB_RESUME = "📄 Забрать резюме"
BUTTON_AI_LIMITS = "🎁 Лимиты ИИ"
BUTTON_CLIENTS = "🧑‍💼 Клиенты"
BUTTON_CLIENT_START = "🆕 Стать клиентом"
BUTTON_CLIENT_STATUS = "📄 Моя заявка"
BUTTON_HH_AUTH = "🔐 Вход HH"
BUTTON_HH_RESUMES = "🧾 HH резюме"
BUTTON_DAEMON_ON = "🟢 Демон: вкл"
BUTTON_DAEMON_OFF = "⛔ Демон: выкл"
BUTTON_SCHEDULE = "⏰ Расписание"
BUTTON_REPEAT_3DAY = "🔁 3 раза/день"
BUTTON_REPEAT_DAILY = "📅 1 раз/день"
BUTTON_REPEAT_WEEKLY = "🗓 1 раз/нед"
BUTTON_REPEAT_OFF = "🛑 Повтор: выкл"
BUTTON_PROFILES = "🧾 Профили"
BUTTON_USERS = "👥 Пользователи"
BUTTON_HELP = "❓ Помощь"
BUTTON_MENU = "🏠 Меню"
BUTTON_BACK = "◀️ Назад"
BUTTON_BUSY = "⏳ Выполняется..."
BUTTON_CANCEL_ACTIVE = "⏹ Остановить"
BUTTON_MENU_MONITOR = "📊 Мониторинг"
BUTTON_MENU_RUN = "🚀 Запуск"
BUTTON_MENU_REPEAT = "⏰ Повтор"
BUTTON_MENU_ADMIN = "⚙️ Админ"
BUTTON_PROFILE_PREFIX = "📁 Профиль: "
LEGACY_BUTTON_PROFILE_PREFIX = "Профиль: "

MENU_MAIN = "main"
MENU_MONITOR = "monitor"
MENU_RUN = "run"
MENU_REPEAT = "repeat"
MENU_ADMIN = "admin"
PROGRESS_FRAMES = ["⏳", "⌛️", "🕐", "🕑", "🕒", "🕓", "🕔", "🕕"]
CLIENT_ONBOARDING_STEPS = ("full_name", "target_role", "target_location", "notes")
CALLBACK_CLIENT_APPROVE = "ca"
CALLBACK_CLIENT_REJECT = "cr"
CALLBACK_CLIENT_HH_AUTH = "ch"
CALLBACK_CHAT_AI_REPLY = "chat_ai"
CALLBACK_CHAT_AI_SEND = "chat_send"

ADMIN_BUTTON_MAP = {
    BUTTON_MENU_MONITOR: "/menu_monitor",
    BUTTON_MENU_RUN: "/menu_run",
    BUTTON_MENU_REPEAT: "/menu_repeat",
    BUTTON_MENU_ADMIN: "/menu_admin",
    BUTTON_STATUS: "/status",
    BUTTON_STATS: "/stats",
    BUTTON_RUNS: "/runs",
    BUTTON_LOG: "/log",
    BUTTON_CHAT_LOG: "/chat_log",
    BUTTON_SEARCH: "/search",
    BUTTON_DRYRUN: "/dryrun",
    BUTTON_CHECK: "/check",
    BUTTON_DIGEST: "/digest",
    BUTTON_ANALYZE: "/analyze",
    BUTTON_BACKFILL: "/backfill",
    BUTTON_GRAB_RESUME: "/grabresume",
    BUTTON_AI_LIMITS: "/ai_limits",
    BUTTON_CLIENTS: "/clients",
    BUTTON_HH_AUTH: "/hh_auth",
    BUTTON_HH_RESUMES: "/hh_resumes",
    BUTTON_DAEMON_ON: "/daemon_on",
    BUTTON_DAEMON_OFF: "/daemon_off",
    BUTTON_SCHEDULE: "/schedule",
    BUTTON_REPEAT_3DAY: "/repeat_3day",
    BUTTON_REPEAT_DAILY: "/repeat_daily",
    BUTTON_REPEAT_WEEKLY: "/repeat_weekly",
    BUTTON_REPEAT_OFF: "/repeat_off",
    BUTTON_PROFILES: "/profiles",
    BUTTON_USERS: "/users",
    BUTTON_HELP: "/help",
    BUTTON_MENU: "/menu",
    BUTTON_BACK: "/menu",
    BUTTON_BUSY: "/busy",
    BUTTON_CANCEL_ACTIVE: "/cancel_active",
}
USER_BUTTON_MAP = {
    BUTTON_MENU_MONITOR: "/menu_monitor",
    BUTTON_MENU_RUN: "/menu_run",
    BUTTON_MENU_REPEAT: "/menu_repeat",
    BUTTON_STATUS: "/status",
    BUTTON_STATS: "/stats",
    BUTTON_RUNS: "/runs",
    BUTTON_LOG: "/log",
    BUTTON_CHAT_LOG: "/chat_log",
    BUTTON_SEARCH: "/search",
    BUTTON_DRYRUN: "/dryrun",
    BUTTON_ANALYZE: "/analyze",
    BUTTON_CHECK: "/check",
    BUTTON_HH_AUTH: "/hh_auth",
    BUTTON_HH_RESUMES: "/hh_resumes",
    BUTTON_SCHEDULE: "/schedule",
    BUTTON_REPEAT_3DAY: "/repeat_3day",
    BUTTON_REPEAT_DAILY: "/repeat_daily",
    BUTTON_REPEAT_WEEKLY: "/repeat_weekly",
    BUTTON_REPEAT_OFF: "/repeat_off",
    BUTTON_HELP: "/help",
    BUTTON_MENU: "/menu",
    BUTTON_BACK: "/menu",
    BUTTON_BUSY: "/busy",
    BUTTON_CANCEL_ACTIVE: "/cancel_active",
}
GUEST_BUTTON_MAP = {
    BUTTON_CLIENT_START: "/client_start",
    BUTTON_CLIENT_STATUS: "/client_status",
    BUTTON_HH_AUTH: "/hh_auth",
    BUTTON_HH_RESUMES: "/hh_resumes",
    BUTTON_HELP: "/start",
}
LEGACY_BUTTON_MAP = {
    "Мониторинг": "/menu_monitor",
    "Запуск": "/menu_run",
    "Повтор": "/menu_repeat",
    "Админ": "/menu_admin",
    "Статус": "/status",
    "Статистика": "/stats",
    "Прогоны": "/runs",
    "Лог": "/log",
    "Лог поиска": "/log",
    "Логи": "/log",
    "Лог чатов": "/chat_log",
    "Чат лог": "/chat_log",
    "лог": "/log",
    "лог поиска": "/log",
    "логи": "/log",
    "лог чатов": "/chat_log",
    "чат лог": "/chat_log",
    "Поиск": "/search",
    "Dry-run": "/dryrun",
    "Тестовый прогон": "/dryrun",
    "Инвайты": "/check",
    "Дайджест": "/digest",
    "Анализ резюме": "/analyze",
    "AI лимиты": "/ai_limits",
    "Лимиты ИИ": "/ai_limits",
    "Клиенты": "/clients",
    "Стать клиентом": "/client_start",
    "Моя заявка": "/client_status",
    "HH auth": "/hh_auth",
    "Вход HH": "/hh_auth",
    "HH резюме": "/hh_resumes",
    "Backfill": "/backfill",
    "Пересчёт аналитики": "/backfill",
    "Забрать резюме": "/grabresume",
    "Демон ON": "/daemon_on",
    "Демон OFF": "/daemon_off",
    "Демон: вкл": "/daemon_on",
    "Демон: выкл": "/daemon_off",
    "Расписание": "/schedule",
    "Профили": "/profiles",
    "Пользователи": "/users",
    "Помощь": "/help",
    "Меню": "/menu",
    "Назад": "/menu",
    "Выполняется...": "/busy",
    "Остановить": "/cancel_active",
}
ADMIN_ONLY_COMMANDS = {
    "/profiles", "/profile", "/users", "/grant", "/revoke",
    "/digest", "/backfill", "/grabresume",
    "/daemon_on", "/daemon_off",
    "/ai_limits", "/ai_grant", "/ai_reset",
    "/clients", "/client_approve", "/client_reject", "/client_hh_auth",
    "/menu_admin",
    "/cancel_active",
}
ACTIVE_CONFLICT_COMMANDS = {
    "/search", "/dryrun", "/check", "/digest", "/analyze", "/backfill", "/grabresume",
    "/hh_auth", "/client_hh_auth",
    "/daemon_on", "/daemon_off",
    "/repeat_3day", "/repeat_daily", "/repeat_weekly", "/repeat_off",
}
ACTIVE_RUNTIME_TOKENS = ("agent.py",)



@dataclass
class ActiveCommandState:
    label: str
    profile_name: str
    owner_user_id: int
    started_at: float
    task: asyncio.Task | None = None
    subprocess_pid: int = 0
    cancel_requested: bool = False


def _build_logging_handlers() -> list[logging.Handler]:
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if config.TELEGRAM_BOT_LOG_FILE:
        log_dir = os.path.dirname(config.TELEGRAM_BOT_LOG_FILE)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        handlers.append(logging.FileHandler(config.TELEGRAM_BOT_LOG_FILE))
    return handlers


def _configure_logging(force: bool = False) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=_build_logging_handlers(),
        force=force,
    )


_configure_logging()


def normalize_command(text: str) -> tuple[str, str]:
    raw = (text or "").strip()
    if not raw:
        return "", ""
    command, _, tail = raw.partition(" ")
    command = command.split("@", 1)[0].casefold()
    return command, tail.strip()


def split_message(text: str, limit: int = 3800) -> list[str]:
    if len(text) <= limit:
        return [text]

    parts = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut <= 0:
            cut = limit
        parts.append(remaining[:cut].rstrip())
        remaining = remaining[cut:].lstrip()
    if remaining:
        parts.append(remaining)
    return [part for part in parts if part]


def _status_label(state: dict) -> str:
    return "онлайн" if state.get("running") else "остановлен"


def _role_label(role: str) -> str:
    return "администратор" if role == ROLE_ADMIN else "пользователь"


def _role_title(role: str) -> str:
    return "Администратор" if role == ROLE_ADMIN else "Пользователь"


def _status_icon(state: dict) -> str:
    return "🟢" if state.get("running") else "⚪️"


def _ok_icon(ok: bool) -> str:
    return "✅" if ok else "❌"


def _pretty_value(value: object) -> str:
    if value in (None, "", 0):
        return "—"
    if isinstance(value, str):
        return value.replace("T", " ")
    return str(value)


def _pretty_pid(state: dict) -> str:
    pid = int(state.get("pid") or 0)
    return str(pid) if pid > 0 else "—"


def _pretty_profile_name(name: str) -> str:
    if name == "qa":
        return "QA"
    if name == "electrician":
        return "Электрик"
    if name == "default":
        return "Основной"
    return name


def _pretty_runtime_action(action: object) -> str:
    mapping = {
        "search_collect": "Сбор вакансий",
        "search_collect_error": "Сбой одного источника",
        "runtime_stale": "Оборванный прошлый запуск",
        "command_start": "Запуск команды",
        "command_done": "Команда завершена",
        "bot_start": "Запуск бота",
        "bot_stop": "Остановка бота",
        "bot_poll_error": "Ошибка опроса Telegram",
        "daemon_stop": "Остановка демона",
        "search_done": "Поиск завершён",
        "no_invitations": "Инвайтов нет",
    }
    text = str(action or "").strip()
    return mapping.get(text, _pretty_value(text))


def _pretty_runtime_status(status: object) -> str:
    mapping = {
        "working": "выполняется",
        "busy": "занят",
        "idle": "ожидание",
        "error": "ошибка",
        "offline": "остановлен",
        "stale": "устарел",
    }
    text = str(status or "").strip().lower()
    return mapping.get(text, _pretty_value(text))


def _pretty_runtime_mode(mode: object) -> str:
    mapping = {
        "search": "поиск",
        "check": "проверка приглашений",
        "daemon": "демон",
    }
    text = str(mode or "").strip().lower()
    return mapping.get(text, _pretty_value(text))


def _format_interval_label(minutes: int) -> str:
    minutes = max(0, int(minutes or 0))
    if minutes == 0:
        return "выключено"
    if minutes % 10080 == 0:
        weeks = minutes // 10080
        return f"каждые {weeks} нед" if weeks > 1 else "1 раз в неделю"
    if minutes % 1440 == 0:
        days = minutes // 1440
        return f"каждые {days} дн" if days > 1 else "1 раз в день"
    if minutes % 60 == 0:
        hours = minutes // 60
        return f"каждые {hours} ч"
    return f"каждые {minutes} мин"


def _schedule_preset_label(minutes: int) -> str:
    if minutes == 480:
        return "3 раза в день"
    if minutes == 1440:
        return "1 раз в день"
    if minutes == 10080:
        return "1 раз в неделю"
    return _format_interval_label(minutes)


def _format_runtime_block(title: str, runtime: dict) -> list[str]:
    return [
        title,
        f"• Действие: {_pretty_runtime_action(runtime.get('action'))}",
        f"• Статус: {_pretty_runtime_status(runtime.get('status'))}",
        f"• Режим: {_pretty_runtime_mode(runtime.get('mode'))}",
        f"• Сообщение: {_pretty_value(runtime.get('message'))}",
        f"• Обновлено: {_pretty_value(runtime.get('updated_at'))}",
    ]


def _normalize_menu(role: str, menu: str | None) -> str:
    if role == ROLE_ADMIN:
        allowed = {MENU_MAIN, MENU_MONITOR, MENU_RUN, MENU_REPEAT, MENU_ADMIN}
    else:
        allowed = {MENU_MAIN, MENU_MONITOR, MENU_RUN, MENU_REPEAT}
    return menu if menu in allowed else MENU_MAIN


def _append_active_controls(rows: list[list[dict[str, str]]], *, active: bool, can_stop: bool) -> list[list[dict[str, str]]]:
    if not active:
        return rows
    busy_row = [{"text": BUTTON_BUSY}]
    if can_stop:
        busy_row.append({"text": BUTTON_CANCEL_ACTIVE})
    return [*rows, busy_row]


def _command_conflicts_with_active(command: str) -> bool:
    return command in ACTIVE_CONFLICT_COMMANDS


def _normalize_process_runtime(runtime: dict | None, *, expected_tokens: tuple[str, ...]) -> dict | None:
    if not isinstance(runtime, dict):
        return None
    pid = runtime.get("pid") if isinstance(runtime.get("pid"), int) else 0
    status = str(runtime.get("status") or "").strip().lower()
    if pid <= 0 or status not in {"working", "busy"}:
        return runtime
    if runtime_control.is_pid_running(pid):
        cmdline = runtime_control.read_process_cmdline(pid)
        if not cmdline or all(token in cmdline for token in expected_tokens):
            return runtime
    stale_runtime = dict(runtime)
    stale_runtime["action"] = "runtime_stale"
    stale_runtime["message"] = f"Прошлый запуск уже остановлен или оборвался (pid {pid} недоступен)."
    stale_runtime["status"] = "stale"
    stale_runtime["pid"] = 0
    return stale_runtime


def build_reply_markup(
    role: str,
    *,
    menu: str = MENU_MAIN,
    profile_buttons: list[str] | None = None,
    active: bool = False,
    can_stop: bool = False,
) -> dict:
    if profile_buttons is not None:
        rows = [[{"text": f"{BUTTON_PROFILE_PREFIX}{name}"}] for name in profile_buttons]
        rows.append([{"text": BUTTON_BACK}])
        rows = _append_active_controls(rows, active=active, can_stop=can_stop)
        return {
            "keyboard": rows,
            "resize_keyboard": True,
            "one_time_keyboard": True,
            "is_persistent": False,
        }

    menu = _normalize_menu(role, menu)
    if role == ROLE_ADMIN:
        if menu == MENU_MONITOR:
            rows = [
                [{"text": BUTTON_STATUS}, {"text": BUTTON_STATS}],
                [{"text": BUTTON_RUNS}, {"text": BUTTON_CHECK}],
                [{"text": BUTTON_LOG}, {"text": BUTTON_CHAT_LOG}],
                [{"text": BUTTON_MENU}],
            ]
        elif menu == MENU_RUN:
            rows = [
                [{"text": BUTTON_SEARCH}, {"text": BUTTON_DRYRUN}],
                [{"text": BUTTON_CANCEL_ACTIVE}],
                [{"text": BUTTON_ANALYZE}, {"text": BUTTON_DIGEST}],
                [{"text": BUTTON_HH_AUTH}, {"text": BUTTON_HH_RESUMES}],
                [{"text": BUTTON_BACKFILL}, {"text": BUTTON_GRAB_RESUME}],
                [{"text": BUTTON_MENU}],
            ]
        elif menu == MENU_REPEAT:
            rows = [
                [{"text": BUTTON_SCHEDULE}],
                [{"text": BUTTON_REPEAT_3DAY}, {"text": BUTTON_REPEAT_DAILY}],
                [{"text": BUTTON_REPEAT_WEEKLY}, {"text": BUTTON_REPEAT_OFF}],
                [{"text": BUTTON_DAEMON_ON}, {"text": BUTTON_DAEMON_OFF}],
                [{"text": BUTTON_MENU}],
            ]
        elif menu == MENU_ADMIN:
            rows = [
                [{"text": BUTTON_PROFILES}, {"text": BUTTON_USERS}],
                [{"text": BUTTON_CLIENTS}],
                [{"text": BUTTON_AI_LIMITS}, {"text": BUTTON_HELP}],
                [{"text": BUTTON_MENU}],
            ]
        else:
            rows = [
                [{"text": BUTTON_MENU_MONITOR}, {"text": BUTTON_MENU_RUN}],
                [{"text": BUTTON_MENU_REPEAT}, {"text": BUTTON_MENU_ADMIN}],
                [{"text": BUTTON_PROFILES}, {"text": BUTTON_HELP}],
            ]
    else:
        if menu == MENU_MONITOR:
            rows = [
                [{"text": BUTTON_STATUS}, {"text": BUTTON_STATS}],
                [{"text": BUTTON_RUNS}, {"text": BUTTON_CHECK}],
                [{"text": BUTTON_LOG}, {"text": BUTTON_CHAT_LOG}],
                [{"text": BUTTON_MENU}],
            ]
        elif menu == MENU_RUN:
            rows = [
                [{"text": BUTTON_SEARCH}, {"text": BUTTON_DRYRUN}],
                [{"text": BUTTON_CANCEL_ACTIVE}],
                [{"text": BUTTON_ANALYZE}],
                [{"text": BUTTON_HH_AUTH}, {"text": BUTTON_HH_RESUMES}],
                [{"text": BUTTON_MENU}],
            ]
        elif menu == MENU_REPEAT:
            rows = [
                [{"text": BUTTON_SCHEDULE}],
                [{"text": BUTTON_REPEAT_3DAY}, {"text": BUTTON_REPEAT_DAILY}],
                [{"text": BUTTON_REPEAT_WEEKLY}, {"text": BUTTON_REPEAT_OFF}],
                [{"text": BUTTON_MENU}],
            ]
        else:
            rows = [
                [{"text": BUTTON_MENU_MONITOR}, {"text": BUTTON_MENU_RUN}],
                [{"text": BUTTON_MENU_REPEAT}, {"text": BUTTON_HELP}],
            ]
    rows = _append_active_controls(rows, active=active, can_stop=can_stop)
    return {
        "keyboard": rows,
        "resize_keyboard": True,
        "is_persistent": active or menu == MENU_MAIN,
    }


def build_busy_reply_markup(*, can_stop: bool = True) -> dict:
    rows = [[{"text": BUTTON_BUSY}]]
    if can_stop:
        rows[0].append({"text": BUTTON_CANCEL_ACTIVE})
    return {
        "keyboard": rows,
        "resize_keyboard": True,
        "one_time_keyboard": False,
        "is_persistent": False,
    }


def build_guest_reply_markup() -> dict:
    return {
        "keyboard": [
            [{"text": BUTTON_CLIENT_START}],
            [{"text": BUTTON_CLIENT_STATUS}, {"text": BUTTON_HELP}],
        ],
        "resize_keyboard": True,
        "is_persistent": True,
    }


def _callback_data(action: str, user_id: int) -> str:
    return f"{action}:{int(user_id)}"


def _parse_callback_data(data: str) -> tuple[str, int]:
    raw = str(data or "").strip()
    if ":" not in raw:
        return "", 0
    action, _, tail = raw.partition(":")
    if action not in {CALLBACK_CLIENT_APPROVE, CALLBACK_CLIENT_REJECT, CALLBACK_CLIENT_HH_AUTH}:
        return "", 0
    try:
        user_id = int(tail.strip())
    except ValueError:
        return "", 0
    return action, user_id if user_id > 0 else 0


def _parse_chat_action_callback_data(data: str, action: str) -> tuple[str, str, str]:
    raw = str(data or "").strip()
    parts = raw.split(":")
    if len(parts) != 4 or parts[0] != action:
        return "", "", ""
    profile_name, chat_id, message_id = (part.strip() for part in parts[1:])
    if not profile_name or not chat_id.isdigit() or not message_id.isdigit():
        return "", "", ""
    return profile_name, chat_id, message_id


def _parse_chat_ai_callback_data(data: str) -> tuple[str, str, str]:
    return _parse_chat_action_callback_data(data, CALLBACK_CHAT_AI_REPLY)


def _parse_chat_send_callback_data(data: str) -> tuple[str, str, str]:
    return _parse_chat_action_callback_data(data, CALLBACK_CHAT_AI_SEND)


def build_client_review_inline_markup(user_id: int) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "✅ Принять", "callback_data": _callback_data(CALLBACK_CLIENT_APPROVE, user_id)},
            {"text": "🛑 Отклонить", "callback_data": _callback_data(CALLBACK_CLIENT_REJECT, user_id)},
        ]]
    }


def build_client_hh_auth_inline_markup(user_id: int) -> dict:
    return {
        "inline_keyboard": [[
            {"text": "🔐 Запустить вход HH", "callback_data": _callback_data(CALLBACK_CLIENT_HH_AUTH, user_id)},
        ]]
    }


def build_clients_inline_markup(clients: list[dict]) -> dict | None:
    rows: list[list[dict]] = []
    for item in clients:
        user_id = int(item.get("user_id") or 0)
        if user_id <= 0:
            continue
        status = str(item.get("status") or "")
        if status == telegram_clients.STATUS_PENDING_REVIEW:
            rows.append([
                {"text": f"✅ Принять {user_id}", "callback_data": _callback_data(CALLBACK_CLIENT_APPROVE, user_id)},
                {"text": f"🛑 Отклонить {user_id}", "callback_data": _callback_data(CALLBACK_CLIENT_REJECT, user_id)},
            ])
            continue
        if status == telegram_clients.STATUS_APPROVED:
            rows.append([
                {"text": f"🔐 Вход HH {user_id}", "callback_data": _callback_data(CALLBACK_CLIENT_HH_AUTH, user_id)},
            ])
    return {"inline_keyboard": rows} if rows else None


def _format_elapsed(seconds: int) -> str:
    seconds = max(0, int(seconds))
    minutes, seconds = divmod(seconds, 60)
    if minutes:
        return f"{minutes} мин {seconds:02d} сек"
    return f"{seconds} сек"


def build_progress_text(label: str, *, profile_name: str, elapsed_sec: int = 0, frame_idx: int = 0) -> str:
    icon = PROGRESS_FRAMES[frame_idx % len(PROGRESS_FRAMES)]
    pretty_label = _pretty_command_label(label)
    lines = [
        f"{icon} {pretty_label}",
        "",
        f"📁 Профиль: {_pretty_profile_name(profile_name)}",
        f"🕓 Прошло: {_format_elapsed(elapsed_sec)}",
    ]
    if label == "analyze resume":
        lines.extend([
            "",
            "Это долгий запуск ИИ.",
            "Обычно занимает 30-90 секунд.",
            "Если ждёте около минуты, это нормально.",
        ])
    elif label == "hh auth capture":
        lines.extend([
            "",
            "Ожидаем вход в HH в открытом браузере.",
            "После входа сохраним сессию в профиль клиента.",
            "Затем автоматически захватим текущие HH резюме.",
        ])
    else:
        lines.extend([
            "",
            "Команда выполняется.",
            "Конфликтующие команды этого профиля временно заблокированы.",
        ])
    return "\n".join(lines)


def build_busy_status_text(
    *,
    active_command: str,
    profile_name: str,
    elapsed_sec: int,
    pid: int = 0,
    can_cancel: bool = False,
) -> str:
    lines = [
        "⏳ Сейчас выполняется команда",
        "",
        f"• Команда: {_pretty_command_label(active_command)}",
        f"• Профиль: {_pretty_profile_name(profile_name)}",
        f"• Прошло: {_format_elapsed(elapsed_sec)}",
    ]
    if pid > 0:
        lines.append(f"• PID: {pid}")
    lines.extend([
        "",
        "Команды запуска и управления этим профилем временно заблокированы.",
        "Навигация по меню, статус и статистика остаются доступны.",
        "Нажмите «⏳ Выполняется...», чтобы обновить статус.",
    ])
    if can_cancel:
        lines.append("Если запуск завис, можно нажать «⏹ Остановить».")
    return "\n".join(lines)


def build_help_text(role: str = ROLE_ADMIN, *, profile_name: str = "default") -> str:
    lines = [
        "🤖 Job Hunter",
        "",
        f"👤 Роль: {_role_title(role)}",
        f"📁 Профиль: {_pretty_profile_name(profile_name)}",
        "",
        "⚡ Основные действия доступны через кнопки меню.",
        "• «Мониторинг»: статус, статистика, прогоны, инвайты",
        "• «Запуск»: поиск, тестовый прогон, ИИ-анализ, вход HH",
        "• «Повтор»: расписание и периодические запуски",
    ]
    if role == ROLE_ADMIN:
        lines.extend([
            "",
            "🔧 Раздел «Админ»: профили, пользователи, клиенты и лимиты ИИ.",
        ])
    lines.extend([
        "",
        "💡 Слеш-команды можно не помнить: достаточно кнопок меню.",
    ])
    return "\n".join(lines)


def build_menu_section_text(menu: str, *, role: str, profile_name: str) -> str:
    section = _normalize_menu(role, menu)
    profile_title = _pretty_profile_name(profile_name)
    if section == MENU_MONITOR:
        return "\n".join([
            f"📊 Мониторинг профиля {profile_title}",
            "",
            "• Статус процесса и расписания",
            "• Статистика откликов и прогоны",
            "• Проверка инвайтов",
        ])
    if section == MENU_RUN:
        lines = [
            f"🚀 Запуск профиля {profile_title}",
            "",
            "• Разовый поиск и тестовый прогон",
            "• ИИ-проверка резюме",
            "• Вход HH и захват текущих резюме",
        ]
        if role == ROLE_ADMIN:
            lines.extend([
                "• Дайджест и обновление резюме",
                "• Пересчёт аналитики и обновление резюме",
            ])
        return "\n".join(lines)
    if section == MENU_REPEAT:
        lines = [
            f"⏰ Повтор профиля {profile_title}",
            "",
            "• Посмотреть текущее расписание",
            "• Включить режим 3/день, 1/день, 1/нед",
        ]
        if role == ROLE_ADMIN:
            lines.append("• Остановить повтор или вручную включить демон")
        else:
            lines.append("• Остановить повтор, не прерывая текущий разовый поиск")
        return "\n".join(lines)
    if section == MENU_ADMIN:
        return "\n".join([
            f"⚙️ Админ-раздел профиля {profile_title}",
            "",
            "• Переключение профилей",
            "• Просмотр клиентских заявок",
            "• Управление доступами пользователей",
            "• Помощь по командам",
        ])
    return build_help_text(role, profile_name=profile_name)


def build_guest_welcome_text(client: dict | None = None) -> str:
    if client and client.get("status") in {telegram_clients.STATUS_PENDING_REVIEW, telegram_clients.STATUS_APPROVED}:
        return build_client_status_text(client)
    return "\n".join([
        "👋 Добро пожаловать в Job Hunter",
        "",
        "Если хотите стать клиентом, начните короткую заявку прямо здесь.",
        "Кнопка: «Стать клиентом».",
        "Нужно будет ответить на 4 коротких вопроса.",
        "",
        "После проверки администратор создаст профиль и переведёт вас на шаг авторизации.",
    ])


def build_hh_resumes_text(profile_name: str, resumes: list[dict]) -> str:
    title = _pretty_profile_name(profile_name)
    if not resumes:
        return "\n".join([
            f"🧾 HH резюме профиля {title}",
            "",
            "Пока ничего не захвачено.",
            "Сначала запустите вход в HH.",
        ])
    lines = [f"🧾 HH резюме профиля {title}"]
    for idx, item in enumerate(resumes, start=1):
        lines.extend([
            "",
            f"{idx}. {item.get('title') or item.get('id') or 'резюме'}",
            f"• ID: {item.get('id') or '—'}",
            f"• Файл: {item.get('path') or '—'}",
        ])
    return "\n".join(lines)


def build_hh_auth_result_text(result: dict) -> str:
    if result.get("cancelled"):
        return "\n".join([
            "⏹ Вход HH остановлен.",
            "",
            "Захват сессии и резюме был прерван вручную.",
            "Можно запустить попытку ещё раз.",
        ])
    if result.get("timeout"):
        return "\n".join([
            "⏱ Вход HH не завершён.",
            "",
            "Время ожидания истекло до успешного входа в HH.",
            "Администратор может запустить попытку ещё раз.",
        ])
    if not result.get("ok"):
        if result.get("authenticated"):
            return "\n".join([
                "✅ Вход HH завершён.",
                "",
                "Сессия HH сохранена.",
                "Активные HH резюме не найдены.",
                "Проверьте, что на HH есть опубликованное резюме.",
            ])
        return "\n".join([
            "❌ Вход HH завершился с ошибкой.",
            "",
            "Не удалось захватить сессию или текущие HH резюме.",
            "Администратор уже получил журнал отладки для разбора.",
        ])

    resumes = result.get("resumes") or []
    primary = resumes[0] if resumes else {}
    lines = [
        "✅ Вход HH завершён.",
        "",
        "Сессия HH сохранена.",
        f"Захвачено резюме: {result.get('count', 0)}",
    ]
    if primary:
        lines.append(f"Основное резюме: {primary.get('title') or primary.get('id') or '—'}")
    return "\n".join(lines)


def _client_status_label(status: str) -> str:
    mapping = {
        telegram_clients.STATUS_NEW: "новый",
        telegram_clients.STATUS_ONBOARDING: "заполняет заявку",
        telegram_clients.STATUS_PENDING_REVIEW: "ждёт проверки",
        telegram_clients.STATUS_APPROVED: "одобрен",
        telegram_clients.STATUS_REJECTED: "отклонён",
    }
    return mapping.get(status, status or "—")


def _client_auth_label(status: str) -> str:
    mapping = {
        telegram_clients.AUTH_NOT_STARTED: "не начата",
        telegram_clients.AUTH_PENDING_WEB: "ожидает вход через браузер",
        telegram_clients.AUTH_READY: "сессия готова",
    }
    return mapping.get(status, status or "—")


def _client_display_name(client: dict) -> str:
    return str(client.get("full_name") or client.get("username") or client.get("user_id") or "клиент")


def build_client_status_text(client: dict) -> str:
    lines = [
        "📄 Заявка клиента",
        "",
        f"• Статус: {_client_status_label(client.get('status', ''))}",
        f"• Авторизация: {_client_auth_label(client.get('auth_status', ''))}",
    ]
    if client.get("full_name"):
        lines.append(f"• Имя: {client['full_name']}")
    if client.get("target_role"):
        lines.append(f"• Направление: {client['target_role']}")
    if client.get("target_location"):
        lines.append(f"• Локация: {client['target_location']}")
    if client.get("profile_name"):
        lines.append(f"• Профиль: {client['profile_name']}")
    if client.get("admin_note"):
        lines.append(f"• Комментарий: {client['admin_note']}")
    if client.get("submitted_at"):
        lines.append(f"• Отправлена: {_pretty_value(client['submitted_at'])}")
    return "\n".join(lines)


def build_clients_text(clients: list[dict]) -> str:
    if not clients:
        return "🧑‍💼 Клиентских заявок пока нет."
    lines = ["🧑‍💼 Клиенты"]
    for item in clients:
        title = item.get("full_name") or item.get("username") or str(item["user_id"])
        lines.extend([
            "",
            f"• {title} | Telegram {item['user_id']}",
            f"  статус {_client_status_label(item.get('status', ''))} | авторизация {_client_auth_label(item.get('auth_status', ''))}",
        ])
        if item.get("target_role"):
            lines.append(f"  роль {item['target_role']}")
        if item.get("profile_name"):
            lines.append(f"  профиль {item['profile_name']}")
    lines.extend([
        "",
        "⬇️ Быстрые действия вынесены в кнопки под этим сообщением.",
        "",
        "⚙️ Команды:",
        "• /client_approve <id_пользователя> [имя_профиля]",
        "• /client_reject <id_пользователя> [комментарий]",
    ])
    return "\n".join(lines)


def _error_excerpt(text: str, *, max_lines: int = 8, max_chars: int = 900) -> str:
    lines = [line.rstrip() for line in str(text or "").splitlines() if line.strip()]
    if not lines:
        return ""
    snippet = "\n".join(lines[-max_lines:])
    return snippet[-max_chars:] if len(snippet) > max_chars else snippet


def build_hh_auth_admin_text(result: dict, *, client: dict, debug_log_path: str = "") -> str:
    lines = [
        f"🔐 Вход HH клиента {_client_display_name(client)}",
        "",
        f"• Telegram-пользователь: {client.get('user_id') or '—'}",
        f"• Профиль: {result.get('profile_name') or client.get('profile_name') or '—'}",
    ]
    if result.get("cancelled"):
        lines.extend([
            "",
            "⏹ Вход HH остановлен вручную.",
        ])
        if result.get("cookies_file"):
            lines.append(f"• Файл сессии: {result.get('cookies_file')}")
    elif result.get("timeout"):
        lines.extend([
            "",
            "⏱ Время ожидания истекло до входа в HH.",
        ])
    elif result.get("ok"):
        resumes = result.get("resumes") or []
        primary = resumes[0] if resumes else {}
        lines.extend([
            "",
            "✅ Сессия HH сохранена.",
            f"• Файл сессии: {result.get('cookies_file') or '—'}",
            f"• Захвачено резюме: {result.get('count', 0)}",
        ])
        if primary:
            lines.append(f"• Основное резюме: {primary.get('title') or primary.get('id') or '—'}")
        if result.get("resume_file"):
            lines.append(f"• Файл резюме: {result.get('resume_file')}")
        if result.get("catalog_path"):
            lines.append(f"• Каталог HH: {result.get('catalog_path')}")
    elif result.get("authenticated"):
        lines.extend([
            "",
            "✅ Сессия HH сохранена.",
            "• Логин выполнен, но активные HH резюме не найдены.",
        ])
        if result.get("cookies_file"):
            lines.append(f"• Файл сессии: {result.get('cookies_file')}")
    else:
        error_text = (
            result.get("error")
            or _error_excerpt(result.get("stderr") or "")
            or _error_excerpt(result.get("traceback") or "")
            or _error_excerpt(result.get("raw_output") or "")
            or "Подробности смотрите в журнале отладки."
        )
        lines.extend([
            "",
            "❌ Вход HH завершился с ошибкой.",
            f"• Ошибка: {error_text}",
        ])
    if debug_log_path:
        lines.extend([
            "",
            f"🪵 Журнал отладки: {debug_log_path}",
        ])
    return "\n".join(lines)


def parse_hh_auth_command_result(command_result: dict, *, profile_name: str) -> dict:
    stdout = str(command_result.get("stdout") or "").strip()
    stderr = str(command_result.get("stderr") or "").strip()
    parsed: dict = {}
    if stdout:
        for candidate in reversed(stdout.splitlines()):
            candidate = candidate.strip()
            if not candidate.startswith("{"):
                continue
            try:
                payload = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(payload, dict):
                parsed = payload
                break
    result = parsed or {
        "ok": False,
        "timeout": bool(command_result.get("timeout")),
        "profile_name": profile_name,
    }
    if stdout and not result.get("raw_output"):
        result["raw_output"] = stdout
    if stderr and not result.get("stderr"):
        result["stderr"] = stderr
    if command_result.get("cancelled"):
        result["cancelled"] = True
    if not result.get("ok") and not result.get("error"):
        error_text = _error_excerpt(stderr) or _error_excerpt(stdout)
        if error_text:
            result["error"] = error_text
    return result


def build_profiles_text(profiles: list[str], *, selected_profile: str) -> str:
    lines = ["🧾 Доступные профили", ""]
    for name in profiles:
        marker = "✅" if name == selected_profile else "•"
        lines.append(f"{marker} {name}")
    lines.extend(["", "Нажмите кнопку профиля ниже, чтобы переключиться."])
    return "\n".join(lines)


def _format_ai_profile_counts(snapshot: dict) -> str:
    profiles = snapshot.get("profiles") or {}
    if not profiles:
        return "—"
    parts = []
    for profile_name, bucket in sorted(profiles.items()):
        count = int((bucket or {}).get("analysis_count", 0))
        if count <= 0:
            continue
        parts.append(f"{_pretty_profile_name(profile_name)} {count}")
    return ", ".join(parts) if parts else "—"


def format_ai_snapshot_text(snapshot: dict) -> str:
    return "\n".join([
        "🎁 ИИ-проверка резюме",
        "",
        f"• Бесплатных использовано: {snapshot.get('free_used', 0)}/{snapshot.get('free_total', 0)}",
        f"• Бонусов: {snapshot.get('bonus_total', 0)}",
        f"• Мягкий остаток: {snapshot.get('available_soft', 0)}",
        f"• Всего запусков: {snapshot.get('analysis_total', 0)}",
        f"• По профилям: {_format_ai_profile_counts(snapshot)}",
        "",
        "ℹ️ Сейчас лимит мягкий: запуск не блокируется, только считается.",
    ])


def build_ai_limits_text(*, users: list[dict], snapshots: list[dict], events: list[dict]) -> str:
    lines = [
        "🎁 Лимиты ИИ для резюме",
        "",
        f"• Базовый бесплатный лимит: {config.TELEGRAM_AI_FREE_ANALYSES}",
        "• Пока ограничение не блокирует анализ, а только считает использования.",
        "",
        "👥 Пользователи:",
    ]
    if not users:
        lines.append("• Реестр Telegram пока пуст.")
    else:
        by_user_id = {item["user_id"]: item for item in snapshots}
        for user in users:
            snapshot = by_user_id.get(user["user_id"]) or telegram_resume_limits.get_user_snapshot(user["user_id"])
            role_icon = "👑" if user.get("role") == ROLE_ADMIN else "👤"
            label = f" · {user['label']}" if user.get("label") else ""
            lines.append(
                f"• {role_icon} {user['user_id']} | {_role_label(user.get('role', ROLE_USER))} | "
                f"{_pretty_profile_name(user.get('profile', 'default'))}{label}"
            )
            lines.append(
                f"  бесплатных {snapshot.get('free_used', 0)}/{snapshot.get('free_total', 0)} | "
                f"бонусов {snapshot.get('bonus_total', 0)} | "
                f"мягкий остаток {snapshot.get('available_soft', 0)} | "
                f"запусков {snapshot.get('analysis_total', 0)}"
            )

    lines.extend(["", "⚙️ Управление доступно через раздел «Админ»."])
    if events:
        lines.extend(["", "🕓 Последние события:"])
        for item in events:
            actor = f" | администратор {item['actor_user_id']}" if item.get("actor_user_id") else ""
            profile_name = f" | профиль {_pretty_profile_name(item['profile_name'])}" if item.get("profile_name") else ""
            amount = f" | количество {item['amount']}" if item.get("amount") else ""
            lines.append(
                f"• {_pretty_value(item.get('created_at'))} | {item.get('action', 'событие')} | "
                f"пользователь {item.get('user_id', 0)}{profile_name}{amount}{actor}"
            )
    return "\n".join(lines)


def build_schedule_text(
    *,
    profile_name: str,
    daemon_state: dict,
    search_interval_min: int,
    invite_check_interval_min: int,
) -> str:
    return "\n".join([
        f"⏰ Расписание профиля {_pretty_profile_name(profile_name)}",
        "",
        f"• Повтор поиска: {_schedule_preset_label(search_interval_min)}",
        f"• Проверка инвайтов: {_format_interval_label(invite_check_interval_min)}",
        f"• Демон: {_status_icon(daemon_state)} {_status_label(daemon_state)}",
        "",
        "💡 Разовый запуск: кнопка «Поиск».",
        "💡 Повтор: кнопки 3/день, 1/день, 1/нед или «выкл».",
    ])


def build_status_text(
    *,
    profile_name: str,
    daemon_state: dict,
    bot_state: dict,
    search_interval_min: int,
    invite_check_interval_min: int,
    runtime_status: dict | None,
    bot_runtime: dict | None,
    last_run: dict | None,
    active_command: str = "",
) -> str:
    lines = [
        "📊 Статус Job Hunter",
        "",
        f"📁 Профиль: {_pretty_profile_name(profile_name)}",
        "",
        "⚙️ Процессы:",
        f"• {_status_icon(daemon_state)} Демон: {_status_label(daemon_state)} · pid {_pretty_pid(daemon_state)}",
        f"• {_status_icon(bot_state)} Бот: {_status_label(bot_state)} · pid {_pretty_pid(bot_state)}",
        "",
        "⏰ Расписание:",
        f"• Поиск: {_schedule_preset_label(search_interval_min)}",
        f"• Инвайты: {_format_interval_label(invite_check_interval_min)}",
    ]
    if active_command:
        lines.append(f"• ⏳ Активная задача: {_pretty_command_label(active_command)}")
    if runtime_status:
        lines.extend(["", *_format_runtime_block("🧠 Основное состояние:", runtime_status)])
    if bot_runtime:
        lines.extend(["", *_format_runtime_block("🤖 Состояние бота:", bot_runtime)])
    if last_run:
        lines.extend(["", "🕓 Последний прогон:", format_run_summary(last_run)])
    return "\n".join(lines)


def build_stats_text(
    *,
    profile_name: str,
    seen_stats: dict,
    analytics_summary: dict,
    recent_runs: list[dict],
    selected: bool = False,
    profile_snapshots: list[dict] | None = None,
) -> str:
    if profile_snapshots:
        lines = ["📈 Профили Job Hunter"]
        for item in profile_snapshots:
            lines.extend([
                "",
                *build_stats_text(
                    profile_name=item["profile_name"],
                    seen_stats=item["seen_stats"],
                    analytics_summary=item["analytics_summary"],
                    recent_runs=item["recent_runs"],
                    selected=item.get("selected", False),
                ).splitlines(),
            ])
        return "\n".join(lines)

    funnel = analytics_summary.get("funnel", {})
    sent_total = max(
        seen_stats.get("applied", 0),
        analytics_summary.get("auto_applied", 0),
        funnel.get("applied", 0),
    )
    interview_total = analytics_summary.get("interview_statuses", 0)
    offer_total = analytics_summary.get("offer_statuses", 0)
    rejected_total = analytics_summary.get("rejected_statuses", 0)
    pending_total = analytics_summary.get("pending_statuses", 0)
    invitation_total = analytics_summary.get("invitations", 0)
    test_task_total = analytics_summary.get("test_task_statuses", 0)
    viewed_total = analytics_summary.get("pending_viewed_statuses", 0)
    new_total = analytics_summary.get("pending_new_statuses", 0)
    positive_other_total = analytics_summary.get("positive_other_statuses", 0)
    last_run = recent_runs[0] if recent_runs else None

    lines = [
        f"{'✅' if selected else '📁'} Профиль {_pretty_profile_name(profile_name)}{' · текущий' if selected else ''}",
        "",
        "📬 Отклики и исходы:",
        f"• Отправлено: {sent_total}",
        f"• Отказы: {rejected_total} | Собесы: {interview_total} | Офферы: {offer_total}",
        f"• Тестовые: {test_task_total} | Инвайты: {invitation_total} | В ожидании: {pending_total}",
        (
            f"• Просмотрено: {viewed_total} | "
            f"Не просмотрено: {new_total} | "
            f"Прочий позитив: {positive_other_total}"
        ),
        "",
        "🗂 Обработка:",
        f"• Всего просмотрено: {seen_stats.get('total', 0)}",
        f"• Ручных решений: {seen_stats.get('manual', 0)} | Пропущено: {seen_stats.get('skipped', 0)}",
    ]
    by_source = seen_stats.get("by_source", {})
    if by_source:
        lines.extend(["", "🌐 По источникам:"])
        for src, bucket in sorted(by_source.items()):
            lines.append(
                f"• {src}: всего {bucket.get('total', 0)} | "
                f"откликов {bucket.get('applied', 0)} | вручную {bucket.get('manual', 0)} | пропущено {bucket.get('skipped', 0)}"
            )

    lines.extend([
        "",
        "🧮 Аналитика:",
        (
            f"• Прогонов {analytics_summary.get('search_runs', 0)} | "
            f"событий {analytics_summary.get('events', 0)} | "
            f"решений {analytics_summary.get('decisions', 0)}"
        ),
    ])
    if funnel.get("applied", 0) > 0:
        lines.extend([
            (
                f"• Доля ответов: {funnel['response_rate']:.1f}% | "
                f"Позитивных ответов: {funnel['positive_rate']:.1f}%"
            ),
            (
                f"• Воронка: отклики {funnel['applied']} | просмотры {funnel['viewed']} | "
                f"ожидание {funnel.get('pending', 0)} | отказы {funnel.get('rejected', 0)} | позитив {funnel.get('positive', 0)}"
            ),
        ])
    if last_run:
        lines.extend([
            "",
            "🕓 Последний прогон:",
            (
                f"• {_pretty_value(last_run.get('created_at'))} | {_pretty_runtime_mode(last_run.get('mode', 'search'))} | "
                f"найдено {last_run.get('found', 0)} | откликов {last_run.get('applied', 0)} | "
                f"пропущено {last_run.get('skipped', 0)}"
            ),
        ])
    if analytics_summary.get("events", 0) == 0:
        lines.extend(["", "ℹ️ Аналитика этого профиля пока пустая. После поиска или проверки приглашений карточка станет точнее."])
    return "\n".join(lines)


def format_run_summary(run: dict) -> str:
    status = "успешно" if run.get("ok") else "с ошибкой"
    lines = [
        f"{_ok_icon(run.get('ok', False))} {_pretty_value(run.get('created_at'))} | {_pretty_runtime_mode(run.get('mode', 'search'))} | {status}",
        f"• Найдено: {run.get('found', 0)} | Отклики: {run.get('applied', 0)} | Пропущено: {run.get('skipped', 0)}",
    ]
    source_stats = run.get("source_stats", {}) or {}
    if source_stats:
        lines.append("• Источники:")
        for source, bucket in source_stats.items():
            lines.append(
                f"  - {source}: новых {bucket.get('new', 0)} | релевантных {bucket.get('relevant', 0)} | "
                f"откликов {bucket.get('applied', 0)} | вручную {bucket.get('manual', 0)}"
            )
    if run.get("error"):
        lines.append(f"• Ошибка: {run['error']}")
    elif run.get("note"):
        lines.append(f"• Примечание: {run['note']}")
    return "\n".join(lines)


def build_runs_text(recent_runs: list[dict]) -> str:
    if not recent_runs:
        return "🕓 История прогонов пока пустая."
    lines = ["🕓 Последние прогоны"]
    for idx, run in enumerate(recent_runs, start=1):
        summary_lines = format_run_summary(run).splitlines()
        lines.extend(["", f"{idx}. {summary_lines[0]}"])
        lines.extend(summary_lines[1:])
    return "\n".join(lines)


def _unique_paths(paths: list[str]) -> list[str]:
    out: list[str] = []
    for path in paths:
        normalized = os.path.abspath(os.path.expanduser(str(path or "").strip()))
        if normalized and normalized not in out:
            out.append(normalized)
    return out


def _redact_log_text(text: str) -> str:
    redacted = re.sub(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b", "<redacted-telegram-token>", text)
    redacted = re.sub(
        r"(?i)\b(api[_-]?key|token|secret|password)\s*=\s*[^\s]+",
        lambda match: f"{match.group(1)}=<redacted>",
        redacted,
    )
    redacted = re.sub(
        r"(?i)([?&](?:access_token|refresh_token|token|api_key|key)=)[^&\s]+",
        r"\1<redacted>",
        redacted,
    )
    return redacted


def _tail_text_file(path: str, *, lines: int = 80, max_chars: int = 3600) -> str:
    try:
        with open(path, encoding="utf-8", errors="replace") as f:
            tail = deque(f, maxlen=max(1, int(lines)))
    except OSError:
        return ""
    text = "".join(tail).strip()
    if len(text) > max_chars:
        text = text[-max_chars:].lstrip()
    return _redact_log_text(text)


def build_log_text(profile_name: str, *, kind: str, path: str, content: str, lines: int) -> str:
    title = "💬 Лог чатов" if kind == "chat_log" else "📜 Лог поиска"
    if not content:
        return "\n".join([
            title,
            "",
            f"📁 Профиль: {_pretty_profile_name(profile_name)}",
            f"Файл: {path or 'не найден'}",
            "",
            "Лог пустой или файл ещё не создан.",
        ])
    return "\n".join([
        title,
        "",
        f"📁 Профиль: {_pretty_profile_name(profile_name)}",
        f"Файл: {path}",
        f"Последние {lines} строк:",
        "",
        content,
    ])


def _pretty_command_label(label: str) -> str:
    mapping = {
        "search": "Поиск вакансий",
        "dry-run": "Тестовый прогон",
        "check invitations": "Проверка инвайтов",
        "digest": "Дайджест",
        "analyze resume": "ИИ-анализ резюме",
        "hh auth capture": "Авторизация HH и захват резюме",
        "analytics backfill": "Пересчёт аналитики",
        "grab resume": "Загрузка резюме",
    }
    return mapping.get(label, label)


def _strip_markdown_markup(text: str) -> str:
    text = text.replace("\r", "")
    text = re.sub(r"^\s{0,3}#{1,6}\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"\*\*(.*?)\*\*", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"__(.*?)__", r"\1", text, flags=re.DOTALL)
    text = re.sub(r"`([^`]*)`", r"\1", text)
    text = re.sub(r"^\s*[-*]\s+", "• ", text, flags=re.MULTILINE)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _sanitize_analyze_output(stdout: str) -> str:
    cleaned = _extract_analyze_markdown(stdout)
    return _strip_markdown_markup(cleaned)


def _extract_analyze_markdown(stdout: str) -> str:
    cleaned_lines = []
    for raw_line in stdout.splitlines():
        line = raw_line.strip()
        if not line:
            cleaned_lines.append("")
            continue
        if (
            line.startswith("Анализирую резюме:")
            or line.startswith("Модель:")
            or line.startswith("Это может занять")
            or line.startswith("📄 Анализ сохранён:")
        ):
            continue
        cleaned_lines.append(raw_line)
    return "\n".join(cleaned_lines).strip()


def _sanitize_command_output(label: str, stdout: str) -> str:
    text = (stdout or "").strip()
    if not text:
        return ""
    if label == "analyze resume":
        return _sanitize_analyze_output(text)
    return text


def format_command_result(label: str, result: dict, *, role: str = ROLE_ADMIN) -> str:
    pretty_label = _pretty_command_label(label)
    if result.get("cancelled"):
        return "\n".join([
            f"⏹ {pretty_label} остановлен.",
            "",
            "Запуск был прерван вручную.",
        ])
    if result.get("timeout"):
        lines = [f"⏱ {pretty_label} превысил лимит времени."]
        if role == ROLE_ADMIN and result.get("stderr"):
            lines.extend(["", result["stderr"][-1200:]])
        return "\n".join(lines)

    ok = bool(result.get("ok", False))
    stdout = _sanitize_command_output(label, (result.get("stdout") or "").strip())
    stderr = (result.get("stderr") or "").strip()
    lines = [f"{_ok_icon(ok)} {pretty_label} {'завершён' if ok else 'завершён с ошибкой'}"]

    if stdout:
        lines.extend(["", stdout[-3200:]])
    elif ok:
        lines.extend(["", "ℹ️ Команда завершилась без текстового вывода."])
    elif role == ROLE_ADMIN and stderr:
        lines.extend(["", "Технические детали:", stderr[-1600:]])
    else:
        lines.extend(["", "ℹ️ Подробности скрыты. Если проблема повторится, проверьте лог или дайте доступ администратору."])

    if role == ROLE_ADMIN and stderr and stdout:
        lines.extend(["", "stderr:", stderr[-1600:]])
    return "\n".join(lines)


def _resolve_message_command(text: str, role: str) -> tuple[str, str]:
    raw = (text or "").strip()
    if not raw:
        return "", ""
    if raw.startswith(BUTTON_PROFILE_PREFIX):
        return "/profile", raw[len(BUTTON_PROFILE_PREFIX):].strip()
    if raw.startswith(LEGACY_BUTTON_PROFILE_PREFIX):
        return "/profile", raw[len(LEGACY_BUTTON_PROFILE_PREFIX):].strip()
    button_map = ADMIN_BUTTON_MAP if role == ROLE_ADMIN else USER_BUTTON_MAP
    mapped = button_map.get(raw) or LEGACY_BUTTON_MAP.get(raw)
    if mapped:
        return mapped, ""
    return normalize_command(raw)


def _resolve_guest_command(text: str) -> tuple[str, str]:
    raw = (text or "").strip()
    if not raw:
        return "", ""
    mapped = GUEST_BUTTON_MAP.get(raw) or LEGACY_BUTTON_MAP.get(raw)
    if mapped:
        return mapped, ""
    return normalize_command(raw)


def _format_users_text(users: list[dict]) -> str:
    if not users:
        return "👥 Реестр пользователей пуст."
    lines = ["👥 Пользователи Telegram"]
    for item in users:
        label = f" · {item['label']}" if item.get("label") else ""
        enabled_icon = "✅" if item.get("enabled", True) else "⛔"
        role_icon = "👑" if item.get("role", ROLE_USER) == ROLE_ADMIN else "👤"
        lines.append(
            f"• {role_icon} {item['user_id']} | {_role_label(item.get('role', ROLE_USER))} | "
            f"профиль {item.get('profile', 'default')} | {enabled_icon} {'доступ открыт' if item.get('enabled', True) else 'доступ закрыт'}{label}"
        )
    return "\n".join(lines)


class TelegramBot:
    def __init__(self, profile_name: str, drop_pending: bool = True):
        self.profile_name = profile_name
        self.drop_pending = drop_pending
        self._stop_event = asyncio.Event()
        self._sessions: dict[bool, aiohttp.ClientSession] = {}
        self._force_direct = False
        self._active_commands: dict[str, ActiveCommandState] = {}

    async def run(self) -> None:
        if not config.TELEGRAM_CONTROL_BOT_TOKEN:
            raise RuntimeError("Telegram bot requires HUNTER_CONTROL_BOT_TOKEN")

        telegram_access.load_registry()
        runtime_control.register_current_process(
            config.TELEGRAM_BOT_PID_FILE,
            expected_tokens=runtime_control.BOT_TOKENS,
        )
        self._write_runtime("bot_start", "Бот Telegram запущен", "idle")

        loop = asyncio.get_running_loop()
        shutdown_task: asyncio.Task | None = None

        def _signal_handler() -> None:
            nonlocal shutdown_task
            self._stop_event.set()
            if shutdown_task is None or shutdown_task.done():
                shutdown_task = loop.create_task(self._close_sessions())

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _signal_handler)

        try:
            await self._bootstrap_offset()
            while not self._stop_event.is_set():
                try:
                    updates = await self._get_updates()
                except Exception as exc:
                    if self._stop_event.is_set():
                        break
                    log.error("Failed to fetch updates: %s", exc)
                    self._write_runtime("bot_poll_error", f"Ошибка опроса: {exc}", "error")
                    await asyncio.sleep(5)
                    continue

                for update in updates:
                    await self._handle_update(update)
        finally:
            self._write_runtime("bot_stop", "Бот Telegram остановлен", "offline")
            runtime_control.unregister_current_process(config.TELEGRAM_BOT_PID_FILE)
            await self._close_sessions()

    def _load_state(self) -> dict:
        return runtime_control.read_json_file(config.TELEGRAM_BOT_STATE_FILE) or {}

    def _save_state(self, state: dict) -> None:
        runtime_control.write_json_file(config.TELEGRAM_BOT_STATE_FILE, state)

    def _write_runtime(self, action: str, message: str, status: str) -> None:
        runtime_control.write_json_file(
            config.TELEGRAM_BOT_RUNTIME_FILE,
            {
                "action": action,
                "message": message,
                "status": status,
                "pid": os.getpid(),
                "profile": self.profile_name,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            },
        )

    def _prune_active_commands(self) -> None:
        stale_profiles = [
            profile_name
            for profile_name, command in self._active_commands.items()
            if command.task is not None and command.task.done()
        ]
        for profile_name in stale_profiles:
            self._active_commands.pop(profile_name, None)

    def _all_active_commands(self) -> list[ActiveCommandState]:
        self._prune_active_commands()
        return sorted(
            self._active_commands.values(),
            key=lambda item: (item.started_at, item.profile_name),
        )

    def _active_command(self, profile_name: str) -> ActiveCommandState | None:
        self._prune_active_commands()
        return self._active_commands.get(profile_name)

    def _has_active_command(self, profile_name: str | None = None) -> bool:
        if profile_name is None:
            return bool(self._all_active_commands())
        return self._active_command(profile_name) is not None

    def _active_elapsed_sec(self, command: ActiveCommandState | None) -> int:
        if not command or command.started_at <= 0:
            return 0
        return max(0, int(time.monotonic() - command.started_at))

    def _can_cancel_active(self, principal: dict, command: ActiveCommandState | None) -> bool:
        if not command:
            return False
        if principal.get("role") == ROLE_ADMIN:
            return True
        return int(principal.get("user_id") or 0) == command.owner_user_id

    def _sync_active_runtime(self) -> None:
        active_commands = self._all_active_commands()
        if not active_commands:
            self._write_runtime("command_done", "Нет активных команд", "idle")
            return
        if len(active_commands) == 1:
            command = active_commands[0]
            self._write_runtime("command_start", f"Выполняется {command.label} для профиля {command.profile_name}", "busy")
            return
        summary = ", ".join(f"{item.profile_name}:{item.label}" for item in active_commands[:3])
        if len(active_commands) > 3:
            summary += ", ..."
        self._write_runtime("command_start", f"Выполняется {len(active_commands)} команд: {summary}", "busy")

    def _mark_active_command(self, *, principal: dict, label: str, profile_name: str) -> ActiveCommandState:
        command = ActiveCommandState(
            label=label,
            profile_name=profile_name,
            owner_user_id=int(principal.get("user_id") or 0),
            started_at=time.monotonic(),
        )
        self._active_commands[profile_name] = command
        self._sync_active_runtime()
        return command

    def _clear_active_command(self, profile_name: str) -> None:
        self._active_commands.pop(profile_name, None)
        self._sync_active_runtime()

    async def _send_busy_status(self, chat_id: int, principal: dict, *, profile_name: str | None = None) -> None:
        target_profile = profile_name or self._selected_profile(principal)
        command = self._active_command(target_profile)
        if not command:
            await self._send_text(
                chat_id,
                f"✅ Для профиля {_pretty_profile_name(target_profile)} сейчас нет активной команды.",
                reply_markup=self._menu_reply_markup(principal),
            )
            return
        await self._send_text(
            chat_id,
            build_busy_status_text(
                active_command=command.label,
                profile_name=command.profile_name,
                elapsed_sec=self._active_elapsed_sec(command),
                pid=command.subprocess_pid,
                can_cancel=self._can_cancel_active(principal, command),
            ),
            reply_markup=self._menu_reply_markup(principal),
        )

    async def _cancel_active_command(self, chat_id: int, principal: dict, *, profile_name: str | None = None) -> None:
        target_profile = profile_name or self._selected_profile(principal)
        command = self._active_command(target_profile)
        if not command:
            # Свежезапущенной из бота команды нет — ищем внешние agent.py-процессы
            # этого профиля (запущенные через cron/shell/watchdog) и стопим их.
            external = runtime_control.find_agent_pids_for_profile(target_profile)
            if not external:
                await self._send_text(
                    chat_id,
                    f"✅ Для профиля {_pretty_profile_name(target_profile)} сейчас нет активной команды.",
                    reply_markup=self._menu_reply_markup(principal),
                )
                return
            killed: list[str] = []
            for entry in external:
                result = runtime_control.stop_pid(
                    entry["pid"],
                    timeout=10.0,
                    process_group=True,
                )
                if result.get("already_stopped"):
                    continue
                flag_label = runtime_control.AGENT_FLAG_LABELS.get(entry["flag"], entry["flag"])
                killed.append(
                    f"{flag_label} (pid={entry['pid']}, {result.get('signal', 'SIGTERM')})"
                )
            if not killed:
                await self._send_text(
                    chat_id,
                    f"⚪️ Внешние процессы профиля {_pretty_profile_name(target_profile)} уже завершались.",
                    reply_markup=self._menu_reply_markup(principal),
                )
                return
            await self._send_text(
                chat_id,
                "⏹ Остановлено: " + "; ".join(killed),
                reply_markup=self._menu_reply_markup(principal),
            )
            return
        if not self._can_cancel_active(principal, command):
            await self._send_text(
                chat_id,
                build_busy_status_text(
                    active_command=command.label,
                    profile_name=command.profile_name,
                    elapsed_sec=self._active_elapsed_sec(command),
                    pid=command.subprocess_pid,
                    can_cancel=False,
                ),
                reply_markup=build_reply_markup(
                    principal.get("role", ROLE_USER),
                    menu=self._selected_menu(principal),
                    active=True,
                    can_stop=False,
                ),
            )
            return

        command.cancel_requested = True
        if command.subprocess_pid > 0:
            result = runtime_control.stop_pid(
                command.subprocess_pid,
                timeout=15.0,
                process_group=True,
            )
            signal_name = result.get("signal", "SIGTERM")
            await self._send_text(
                chat_id,
                (
                    f"⏹ Останавливаю {_pretty_command_label(command.label)} "
                    f"для {_pretty_profile_name(command.profile_name)} "
                    f"(pid={command.subprocess_pid}, {signal_name})."
                ),
                reply_markup=build_reply_markup(
                    principal.get("role", ROLE_USER),
                    menu=self._selected_menu(principal),
                    active=True,
                    can_stop=False,
                ),
            )
            return

        if command.task:
            command.task.cancel()
        await self._send_text(
            chat_id,
            (
                f"⏹ Останавливаю {_pretty_command_label(command.label)} "
                f"для {_pretty_profile_name(command.profile_name)}."
            ),
            reply_markup=build_reply_markup(
                principal.get("role", ROLE_USER),
                menu=self._selected_menu(principal),
                active=True,
                can_stop=False,
            ),
        )

    def _selected_profile(self, principal: dict) -> str:
        default_profile = self._default_profile_name()
        if principal.get("role") != ROLE_ADMIN:
            available_profiles = set(self._profile_names())
            user_id = int(principal.get("user_id") or 0)
            client = self._client_record(user_id) if user_id > 0 else None
            client_profile = (client.get("profile_name") or "").strip() if client else ""
            if client_profile in available_profiles:
                return client_profile
            selected = (principal.get("profile") or "").strip()
            if selected == "default" and default_profile != "default":
                return default_profile
            return selected or default_profile
        state = self._load_state()
        selected = (((state.get("user_state") or {}).get(str(principal["user_id"])) or {}).get("selected_profile") or "").strip()
        principal_profile = (principal.get("profile") or "").strip()
        if principal_profile == "default" and default_profile != "default":
            principal_profile = default_profile
        return selected or principal_profile or default_profile

    def _set_selected_profile(self, user_id: int, profile_name: str) -> None:
        state = self._load_state()
        user_state = state.setdefault("user_state", {})
        entry = user_state.setdefault(str(user_id), {})
        entry["selected_profile"] = profile_name
        entry["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self._save_state(state)

    def _selected_menu(self, principal: dict) -> str:
        role = principal.get("role", ROLE_USER)
        state = self._load_state()
        selected = (((state.get("user_state") or {}).get(str(principal["user_id"])) or {}).get("menu") or "").strip()
        return _normalize_menu(role, selected or MENU_MAIN)

    def _set_selected_menu(self, user_id: int, menu: str) -> None:
        state = self._load_state()
        user_state = state.setdefault("user_state", {})
        entry = user_state.setdefault(str(user_id), {})
        entry["menu"] = menu
        entry["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self._save_state(state)

    def _guest_state(self, user_id: int) -> dict:
        state = self._load_state()
        return dict((state.get("guest_state") or {}).get(str(user_id)) or {})

    def _set_guest_state(self, user_id: int, **fields: object) -> dict:
        state = self._load_state()
        guest_state = state.setdefault("guest_state", {})
        entry = guest_state.setdefault(str(user_id), {})
        entry.update(fields)
        entry["updated_at"] = datetime.now().isoformat(timespec="seconds")
        self._save_state(state)
        return dict(entry)

    def _clear_guest_state(self, user_id: int) -> None:
        state = self._load_state()
        guest_state = state.setdefault("guest_state", {})
        guest_state.pop(str(user_id), None)
        self._save_state(state)

    def _menu_reply_markup(self, principal: dict, *, menu: str | None = None, profile_buttons: list[str] | None = None) -> dict:
        role = principal.get("role", ROLE_USER)
        selected_menu = _normalize_menu(role, menu or self._selected_menu(principal))
        profile_name = self._selected_profile(principal)
        active_command = self._active_command(profile_name)
        active = active_command is not None
        can_stop = self._can_cancel_active(principal, active_command) if active else False
        return build_reply_markup(
            role,
            menu=selected_menu,
            profile_buttons=profile_buttons,
            active=active,
            can_stop=can_stop,
        )

    def _default_client_profile_name(self, user_id: int) -> str:
        return f"client_{int(user_id)}"

    def _client_record(self, user_id: int) -> dict | None:
        return telegram_clients.get_client(user_id)

    async def _notify_admins(self, text: str, *, reply_markup: dict | None = None) -> None:
        admin_chat_ids = {
            int(item["user_id"])
            for item in telegram_access.list_users()
            if item.get("role") == ROLE_ADMIN and int(item.get("user_id") or 0) > 0
        }
        for chat_id in sorted(admin_chat_ids):
            with contextlib.suppress(Exception):
                await self._send_text(chat_id, text, reply_markup=reply_markup or build_reply_markup(ROLE_ADMIN))

    def _profile_names(self) -> list[str]:
        names = profile_mod.list_profiles()
        visible = [name for name in names if name]
        named = [name for name in visible if name != "default"]
        if named:
            ordered_named = sorted(named, key=lambda item: (item != "qa", item))
            return ordered_named
        return ["default"]

    def _default_profile_name(self) -> str:
        names = self._profile_names()
        if "qa" in names:
            return "qa"
        if names:
            return names[0]
        return "default"

    def _profile(self, profile_name: str):
        return profile_mod.load_profile(profile_name)

    def _analysis_output_path(self, profile_name: str) -> str:
        resume_path = self._profile(profile_name).resume_file
        analysis_path = resume_path.replace(".md", "_analysis.md")
        if analysis_path == resume_path:
            analysis_path = resume_path + ".analysis.md"
        return analysis_path

    def _analysis_document_payload(self, profile_name: str, stdout: str) -> tuple[str, bytes]:
        analysis_path = self._analysis_output_path(profile_name)
        if os.path.isfile(analysis_path):
            with open(analysis_path, "rb") as f:
                return os.path.basename(analysis_path), f.read()
        extracted = _extract_analyze_markdown(stdout)
        filename = f"{profile_name}_resume_analysis.md"
        return filename, extracted.encode("utf-8")

    def _profile_schedule(self, profile_name: str) -> tuple[int, int]:
        profile = self._profile(profile_name)
        return profile.search_interval_min, profile.invite_check_interval_min

    def _log_path_candidates(self, profile_name: str, *, kind: str) -> list[str]:
        profile = self._profile(profile_name)
        if kind == "chat_log":
            profile_dir = os.path.dirname(profile.log_file)
            return _unique_paths([
                os.getenv("JOB_HUNTER_CHAT_LOG_FILE", ""),
                os.path.join(profile_dir, "job-hunter-chat.log") if profile_dir else "",
                "/tmp/job-hunter-chat.log",
            ])
        return _unique_paths([
            profile.log_file,
            config.LOG_FILE,
            "/tmp/job-hunter.log",
        ])

    def _log_tail(self, profile_name: str, *, kind: str = "log", lines: int = 80) -> tuple[str, str]:
        candidates = self._log_path_candidates(profile_name, kind=kind)
        fallback_path = candidates[0] if candidates else ""
        for path in candidates:
            if not path or not os.path.exists(path):
                continue
            content = _tail_text_file(path, lines=lines)
            if content:
                return path, content
            fallback_path = path
        return fallback_path, ""

    async def _send_log_tail(
        self,
        chat_id: int,
        principal: dict,
        *,
        profile_name: str,
        kind: str = "log",
        lines: int = 80,
    ) -> None:
        path, content = self._log_tail(profile_name, kind=kind, lines=lines)
        await self._send_text(
            chat_id,
            build_log_text(profile_name, kind=kind, path=path, content=content, lines=lines),
            reply_markup=self._menu_reply_markup(principal),
        )

    def _update_profile_schedule(self, profile_name: str, *, search_interval_min: int, invite_check_interval_min: int | None = None) -> str:
        invite_minutes = search_interval_min if invite_check_interval_min is None else invite_check_interval_min
        return profile_mod.update_profile_env(
            profile_name,
            {
                "SEARCH_INTERVAL_MIN": int(search_interval_min),
                "INVITE_CHECK_INTERVAL_MIN": int(invite_minutes),
            },
        )

    def _latest_run(self, profile_name: str) -> dict | None:
        return runtime_control.latest_run_entry(self._profile(profile_name).run_history_file)

    def _recent_runs(self, profile_name: str, limit: int = 5) -> list[dict]:
        run_history_file = self._profile(profile_name).run_history_file
        if limit <= 0 or not os.path.exists(run_history_file):
            return []
        try:
            with open(run_history_file, encoding="utf-8") as f:
                lines = [line.strip() for line in f if line.strip()]
        except OSError:
            return []
        items = []
        for line in lines[-limit:]:
            try:
                items.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return list(reversed(items))

    def _stats_snapshot(self, profile_name: str) -> dict:
        profile = self._profile(profile_name)
        return {
            "profile_name": profile_name,
            "seen_stats": seen.stats_from_file(profile.seen_file),
            "analytics_summary": analytics.summarize(events_file=profile.analytics_events_file, all_time=True),
            "recent_runs": self._recent_runs(profile_name, limit=3),
        }

    def _runtime_status(self, profile_name: str) -> dict | None:
        runtime_file = self._profile(profile_name).runtime_status_file
        runtime = runtime_control.read_json_file(runtime_file)
        normalized = _normalize_process_runtime(runtime, expected_tokens=ACTIVE_RUNTIME_TOKENS)
        if runtime and normalized and normalized != runtime:
            runtime_control.write_json_file(runtime_file, normalized)
        return normalized

    def _daemon_state(self, profile_name: str) -> dict:
        profile = self._profile(profile_name)
        runtime = self._runtime_status(profile_name) or {}
        fallback_pid = runtime.get("pid")
        fallback_pid = int(fallback_pid) if isinstance(fallback_pid, int) else 0
        return runtime_control.describe_process(
            profile.daemon_pid_file,
            expected_tokens=runtime_control.AGENT_DAEMON_TOKENS,
            fallback_pid=fallback_pid,
        )

    def _bot_state(self) -> dict:
        bot_runtime = runtime_control.read_json_file(config.TELEGRAM_BOT_RUNTIME_FILE) or {}
        fallback_pid = bot_runtime.get("pid")
        fallback_pid = int(fallback_pid) if isinstance(fallback_pid, int) else 0
        return runtime_control.describe_process(
            config.TELEGRAM_BOT_PID_FILE,
            expected_tokens=runtime_control.BOT_TOKENS,
            fallback_pid=fallback_pid,
        )

    async def _bootstrap_offset(self) -> None:
        state = self._load_state()
        if state.get("last_update_id") is not None or not self.drop_pending:
            return
        updates = await self._get_updates(timeout=0, save_state=False)
        if not updates:
            return
        state["last_update_id"] = updates[-1]["update_id"]
        state["bootstrapped_at"] = datetime.now().isoformat(timespec="seconds")
        self._save_state(state)

    async def _get_session(self, use_proxy: bool, *, timeout: int = 70) -> aiohttp.ClientSession:
        session = self._sessions.get(use_proxy)
        if session and not session.closed:
            return session
        connector = None
        if use_proxy and config.TELEGRAM_PROXY:
            try:
                from aiohttp_socks import ProxyConnector
                connector = ProxyConnector.from_url(config.TELEGRAM_PROXY)
            except ImportError:
                log.warning("aiohttp-socks not installed, proxy disabled for bot")
                use_proxy = False
        session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=timeout),
        )
        self._sessions[use_proxy] = session
        return session

    async def _api_request(self, method: str, payload: dict, *, timeout: int = 70) -> dict | list:
        use_proxy = bool(config.TELEGRAM_PROXY) and not self._force_direct
        try:
            return await self._api_request_once(method, payload, use_proxy=use_proxy, timeout=timeout)
        except Exception as exc:
            if not use_proxy:
                raise
            log.warning("Telegram proxy failed for bot, retrying direct: %s", exc)
            self._force_direct = True
            return await self._api_request_once(method, payload, use_proxy=False, timeout=timeout)

    async def _api_request_once(self, method: str, payload: dict, *, use_proxy: bool, timeout: int) -> dict | list:
        session = await self._get_session(use_proxy, timeout=timeout)
        url = f"https://api.telegram.org/bot{config.TELEGRAM_CONTROL_BOT_TOKEN}/{method}"
        async with session.post(url, json=payload) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200 or not data.get("ok", False):
                raise RuntimeError(f"{method} failed: {resp.status} {data}")
            return data.get("result", {})

    async def _get_updates(self, timeout: int | None = None, save_state: bool = True) -> list[dict]:
        state = self._load_state()
        payload = {
            "timeout": config.TELEGRAM_BOT_POLL_TIMEOUT if timeout is None else timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if state.get("last_update_id") is not None:
            payload["offset"] = int(state["last_update_id"]) + 1
        result = await self._api_request("getUpdates", payload, timeout=payload["timeout"] + 20)
        updates = result if isinstance(result, list) else []
        if updates and save_state:
            state["last_update_id"] = updates[-1]["update_id"]
            state["updated_at"] = datetime.now().isoformat(timespec="seconds")
            self._save_state(state)
        return updates

    async def _send_text(self, chat_id: int, text: str, *, reply_markup: dict | None = None) -> dict | None:
        first_result: dict | None = None
        for idx, part in enumerate(split_message(text)):
            payload = {
                "chat_id": chat_id,
                "text": part[:4096],
                "disable_web_page_preview": True,
            }
            if idx == 0 and reply_markup:
                payload["reply_markup"] = reply_markup
            result = await self._api_request("sendMessage", payload)
            if idx == 0 and isinstance(result, dict):
                first_result = result
        return first_result

    async def _edit_text(self, chat_id: int, message_id: int, text: str, *, reply_markup: dict | None = None) -> None:
        payload = {
            "chat_id": chat_id,
            "message_id": int(message_id),
            "text": text[:4096],
            "disable_web_page_preview": True,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        try:
            await self._api_request("editMessageText", payload)
        except Exception as exc:
            message = str(exc)
            if "message is not modified" in message or "message can't be edited" in message:
                return
            raise

    async def _edit_reply_markup(self, chat_id: int, message_id: int, *, reply_markup: dict | None = None) -> None:
        payload = {
            "chat_id": chat_id,
            "message_id": int(message_id),
        }
        payload["reply_markup"] = reply_markup if reply_markup is not None else {"inline_keyboard": []}
        with contextlib.suppress(Exception):
            await self._api_request("editMessageReplyMarkup", payload)

    async def _send_chat_action(self, chat_id: int, action: str = "typing") -> None:
        with contextlib.suppress(Exception):
            await self._api_request("sendChatAction", {"chat_id": chat_id, "action": action}, timeout=20)

    async def _delete_message(self, chat_id: int, message_id: int) -> None:
        with contextlib.suppress(Exception):
            await self._api_request("deleteMessage", {"chat_id": chat_id, "message_id": int(message_id)}, timeout=20)

    async def _answer_callback_query(self, callback_query_id: str, text: str = "", *, show_alert: bool = False) -> None:
        if not callback_query_id:
            return
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text[:200]
        if show_alert:
            payload["show_alert"] = True
        with contextlib.suppress(Exception):
            await self._api_request("answerCallbackQuery", payload, timeout=20)

    async def _send_document(
        self,
        chat_id: int,
        *,
        filename: str,
        content: bytes,
        caption: str = "",
        reply_markup: dict | None = None,
    ) -> dict:
        use_proxy = bool(config.TELEGRAM_PROXY) and not self._force_direct
        try:
            return await self._send_document_once(
                chat_id,
                filename=filename,
                content=content,
                caption=caption,
                reply_markup=reply_markup,
                use_proxy=use_proxy,
            )
        except Exception as exc:
            if not use_proxy:
                raise
            log.warning("Telegram proxy failed for bot document upload, retrying direct: %s", exc)
            self._force_direct = True
            return await self._send_document_once(
                chat_id,
                filename=filename,
                content=content,
                caption=caption,
                reply_markup=reply_markup,
                use_proxy=False,
            )

    async def _send_document_once(
        self,
        chat_id: int,
        *,
        filename: str,
        content: bytes,
        caption: str,
        reply_markup: dict | None,
        use_proxy: bool,
    ) -> dict:
        session = await self._get_session(use_proxy, timeout=120)
        url = f"https://api.telegram.org/bot{config.TELEGRAM_CONTROL_BOT_TOKEN}/sendDocument"
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        if caption:
            form.add_field("caption", caption[:1024])
        if reply_markup:
            form.add_field("reply_markup", json.dumps(reply_markup, ensure_ascii=False))
        form.add_field(
            "document",
            content,
            filename=filename,
            content_type="text/markdown; charset=utf-8",
        )
        async with session.post(url, data=form) as resp:
            data = await resp.json(content_type=None)
            if resp.status != 200 or not data.get("ok", False):
                raise RuntimeError(f"sendDocument failed: {resp.status} {data}")
            return data.get("result", {})

    async def _run_progress_indicator(
        self,
        *,
        chat_id: int,
        message_id: int,
        label: str,
        profile_name: str,
        reply_markup: dict | None = None,
        interval_sec: int = 4,
    ) -> None:
        started_at = time.monotonic()
        frame_idx = 0
        while True:
            await asyncio.sleep(max(1, interval_sec))
            frame_idx += 1
            elapsed_sec = int(time.monotonic() - started_at)
            await self._send_chat_action(chat_id)
            await self._edit_text(
                chat_id,
                message_id,
                build_progress_text(label, profile_name=profile_name, elapsed_sec=elapsed_sec, frame_idx=frame_idx),
                reply_markup=reply_markup,
            )

    async def _send_menu(self, chat_id: int, principal: dict, *, profile_name: str, menu: str = MENU_MAIN) -> None:
        role = principal.get("role", ROLE_USER)
        selected_menu = _normalize_menu(role, menu)
        self._set_selected_menu(principal["user_id"], selected_menu)
        await self._send_text(
            chat_id,
            build_help_text(role, profile_name=profile_name) if selected_menu == MENU_MAIN else build_menu_section_text(selected_menu, role=role, profile_name=profile_name),
            reply_markup=self._menu_reply_markup(principal, menu=selected_menu),
        )

    async def _send_guest_welcome(self, chat_id: int, user_id: int) -> None:
        await self._send_text(
            chat_id,
            build_guest_welcome_text(telegram_clients.get_client(user_id)),
            reply_markup=build_guest_reply_markup(),
        )

    def _append_debug_log(self, event: str, **fields: object) -> None:
        path = config.TELEGRAM_BOT_DEBUG_LOG_FILE
        if not path:
            return
        try:
            directory = os.path.dirname(path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            payload = {
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "event": event,
                **fields,
            }
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")
        except OSError:
            log.exception("Failed to append Telegram debug log: %s", event)

    async def _approve_client(self, chat_id: int, principal: dict, target_user_id: int, *, profile_name: str = "") -> dict | None:
        reply_markup = self._menu_reply_markup(principal)
        client = telegram_clients.get_client(target_user_id)
        if not client:
            await self._send_text(chat_id, f"❌ Клиент не найден: {target_user_id}", reply_markup=reply_markup)
            return None
        resolved_profile = profile_name.strip() or client.get("profile_name") or self._default_client_profile_name(target_user_id)
        if resolved_profile not in self._profile_names():
            queries = [client.get("target_role") or "поиск работы"]
            profile_mod.create_profile(resolved_profile, search_queries=queries)
        telegram_access.upsert_user(
            target_user_id,
            profile=resolved_profile,
            role=ROLE_USER,
            label=client.get("full_name") or client.get("username") or "",
        )
        client = telegram_clients.set_status(
            target_user_id,
            status=telegram_clients.STATUS_APPROVED,
            auth_status=telegram_clients.AUTH_NOT_STARTED,
            profile_name=resolved_profile,
            admin_note="",
        )
        self._set_selected_menu(target_user_id, MENU_RUN)
        self._append_debug_log(
            "client_approved",
            admin_user_id=principal.get("user_id"),
            target_user_id=target_user_id,
            profile_name=resolved_profile,
        )
        await self._send_text(
            chat_id,
            f"✅ Клиент одобрен: {target_user_id} -> профиль {resolved_profile}",
            reply_markup=reply_markup,
        )
        with contextlib.suppress(Exception):
            await self._send_text(
                target_user_id,
                (
                    "✅ Ваша заявка одобрена.\n\n"
                    f"{build_client_status_text(client)}\n\n"
                    "Следующий этап: вход в HH.\n"
                    "Кнопка «🔐 Вход HH» уже показана ниже на клавиатуре.\n"
                    "Пароль в Telegram мы не просим."
                ),
                reply_markup=build_reply_markup(ROLE_USER, menu=MENU_RUN),
            )
        return client

    async def _reject_client(self, chat_id: int, principal: dict, target_user_id: int, *, admin_note: str = "") -> dict | None:
        reply_markup = self._menu_reply_markup(principal)
        client = telegram_clients.get_client(target_user_id)
        if not client:
            await self._send_text(chat_id, f"❌ Клиент не найден: {target_user_id}", reply_markup=reply_markup)
            return None
        telegram_access.remove_user(target_user_id)
        client = telegram_clients.set_status(
            target_user_id,
            status=telegram_clients.STATUS_REJECTED,
            admin_note=admin_note,
        )
        self._append_debug_log(
            "client_rejected",
            admin_user_id=principal.get("user_id"),
            target_user_id=target_user_id,
            admin_note=admin_note,
        )
        await self._send_text(
            chat_id,
            f"🛑 Заявка отклонена: {target_user_id}",
            reply_markup=reply_markup,
        )
        with contextlib.suppress(Exception):
            await self._send_text(
                target_user_id,
                build_client_status_text(client),
                reply_markup=build_guest_reply_markup(),
            )
        return client

    async def _request_hh_auth(self, chat_id: int, user_id: int, client: dict, *, reply_markup: dict) -> dict:
        updated = telegram_clients.set_status(
            user_id,
            status=telegram_clients.STATUS_APPROVED,
            auth_status=telegram_clients.AUTH_PENDING_WEB,
        )
        self._append_debug_log(
            "hh_auth_requested",
            target_user_id=user_id,
            profile_name=updated.get("profile_name") or self._default_client_profile_name(user_id),
        )
        await self._send_text(
            chat_id,
            (
                "🔐 Запрос на вход HH отправлен.\n\n"
                "Нужен один тап администратора, чтобы подтвердить запуск.\n"
                "После подтверждения мы сохраним сессию и захватим текущие HH резюме."
            ),
            reply_markup=reply_markup,
        )
        await self._notify_admins(
            "\n".join([
                "🔐 Клиент запросил вход HH",
                "",
                f"• Пользователь Telegram: {user_id}",
                f"• Имя: {_client_display_name(updated)}",
                f"• Профиль: {updated.get('profile_name') or self._default_client_profile_name(user_id)}",
                "",
                "Подтвердите запуск кнопкой ниже или командой:",
                f"• /client_hh_auth {user_id}",
            ]),
            reply_markup=build_client_hh_auth_inline_markup(user_id),
        )
        return updated

    async def _dispatch_guest(self, chat_id: int, sender: dict, command: str, arg: str, text: str) -> None:
        user_id = int(sender.get("id") or 0)
        username = str(sender.get("username") or "").strip()
        first_name = str(sender.get("first_name") or "").strip()
        last_name = str(sender.get("last_name") or "").strip()
        guest_state = self._guest_state(user_id)
        active_step = str(guest_state.get("step") or "").strip()

        if command in {"", "/start", "/help"} and not active_step:
            await self._send_guest_welcome(chat_id, user_id)
            return

        if command == "/client_status":
            client = telegram_clients.get_client(user_id)
            if not client:
                await self._send_text(
                    chat_id,
                "📄 Заявки пока нет. Нажмите «Стать клиентом», чтобы начать.",
                reply_markup=build_guest_reply_markup(),
            )
                return
            await self._send_text(chat_id, build_client_status_text(client), reply_markup=build_guest_reply_markup())
            return

        if command == "/hh_auth":
            client = telegram_clients.get_client(user_id)
            if not client or client.get("status") != telegram_clients.STATUS_APPROVED:
                await self._send_text(
                    chat_id,
                    "🔒 Вход HH станет доступен после одобрения заявки.",
                    reply_markup=build_guest_reply_markup(),
                )
                return
            await self._request_hh_auth(chat_id, user_id, client, reply_markup=build_guest_reply_markup())
            return

        if command == "/hh_resumes":
            client = telegram_clients.get_client(user_id)
            if not client or not client.get("profile_name"):
                await self._send_text(
                    chat_id,
                    "🧾 HH резюме пока недоступны: профиль ещё не создан.",
                    reply_markup=build_guest_reply_markup(),
                )
                return
            resumes = client_hh_auth.load_hh_resume_catalog(client["profile_name"])
            await self._send_text(chat_id, build_hh_resumes_text(client["profile_name"], resumes), reply_markup=build_guest_reply_markup())
            return

        if command == "/client_start":
            telegram_clients.start_onboarding(
                user_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
            )
            self._set_guest_state(user_id, step=CLIENT_ONBOARDING_STEPS[0], answers={})
            await self._send_text(
                chat_id,
                "🆕 Начинаем заявку.\n\nКак к вам обращаться? Напишите имя и фамилию.",
                reply_markup=build_guest_reply_markup(),
            )
            return

        if not active_step:
            await self._send_guest_welcome(chat_id, user_id)
            return

        answers = dict(guest_state.get("answers") or {})
        value = (text or "").strip()
        if not value:
            await self._send_text(chat_id, "✍️ Нужен текстовый ответ. Попробуйте ещё раз.", reply_markup=build_guest_reply_markup())
            return

        answers[active_step] = value if not (active_step == "notes" and value == "-") else ""
        current_index = CLIENT_ONBOARDING_STEPS.index(active_step)
        if current_index + 1 < len(CLIENT_ONBOARDING_STEPS):
            next_step = CLIENT_ONBOARDING_STEPS[current_index + 1]
            self._set_guest_state(user_id, step=next_step, answers=answers)
            prompts = {
                "target_role": "На какую роль или направление нацелены?",
                "target_location": "Какую локацию/формат рассматриваете? Например: СПб, удалённо, РФ.",
                "notes": "Добавьте комментарий о себе или ожиданиях. Если нечего добавить, отправьте -.",
            }
            await self._send_text(chat_id, prompts[next_step], reply_markup=build_guest_reply_markup())
            return

        client = telegram_clients.submit_application(
            user_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            full_name=answers.get("full_name", ""),
            target_role=answers.get("target_role", ""),
            target_location=answers.get("target_location", ""),
            notes=answers.get("notes", ""),
        )
        self._clear_guest_state(user_id)
        await self._send_text(
            chat_id,
            (
                "✅ Заявка отправлена.\n\n"
                f"{build_client_status_text(client)}\n\n"
                "Следующий шаг после проверки: выдача профиля и запуск входа через браузер для сохранения сессии."
            ),
            reply_markup=build_guest_reply_markup(),
        )
        await self._notify_admins(
            "\n".join([
                "🆕 Новая клиентская заявка",
                "",
                f"• Пользователь Telegram: {user_id}",
                f"• Имя: {client.get('full_name') or '—'}",
                f"• Направление: {client.get('target_role') or '—'}",
                f"• Локация: {client.get('target_location') or '—'}",
                "",
                "Кнопки ниже или команды:",
                f"• /client_approve {user_id}",
                f"• /client_reject {user_id} причина",
            ]),
            reply_markup=build_client_review_inline_markup(user_id),
        )

    async def _handle_update(self, update: dict) -> None:
        callback = update.get("callback_query") or {}
        if callback:
            await self._handle_callback_query(callback)
            return
        message = update.get("message") or {}
        if (message.get("chat") or {}).get("type") != "private":
            return
        sender = message.get("from") or {}
        user_id = int(sender.get("id") or 0)
        chat_id = int((message.get("chat") or {}).get("id") or 0)
        principal = telegram_access.resolve_user(user_id)
        if not principal:
            if chat_id:
                command, arg = _resolve_guest_command(message.get("text") or "")
                await self._dispatch_guest(chat_id, sender, command, arg, message.get("text") or "")
            return

        text = (message.get("text") or "").strip()

        # captcha-bridge: если есть pending captcha и юзер админ —
        # принимаем короткий текст (≤ 50 символов, без слэшей и стандартных меню-команд)
        # как ответ на captcha и передаём search-процессу через файл.
        if (
            text
            and len(text) <= 50
            and not text.startswith("/")
            and not text.startswith("➡")
            and principal.get("role") == ROLE_ADMIN
        ):
            try:
                import captcha_bridge
                pending = captcha_bridge.peek_pending()
            except Exception as exc:
                log.debug("captcha bridge peek failed: %s", exc)
                pending = None
            if pending:
                # Любой короткий текст-без-слэша от админа во время pending captcha — это ответ.
                # Исключаем только конкретные menu-кнопки (с эмодзи-префиксами).
                is_menu_button = (
                    text in ADMIN_BUTTON_MAP
                    or text in USER_BUTTON_MAP
                    or text in LEGACY_BUTTON_MAP
                )
                if not is_menu_button:
                    try:
                        import captcha_bridge as cb
                        cb.write_response(pending["id"], text)
                        await self._send_text(
                            chat_id,
                            f"✅ Принял ответ на captcha: <code>{text[:80]}</code>\nВставляю в форму hh.ru…",
                        )
                        return
                    except Exception as exc:
                        log.warning("captcha response write failed: %s", exc)

        command, arg = _resolve_message_command(text, principal.get("role", ROLE_USER))
        if not command:
            return
        await self._dispatch(chat_id, principal, command, arg)

    async def _handle_callback_query(self, callback: dict) -> None:
        sender = callback.get("from") or {}
        callback_id = str(callback.get("id") or "")
        user_id = int(sender.get("id") or 0)
        principal = telegram_access.resolve_user(user_id)
        if not principal:
            await self._answer_callback_query(callback_id, "🔒 Доступ закрыт.", show_alert=True)
            return
        if principal.get("role") != ROLE_ADMIN:
            await self._answer_callback_query(callback_id, "🔒 Нужны права администратора.", show_alert=True)
            return

        raw_data = callback.get("data") or ""
        message = callback.get("message") or {}
        chat_id = int(((message.get("chat") or {}).get("id")) or 0)
        message_id = int(message.get("message_id") or 0)
        if chat_id <= 0:
            await self._answer_callback_query(callback_id, "Не удалось определить чат.", show_alert=True)
            return

        if raw_data.startswith(f"{CALLBACK_CHAT_AI_REPLY}:"):
            profile_name, hh_chat_id, hh_message_id = _parse_chat_ai_callback_data(raw_data)
            if not profile_name or profile_name not in self._profile_names():
                await self._answer_callback_query(callback_id, "Не удалось определить профиль.", show_alert=True)
                return
            await self._answer_callback_query(callback_id, "Генерирую ответ через ИИ…")
            if message_id > 0:
                await self._edit_reply_markup(chat_id, message_id)
            await self._start_chat_ai_reply(
                chat_id,
                principal,
                profile_name=profile_name,
                hh_chat_id=hh_chat_id,
                hh_message_id=hh_message_id,
            )
            return

        if raw_data.startswith(f"{CALLBACK_CHAT_AI_SEND}:"):
            profile_name, hh_chat_id, hh_message_id = _parse_chat_send_callback_data(raw_data)
            if not profile_name or profile_name not in self._profile_names():
                await self._answer_callback_query(callback_id, "Не удалось определить профиль.", show_alert=True)
                return
            await self._answer_callback_query(callback_id, "Отправляю ответ в HH…")
            if message_id > 0:
                await self._edit_reply_markup(chat_id, message_id)
            await self._start_chat_ai_reply(
                chat_id,
                principal,
                profile_name=profile_name,
                hh_chat_id=hh_chat_id,
                hh_message_id=hh_message_id,
                force_send=True,
            )
            return

        # captcha-retry: ручной перезапуск поиска из TG (после пропущенного окна captcha).
        if raw_data.startswith("captcha_retry:"):
            request_id = raw_data.split(":", 1)[1].strip()
            await self._answer_callback_query(callback_id, "🔁 Запускаю поиск заново…")
            try:
                import hh_guard
                hh_guard.clear_cooldown()
            except Exception as exc:
                log.warning("clear_cooldown failed: %s", exc)
            if message_id > 0:
                await self._edit_reply_markup(chat_id, message_id)
            await self._send_text(
                chat_id,
                f"🔁 Поиск перезапущен (request_id <code>{request_id}</code>). Жди новую captcha — отвечу ниже.",
                reply_markup=self._menu_reply_markup(principal),
            )
            await self._start_cli_command(
                chat_id,
                principal,
                self._selected_profile(principal),
                "--search",
                "search",
                3600,
            )
            return

        action, target_user_id = _parse_callback_data(raw_data)
        if not action or target_user_id <= 0:
            await self._answer_callback_query(callback_id, "Неизвестное действие.", show_alert=True)
            return
        if action == CALLBACK_CLIENT_APPROVE:
            await self._answer_callback_query(callback_id, "Одобряю клиента…")
            client = await self._approve_client(chat_id, principal, target_user_id)
            if client and message_id > 0:
                await self._edit_reply_markup(chat_id, message_id)
            return
        if action == CALLBACK_CLIENT_REJECT:
            await self._answer_callback_query(callback_id, "Отклоняю заявку…")
            client = await self._reject_client(chat_id, principal, target_user_id)
            if client and message_id > 0:
                await self._edit_reply_markup(chat_id, message_id)
            return
        if action == CALLBACK_CLIENT_HH_AUTH:
            await self._answer_callback_query(callback_id, "Запускаю вход HH…")
            if message_id > 0:
                await self._edit_reply_markup(chat_id, message_id)
            client = self._client_record(target_user_id)
            if not client:
                await self._send_text(chat_id, f"❌ Клиент не найден: {target_user_id}", reply_markup=self._menu_reply_markup(principal))
                return
            if client.get("status") != telegram_clients.STATUS_APPROVED:
                await self._send_text(
                    chat_id,
                    "❌ Вход HH можно запускать только для одобренного клиента.",
                    reply_markup=self._menu_reply_markup(principal),
                )
                return
            await self._start_hh_auth_capture(chat_id, principal, client)
            return

    async def _dispatch(self, chat_id: int, principal: dict, command: str, arg: str) -> None:
        role = principal.get("role", ROLE_USER)
        profile_name = self._selected_profile(principal)
        active_command = self._active_command(profile_name)

        if command == "/busy":
            await self._send_busy_status(chat_id, principal, profile_name=profile_name)
            return
        if command == "/cancel_active":
            await self._cancel_active_command(chat_id, principal, profile_name=profile_name)
            return
        if active_command and _command_conflicts_with_active(command):
            await self._send_busy_status(chat_id, principal, profile_name=profile_name)
            return

        if command in {"/start", "/menu", "/help"}:
            await self._send_menu(chat_id, principal, profile_name=profile_name, menu=MENU_MAIN)
            return

        if command in ADMIN_ONLY_COMMANDS and role != ROLE_ADMIN:
            await self._send_text(chat_id, "🔒 Нужны права администратора.", reply_markup=self._menu_reply_markup(principal))
            return

        menu_commands = {
            "/menu_monitor": MENU_MONITOR,
            "/menu_run": MENU_RUN,
            "/menu_repeat": MENU_REPEAT,
            "/menu_admin": MENU_ADMIN,
        }
        if command in menu_commands:
            await self._send_menu(chat_id, principal, profile_name=profile_name, menu=menu_commands[command])
            return

        if command == "/profiles":
            profiles = self._profile_names()
            await self._send_text(
                chat_id,
                build_profiles_text(profiles, selected_profile=profile_name),
                reply_markup=self._menu_reply_markup(principal, profile_buttons=profiles),
            )
            return

        if command == "/profile":
            target = arg.strip()
            if not target:
                await self._send_text(chat_id, "ℹ️ Использование: /profile <имя_профиля>", reply_markup=self._menu_reply_markup(principal))
                return
            if target not in self._profile_names():
                await self._send_text(chat_id, f"❌ Неизвестный профиль: {target}", reply_markup=self._menu_reply_markup(principal))
                return
            self._set_selected_profile(principal["user_id"], target)
            self._set_selected_menu(principal["user_id"], MENU_MAIN)
            await self._send_text(chat_id, f"✅ Выбран профиль: {target}", reply_markup=self._menu_reply_markup(principal, menu=MENU_MAIN))
            return

        if command == "/users":
            await self._send_text(chat_id, _format_users_text(telegram_access.list_users()), reply_markup=self._menu_reply_markup(principal))
            return

        if command == "/client_status":
            client = self._client_record(principal["user_id"])
            if not client:
                await self._send_text(
                    chat_id,
                    "ℹ️ Клиентская заявка для этого Telegram-пользователя пока не заведена.",
                    reply_markup=self._menu_reply_markup(principal),
                )
                return
            await self._send_text(chat_id, build_client_status_text(client), reply_markup=self._menu_reply_markup(principal))
            return

        if command == "/hh_auth":
            client = self._client_record(principal["user_id"])
            if not client:
                await self._send_text(
                    chat_id,
                    "ℹ️ Вход HH доступен только после заполнения клиентской заявки.",
                    reply_markup=self._menu_reply_markup(principal),
                )
                return
            if client.get("status") != telegram_clients.STATUS_APPROVED:
                await self._send_text(
                    chat_id,
                    "🔒 Вход HH станет доступен после одобрения заявки.",
                    reply_markup=self._menu_reply_markup(principal),
                )
                return
            await self._request_hh_auth(
                chat_id,
                principal["user_id"],
                client,
                reply_markup=self._menu_reply_markup(principal),
            )
            return

        if command == "/hh_resumes":
            client = self._client_record(principal["user_id"])
            target_profile = profile_name
            if client and client.get("profile_name"):
                target_profile = client["profile_name"]
            resumes = client_hh_auth.load_hh_resume_catalog(target_profile)
            await self._send_text(
                chat_id,
                build_hh_resumes_text(target_profile, resumes),
                reply_markup=self._menu_reply_markup(principal),
            )
            return

        if command == "/clients":
            clients = telegram_clients.list_clients()
            actions_markup = build_clients_inline_markup(clients)
            await self._send_text(
                chat_id,
                build_clients_text(clients),
                reply_markup=actions_markup or self._menu_reply_markup(principal),
            )
            return

        if command == "/client_hh_auth":
            parts = [part for part in arg.split() if part]
            if not parts:
                await self._send_text(
                    chat_id,
                    "ℹ️ Использование: /client_hh_auth <id_пользователя>",
                    reply_markup=self._menu_reply_markup(principal),
                )
                return
            try:
                target_user_id = int(parts[0])
            except ValueError:
                await self._send_text(chat_id, "❌ Некорректный ID пользователя.", reply_markup=self._menu_reply_markup(principal))
                return
            client = self._client_record(target_user_id)
            if not client:
                await self._send_text(chat_id, f"❌ Клиент не найден: {target_user_id}", reply_markup=self._menu_reply_markup(principal))
                return
            if client.get("status") != telegram_clients.STATUS_APPROVED:
                await self._send_text(
                    chat_id,
                    "❌ Вход HH можно запускать только для одобренного клиента.",
                    reply_markup=self._menu_reply_markup(principal),
                )
                return
            await self._start_hh_auth_capture(chat_id, principal, client)
            return

        if command == "/client_approve":
            parts = [part for part in arg.split() if part]
            if not parts:
                await self._send_text(
                    chat_id,
                    "ℹ️ Использование: /client_approve <id_пользователя> [имя_профиля]",
                    reply_markup=self._menu_reply_markup(principal),
                )
                return
            try:
                target_user_id = int(parts[0])
            except ValueError:
                await self._send_text(chat_id, "❌ Некорректный ID пользователя.", reply_markup=self._menu_reply_markup(principal))
                return
            profile_name = parts[1].strip() if len(parts) > 1 else ""
            await self._approve_client(chat_id, principal, target_user_id, profile_name=profile_name)
            return

        if command == "/client_reject":
            parts = [part for part in arg.split() if part]
            if not parts:
                await self._send_text(
                    chat_id,
                    "ℹ️ Использование: /client_reject <id_пользователя> [комментарий]",
                    reply_markup=self._menu_reply_markup(principal),
                )
                return
            try:
                target_user_id = int(parts[0])
            except ValueError:
                await self._send_text(chat_id, "❌ Некорректный ID пользователя.", reply_markup=self._menu_reply_markup(principal))
                return
            admin_note = " ".join(parts[1:]).strip()
            await self._reject_client(chat_id, principal, target_user_id, admin_note=admin_note)
            return

        if command == "/ai_limits":
            users = telegram_access.list_users()
            user_ids = [int(item["user_id"]) for item in users]
            snapshots = telegram_resume_limits.list_user_snapshots(user_ids)
            events = telegram_resume_limits.recent_events(limit=8)
            await self._send_text(
                chat_id,
                build_ai_limits_text(users=users, snapshots=snapshots, events=events),
                reply_markup=self._menu_reply_markup(principal),
            )
            return

        if command == "/ai_grant":
            parts = [part for part in arg.split() if part]
            if not parts:
                await self._send_text(
                    chat_id,
                    "ℹ️ Использование: /ai_grant <id_пользователя> [количество]",
                    reply_markup=self._menu_reply_markup(principal),
                )
                return
            try:
                target_user_id = int(parts[0])
            except ValueError:
                await self._send_text(chat_id, "❌ Некорректный ID пользователя.", reply_markup=self._menu_reply_markup(principal))
                return
            try:
                amount = int(parts[1]) if len(parts) > 1 else 1
            except ValueError:
                await self._send_text(chat_id, "❌ Некорректное количество.", reply_markup=self._menu_reply_markup(principal))
                return
            if amount <= 0:
                await self._send_text(chat_id, "❌ Количество должно быть больше нуля.", reply_markup=self._menu_reply_markup(principal))
                return
            snapshot = telegram_resume_limits.grant_bonus(
                target_user_id,
                amount=amount,
                actor_user_id=principal["user_id"],
            )
            await self._send_text(
                chat_id,
                f"✅ Бонус ИИ добавлен пользователю {target_user_id}: +{amount}\n\n{format_ai_snapshot_text(snapshot)}",
                reply_markup=self._menu_reply_markup(principal),
            )
            return

        if command == "/ai_reset":
            parts = [part for part in arg.split() if part]
            if not parts:
                await self._send_text(
                    chat_id,
                    "ℹ️ Использование: /ai_reset <id_пользователя> [новый_бесплатный_лимит]",
                    reply_markup=self._menu_reply_markup(principal),
                )
                return
            try:
                target_user_id = int(parts[0])
            except ValueError:
                await self._send_text(chat_id, "❌ Некорректный ID пользователя.", reply_markup=self._menu_reply_markup(principal))
                return
            try:
                free_total = int(parts[1]) if len(parts) > 1 else None
            except ValueError:
                await self._send_text(chat_id, "❌ Некорректный новый бесплатный лимит.", reply_markup=self._menu_reply_markup(principal))
                return
            snapshot = telegram_resume_limits.reset_free_limit(
                target_user_id,
                actor_user_id=principal["user_id"],
                free_total=free_total,
            )
            await self._send_text(
                chat_id,
                f"♻️ Бесплатный лимит сброшен для пользователя {target_user_id}.\n\n{format_ai_snapshot_text(snapshot)}",
                reply_markup=self._menu_reply_markup(principal),
            )
            return

        if command == "/grant":
            parts = [part for part in arg.split() if part]
            if len(parts) < 2:
                await self._send_text(
                    chat_id,
                    "ℹ️ Использование: /grant <id_пользователя> <профиль> [admin|user]",
                    reply_markup=self._menu_reply_markup(principal),
                )
                return
            try:
                target_user_id = int(parts[0])
            except ValueError:
                await self._send_text(chat_id, "❌ Некорректный ID пользователя.", reply_markup=self._menu_reply_markup(principal))
                return
            target_profile = parts[1]
            target_role = parts[2].casefold() if len(parts) > 2 else ROLE_USER
            if target_profile not in self._profile_names():
                await self._send_text(chat_id, f"❌ Неизвестный профиль: {target_profile}", reply_markup=self._menu_reply_markup(principal))
                return
            record = telegram_access.upsert_user(target_user_id, profile=target_profile, role=target_role)
            await self._send_text(
                chat_id,
                f"✅ Доступ выдан: {record['user_id']} -> профиль {record['profile']} ({record['role']})",
                reply_markup=self._menu_reply_markup(principal),
            )
            return

        if command == "/revoke":
            try:
                target_user_id = int(arg.strip())
            except ValueError:
                await self._send_text(chat_id, "ℹ️ Использование: /revoke <id_пользователя>", reply_markup=self._menu_reply_markup(principal))
                return
            removed = telegram_access.remove_user(target_user_id)
            await self._send_text(
                chat_id,
                f"{'✅ Пользователь удалён' if removed else '⚪️ Пользователь не найден'}: {target_user_id}",
                reply_markup=self._menu_reply_markup(principal),
            )
            return

        if command == "/status":
            search_interval_min, invite_check_interval_min = self._profile_schedule(profile_name)
            active_command = self._active_command(profile_name)
            await self._send_text(
                chat_id,
                build_status_text(
                    profile_name=profile_name,
                    daemon_state=self._daemon_state(profile_name),
                    bot_state=self._bot_state(),
                    search_interval_min=search_interval_min,
                    invite_check_interval_min=invite_check_interval_min,
                    runtime_status=self._runtime_status(profile_name),
                    bot_runtime=runtime_control.read_json_file(config.TELEGRAM_BOT_RUNTIME_FILE),
                    last_run=self._latest_run(profile_name),
                    active_command=active_command.label if active_command else "",
                ),
                reply_markup=self._menu_reply_markup(principal),
            )
            return

        if command == "/schedule":
            search_interval_min, invite_check_interval_min = self._profile_schedule(profile_name)
            await self._send_text(
                chat_id,
                build_schedule_text(
                    profile_name=profile_name,
                    daemon_state=self._daemon_state(profile_name),
                    search_interval_min=search_interval_min,
                    invite_check_interval_min=invite_check_interval_min,
                ),
                reply_markup=self._menu_reply_markup(principal),
            )
            return

        if command == "/stats":
            if role == ROLE_ADMIN:
                ordered_profiles = [profile_name] + [name for name in self._profile_names() if name != profile_name]
                snapshots = []
                for name in ordered_profiles:
                    snapshot = self._stats_snapshot(name)
                    snapshot["selected"] = name == profile_name
                    snapshots.append(snapshot)
                text = build_stats_text(
                    profile_name=profile_name,
                    seen_stats=snapshots[0]["seen_stats"],
                    analytics_summary=snapshots[0]["analytics_summary"],
                    recent_runs=snapshots[0]["recent_runs"],
                    selected=True,
                    profile_snapshots=snapshots,
                )
            else:
                snapshot = self._stats_snapshot(profile_name)
                text = build_stats_text(
                    profile_name=profile_name,
                    seen_stats=snapshot["seen_stats"],
                    analytics_summary=snapshot["analytics_summary"],
                    recent_runs=snapshot["recent_runs"],
                    selected=True,
                )
            await self._send_text(chat_id, text, reply_markup=self._menu_reply_markup(principal))
            return

        if command == "/runs":
            await self._send_text(chat_id, build_runs_text(self._recent_runs(profile_name, limit=5)), reply_markup=self._menu_reply_markup(principal))
            return

        if command in {"/log", "/hunter_log"}:
            await self._send_log_tail(chat_id, principal, profile_name=profile_name, kind="log")
            return

        if command in {"/chat_log", "/hunter_chat_log"}:
            await self._send_log_tail(chat_id, principal, profile_name=profile_name, kind="chat_log")
            return

        command_map = {
            "/search": ("--search", "search", 3600),
            "/dryrun": ("--dry-run", "dry-run", 3600),
            "/check": ("--check", "check invitations", 1800),
            "/digest": ("--digest", "digest", 1800),
            "/analyze": ("--analyze-resume", "analyze resume", 3600),
            "/backfill": ("--analytics-backfill", "analytics backfill", 3600),
            "/grabresume": ("--grab-resume", "grab resume", 1800),
        }
        if command in command_map:
            flag, label, timeout = command_map[command]
            await self._start_cli_command(chat_id, principal, profile_name, flag, label, timeout)
            return

        if command == "/daemon_on":
            state = self._daemon_state(profile_name)
            profile = self._profile(profile_name)
            result = runtime_control.start_background_process(
                runtime_control.agent_command_argv(profile_name, "--daemon"),
                pid_file=profile.daemon_pid_file,
                log_file=config.LOG_FILE,
                expected_tokens=runtime_control.AGENT_DAEMON_TOKENS,
                fallback_pid=state.get("pid") or 0,
            )
            if result["already_running"]:
                await self._send_text(chat_id, f"🟢 Демон уже запущен: pid={result['pid']}", reply_markup=self._menu_reply_markup(principal))
                return
            if not result["ok"]:
                await self._send_text(
                    chat_id,
                    f"❌ Не удалось запустить демон. Лог: {result['log_file']}",
                    reply_markup=self._menu_reply_markup(principal),
                )
                return
            await self._send_text(
                chat_id,
                f"✅ Демон запущен для профиля {profile_name}: pid={result['pid']}",
                reply_markup=self._menu_reply_markup(principal),
            )
            return

        preset_map = {
            "/repeat_3day": ("3 раза в день", 480),
            "/repeat_daily": ("1 раз в день", 1440),
            "/repeat_weekly": ("1 раз в неделю", 10080),
        }
        if command in preset_map:
            label, minutes = preset_map[command]
            env_file = self._update_profile_schedule(profile_name, search_interval_min=minutes)
            state = self._daemon_state(profile_name)
            profile = self._profile(profile_name)
            if state["running"]:
                runtime_control.stop_process(
                    profile.daemon_pid_file,
                    expected_tokens=runtime_control.AGENT_DAEMON_TOKENS,
                    fallback_pid=state.get("pid") or 0,
                )
            result = runtime_control.start_background_process(
                runtime_control.agent_command_argv(profile_name, "--daemon"),
                pid_file=profile.daemon_pid_file,
                log_file=config.LOG_FILE,
                expected_tokens=runtime_control.AGENT_DAEMON_TOKENS,
                fallback_pid=0,
            )
            if not result["ok"] and not result.get("already_running"):
                await self._send_text(
                    chat_id,
                    f"❌ Не удалось включить повтор для {profile_name}. Лог: {result['log_file']}",
                    reply_markup=self._menu_reply_markup(principal),
                )
                return
            await self._send_text(
                chat_id,
                (
                    f"✅ Повтор включён для профиля {profile_name}: {label}\n"
                    f"• profile.env: {env_file}\n"
                    f"• daemon pid: {result.get('pid', 0)}"
                ),
                reply_markup=self._menu_reply_markup(principal),
            )
            return

        if command == "/repeat_off":
            state = self._daemon_state(profile_name)
            profile = self._profile(profile_name)
            result = runtime_control.stop_process(
                profile.daemon_pid_file,
                expected_tokens=runtime_control.AGENT_DAEMON_TOKENS,
                fallback_pid=state.get("pid") or 0,
            )
            await self._send_text(
                chat_id,
                (
                    f"{'🛑 Повтор остановлен' if not result.get('already_stopped') else '⚪️ Повтор уже был выключен'} "
                    f"для профиля {profile_name}.\n"
                    f"Разовый запуск остаётся доступен через кнопку «Поиск»."
                ),
                reply_markup=self._menu_reply_markup(principal),
            )
            return

        if command == "/daemon_off":
            state = self._daemon_state(profile_name)
            profile = self._profile(profile_name)
            result = runtime_control.stop_process(
                profile.daemon_pid_file,
                expected_tokens=runtime_control.AGENT_DAEMON_TOKENS,
                fallback_pid=state.get("pid") or 0,
            )
            if result.get("already_stopped"):
                await self._send_text(chat_id, "⚪️ Демон уже остановлен.", reply_markup=self._menu_reply_markup(principal))
                return
            await self._send_text(
                chat_id,
                f"🛑 Остановка демона запрошена для профиля {profile_name}: pid={result['pid']} via {result.get('signal', 'SIGTERM')}",
                reply_markup=self._menu_reply_markup(principal),
            )
            return

        await self._send_text(chat_id, f"❓ Неизвестная команда: {command}", reply_markup=self._menu_reply_markup(principal))

    async def _start_hh_auth_capture(self, chat_id: int, principal: dict, client: dict) -> None:
        role = principal.get("role", ROLE_USER)
        reply_markup = self._menu_reply_markup(principal)

        target_user_id = int(client["user_id"])
        profile_name = client.get("profile_name") or self._default_client_profile_name(target_user_id)
        if self._has_active_command(profile_name):
            await self._send_busy_status(chat_id, principal, profile_name=profile_name)
            return
        telegram_clients.set_status(
            target_user_id,
            status=telegram_clients.STATUS_APPROVED,
            auth_status=telegram_clients.AUTH_PENDING_WEB,
            profile_name=profile_name,
        )
        with contextlib.suppress(Exception):
            await self._send_text(
                target_user_id,
                "🔐 Администратор подтвердил вход HH. Запускаем захват сессии и текущих HH резюме.",
                reply_markup=build_reply_markup(ROLE_USER, menu=MENU_RUN),
            )
        active_command = self._mark_active_command(principal=principal, label="hh auth capture", profile_name=profile_name)
        self._append_debug_log(
            "hh_auth_capture_started",
            admin_user_id=principal.get("user_id"),
            target_user_id=target_user_id,
            profile_name=profile_name,
            argv=runtime_control.client_hh_auth_command_argv(profile_name, timeout_sec=900),
        )
        progress_message = await self._send_text(
            chat_id,
            build_progress_text("hh auth capture", profile_name=profile_name),
            reply_markup=self._menu_reply_markup(principal),
        )
        progress_task: asyncio.Task | None = None
        progress_message_id = int((progress_message or {}).get("message_id") or 0)
        if progress_message_id > 0:
            progress_task = asyncio.create_task(
                self._run_progress_indicator(
                    chat_id=chat_id,
                    message_id=progress_message_id,
                    label="hh auth capture",
                    profile_name=profile_name,
                    reply_markup=self._menu_reply_markup(principal),
                    interval_sec=5,
                )
            )

        async def runner() -> None:
            nonlocal progress_task
            try:
                command_result = await runtime_control.run_command_capture(
                    runtime_control.client_hh_auth_command_argv(profile_name, timeout_sec=900),
                    timeout=1800,
                    on_start=lambda pid: setattr(active_command, "subprocess_pid", pid),
                )
                if active_command.cancel_requested:
                    command_result["cancelled"] = True
                self._append_debug_log(
                    "hh_auth_capture_command_result",
                    admin_user_id=principal.get("user_id"),
                    target_user_id=target_user_id,
                    profile_name=profile_name,
                    command_result=command_result,
                )
                result = parse_hh_auth_command_result(command_result, profile_name=profile_name)
                self._append_debug_log(
                    "hh_auth_capture_result",
                    admin_user_id=principal.get("user_id"),
                    target_user_id=target_user_id,
                    profile_name=profile_name,
                    result=result,
                )
                telegram_clients.set_status(
                    target_user_id,
                    status=telegram_clients.STATUS_APPROVED,
                    auth_status=(
                        telegram_clients.AUTH_READY
                        if (result.get("ok") or result.get("authenticated")) and not result.get("cancelled")
                        else telegram_clients.AUTH_PENDING_WEB
                    ),
                    profile_name=profile_name,
                )
                if progress_task:
                    progress_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await progress_task
                    progress_task = None
                if progress_message_id > 0:
                    await self._delete_message(chat_id, progress_message_id)
                admin_text = build_hh_auth_admin_text(
                    result,
                    client=telegram_clients.get_client(target_user_id) or client,
                    debug_log_path=config.TELEGRAM_BOT_DEBUG_LOG_FILE,
                )
                await self._send_text(chat_id, admin_text, reply_markup=reply_markup)
                with contextlib.suppress(Exception):
                    await self._send_text(
                        target_user_id,
                        (
                            f"{build_hh_auth_result_text(result)}\n\n"
                            f"{build_client_status_text(telegram_clients.get_client(target_user_id) or client)}"
                        ),
                        reply_markup=build_reply_markup(ROLE_USER, menu=MENU_RUN),
                    )
            except asyncio.CancelledError:
                if progress_task:
                    progress_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await progress_task
                    progress_task = None
                if progress_message_id > 0:
                    await self._delete_message(chat_id, progress_message_id)
                await self._send_text(
                    chat_id,
                    build_hh_auth_admin_text(
                        {"cancelled": True, "profile_name": profile_name},
                        client=telegram_clients.get_client(target_user_id) or client,
                        debug_log_path=config.TELEGRAM_BOT_DEBUG_LOG_FILE,
                    ),
                    reply_markup=reply_markup,
                )
                with contextlib.suppress(Exception):
                    await self._send_text(
                        target_user_id,
                        build_hh_auth_result_text({"cancelled": True}),
                        reply_markup=build_reply_markup(ROLE_USER, menu=MENU_RUN),
                    )
            except Exception as exc:
                log.error("HH auth capture failed: %s", exc, exc_info=True)
                self._append_debug_log(
                    "hh_auth_capture_exception",
                    admin_user_id=principal.get("user_id"),
                    target_user_id=target_user_id,
                    profile_name=profile_name,
                    error=str(exc),
                    traceback=traceback.format_exc(),
                )
                if progress_task:
                    progress_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await progress_task
                    progress_task = None
                if progress_message_id > 0:
                    await self._delete_message(chat_id, progress_message_id)
                await self._send_text(
                    chat_id,
                    (
                        "❌ Вход HH завершился с ошибкой.\n\n"
                        f"• Ошибка: {exc}\n"
                        f"• Журнал отладки: {config.TELEGRAM_BOT_DEBUG_LOG_FILE}"
                    ),
                    reply_markup=reply_markup,
                )
                with contextlib.suppress(Exception):
                    await self._send_text(
                        target_user_id,
                        (
                            "❌ Вход HH завершился с ошибкой.\n\n"
                            "Администратор уже получил журнал отладки и сможет повторить запуск."
                        ),
                        reply_markup=build_reply_markup(ROLE_USER, menu=MENU_RUN),
                    )
            finally:
                self._clear_active_command(profile_name)

        active_command.task = asyncio.create_task(runner())

    async def _start_chat_ai_reply(
        self,
        chat_id: int,
        principal: dict,
        *,
        profile_name: str,
        hh_chat_id: str,
        hh_message_id: str,
        force_send: bool = False,
    ) -> None:
        role = principal.get("role", ROLE_USER)
        reply_markup = self._menu_reply_markup(principal)
        if self._has_active_command(profile_name):
            await self._send_busy_status(chat_id, principal, profile_name=profile_name)
            return

        label = "chat AI send" if force_send else "chat AI reply"
        active_command = self._mark_active_command(principal=principal, label=label, profile_name=profile_name)
        progress_message = await self._send_text(
            chat_id,
            build_progress_text(label, profile_name=profile_name),
            reply_markup=self._menu_reply_markup(principal),
        )
        progress_task: asyncio.Task | None = None
        progress_message_id = int((progress_message or {}).get("message_id") or 0)
        if progress_message_id > 0:
            progress_task = asyncio.create_task(
                self._run_progress_indicator(
                    chat_id=chat_id,
                    message_id=progress_message_id,
                    label=label,
                    profile_name=profile_name,
                    reply_markup=self._menu_reply_markup(principal),
                    interval_sec=5,
                )
            )

        async def runner() -> None:
            nonlocal progress_task
            try:
                argv = runtime_control.agent_command_argv(
                    profile_name,
                    "--chat-respond-one",
                    hh_chat_id,
                    "--chat-message-id",
                    hh_message_id,
                    "--chat-allow-suspicious",
                )
                if force_send:
                    argv.append("--chat-force-send")
                result = await runtime_control.run_command_capture(
                    argv,
                    timeout=1200,
                    on_start=lambda pid: setattr(active_command, "subprocess_pid", pid),
                )
                if active_command.cancel_requested:
                    result["cancelled"] = True
                message = format_command_result(label, result, role=role)
                if progress_task:
                    progress_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await progress_task
                    progress_task = None
                if progress_message_id > 0:
                    await self._delete_message(chat_id, progress_message_id)
                await self._send_text(chat_id, message, reply_markup=reply_markup)
            except asyncio.CancelledError:
                if progress_task:
                    progress_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await progress_task
                    progress_task = None
                if progress_message_id > 0:
                    await self._delete_message(chat_id, progress_message_id)
                await self._send_text(
                    chat_id,
                    format_command_result(label, {"cancelled": True}, role=role),
                    reply_markup=reply_markup,
                )
            except Exception as exc:
                log.error("Chat AI reply failed: %s", exc, exc_info=True)
                if progress_task:
                    progress_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await progress_task
                    progress_task = None
                if progress_message_id > 0:
                    await self._delete_message(chat_id, progress_message_id)
                await self._send_text(
                    chat_id,
                    f"❌ Ответ ИИ в чат завершился с ошибкой:\n{exc}",
                    reply_markup=reply_markup,
                )
            finally:
                if progress_task:
                    progress_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await progress_task
                self._clear_active_command(profile_name)

        active_command.task = asyncio.create_task(runner())

    async def _start_cli_command(
        self,
        chat_id: int,
        principal: dict,
        profile_name: str,
        flag: str,
        label: str,
        timeout: int,
    ) -> None:
        role = principal.get("role", ROLE_USER)
        reply_markup = self._menu_reply_markup(principal)
        if self._has_active_command(profile_name):
            await self._send_busy_status(chat_id, principal, profile_name=profile_name)
            return

        daemon_state = self._daemon_state(profile_name)
        if daemon_state["running"]:
            await self._send_text(
                chat_id,
                f"🟢 Демон уже работает для профиля {profile_name} (pid={daemon_state['pid']}). Сначала остановите его через /daemon_off.",
                reply_markup=reply_markup,
            )
            return

        active_command = self._mark_active_command(principal=principal, label=label, profile_name=profile_name)
        progress_message = await self._send_text(
            chat_id,
            build_progress_text(label, profile_name=profile_name),
            reply_markup=self._menu_reply_markup(principal),
        )
        progress_task: asyncio.Task | None = None
        progress_message_id = int((progress_message or {}).get("message_id") or 0)
        if progress_message and label == "analyze resume":
            if progress_message_id > 0:
                progress_task = asyncio.create_task(
                    self._run_progress_indicator(
                        chat_id=chat_id,
                        message_id=progress_message_id,
                        label=label,
                        profile_name=profile_name,
                        reply_markup=self._menu_reply_markup(principal),
                    )
                )

        async def runner() -> None:
            nonlocal progress_task
            try:
                result = await runtime_control.run_command_capture(
                    runtime_control.agent_command_argv(profile_name, flag),
                    timeout=timeout,
                    on_start=lambda pid: setattr(active_command, "subprocess_pid", pid),
                )
                if active_command.cancel_requested:
                    result["cancelled"] = True
                message = format_command_result(label, result, role=role)
                send_as_document = False
                document_name = ""
                document_content = b""
                if flag == "--analyze-resume" and result.get("ok"):
                    telegram_resume_limits.record_resume_analysis(
                        principal["user_id"],
                        profile_name=profile_name,
                    )
                    document_name, document_content = self._analysis_document_payload(
                        profile_name,
                        (result.get("stdout") or "").strip(),
                    )
                    send_as_document = bool(document_content)
                    message = (
                        "✅ ИИ-анализ резюме готов. Отправляю файл анализа."
                        if send_as_document
                        else "✅ ИИ-анализ резюме готов."
                    )
                if flag in {"--search", "--dry-run"}:
                    last_run = self._latest_run(profile_name)
                    if last_run:
                        message = f"{message}\n\n🕓 Последний прогон:\n{format_run_summary(last_run)}"
                if progress_task:
                    progress_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await progress_task
                    progress_task = None
                if progress_message_id > 0:
                    await self._delete_message(chat_id, progress_message_id)
                if send_as_document:
                    await self._send_document(
                        chat_id,
                        filename=document_name,
                        content=document_content,
                        caption=message,
                        reply_markup=reply_markup,
                    )
                else:
                    await self._send_text(chat_id, message, reply_markup=reply_markup)
            except asyncio.CancelledError:
                if progress_task:
                    progress_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await progress_task
                    progress_task = None
                if progress_message_id > 0:
                    await self._delete_message(chat_id, progress_message_id)
                await self._send_text(
                    chat_id,
                    format_command_result(label, {"cancelled": True}, role=role),
                    reply_markup=reply_markup,
                )
            except Exception as exc:
                log.error("Command %s failed: %s", label, exc, exc_info=True)
                if progress_task:
                    progress_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await progress_task
                    progress_task = None
                if progress_message_id > 0:
                    await self._delete_message(chat_id, progress_message_id)
                await self._send_text(
                    chat_id,
                    f"❌ Команда завершилась с ошибкой: {label}\n{exc}",
                    reply_markup=reply_markup,
                )
            finally:
                if progress_task:
                    progress_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await progress_task
                self._clear_active_command(profile_name)

        active_command.task = asyncio.create_task(runner())

    async def _close_sessions(self) -> None:
        for session in list(self._sessions.values()):
            if session and not session.closed:
                await session.close()
        self._sessions.clear()


async def main() -> None:
    parser = argparse.ArgumentParser(description="Standalone Telegram bot for Job Hunter")
    parser.add_argument(
        "--profile",
        default="default",
        help="Имя профиля для bot-runtime/state файлов; по умолчанию запускай bot из default профиля",
    )
    parser.add_argument(
        "--keep-pending",
        action="store_true",
        help="Не пропускать уже накопленные Telegram updates при первом запуске",
    )
    args = parser.parse_args()

    profile_mod.activate_no_lock(args.profile)
    _configure_logging(force=True)
    bot = TelegramBot(profile_name=args.profile, drop_pending=not args.keep_pending)
    await bot.run()


if __name__ == "__main__":
    asyncio.run(main())
