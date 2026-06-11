"""Telegram bot UI text, reply markup, and command parsing helpers."""
from __future__ import annotations

import json
import os
import re
from collections import deque

import config
import runtime_control
import telegram_access
import telegram_clients
import telegram_resume_limits

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


_EXPORTED_HELPERS = ['_append_active_controls', '_callback_data', '_client_auth_label', '_client_display_name', '_client_status_label', '_command_conflicts_with_active', '_error_excerpt', '_extract_analyze_markdown', '_format_ai_profile_counts', '_format_elapsed', '_format_interval_label', '_format_runtime_block', '_format_users_text', '_normalize_menu', '_normalize_process_runtime', '_ok_icon', '_parse_callback_data', '_parse_chat_action_callback_data', '_parse_chat_ai_callback_data', '_parse_chat_send_callback_data', '_pretty_command_label', '_pretty_pid', '_pretty_profile_name', '_pretty_runtime_action', '_pretty_runtime_mode', '_pretty_runtime_status', '_pretty_value', '_redact_log_text', '_resolve_guest_command', '_resolve_message_command', '_role_label', '_role_title', '_sanitize_analyze_output', '_sanitize_command_output', '_schedule_preset_label', '_status_icon', '_status_label', '_strip_markdown_markup', '_tail_text_file', '_unique_paths', 'build_ai_limits_text', 'build_busy_reply_markup', 'build_busy_status_text', 'build_client_hh_auth_inline_markup', 'build_client_review_inline_markup', 'build_client_status_text', 'build_clients_inline_markup', 'build_clients_text', 'build_guest_reply_markup', 'build_guest_welcome_text', 'build_help_text', 'build_hh_auth_admin_text', 'build_hh_auth_result_text', 'build_hh_resumes_text', 'build_log_text', 'build_menu_section_text', 'build_profiles_text', 'build_progress_text', 'build_reply_markup', 'build_runs_text', 'build_schedule_text', 'build_stats_text', 'build_status_text', 'format_ai_snapshot_text', 'format_command_result', 'format_run_summary', 'normalize_command', 'parse_hh_auth_command_result', 'split_message']

__all__ = [
    name
    for name in globals()
    if name.isupper()
    or name.startswith(("BUTTON_", "MENU_", "CALLBACK_"))
    or name in _EXPORTED_HELPERS
]
