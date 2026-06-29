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
from dataclasses import dataclass
from datetime import datetime

import aiohttp

import analytics
import client_hh_auth
import manual_apply_queue
import config
import profile as profile_mod
import runtime_control
import seen
import telegram_access
import telegram_clients
import telegram_resume_limits
from telegram_bot_ui import *  # re-export UI helpers for existing imports/tests

log = logging.getLogger("telegram_bot")


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


def _parse_manual_chat_ai_arg(raw: str) -> tuple[str, str]:
    text = (raw or "").strip()
    if not text:
        return "", ""

    chat_id = ""
    message_id = ""

    chat_match = re.search(r"(?:chatik\.hh\.ru|hh\.ru)?/chat/(\d{6,})", text, re.I)
    if chat_match:
        chat_id = chat_match.group(1)

    if not chat_id:
        chat_param = re.search(r"(?:chat_id|chatId|chat)=([0-9]{6,})", text, re.I)
        if chat_param:
            chat_id = chat_param.group(1)

    msg_param = re.search(r"(?:message_id|messageId|msg|mid)=([0-9]{6,})", text, re.I)
    if msg_param:
        message_id = msg_param.group(1)

    numbers = re.findall(r"\d{6,}", text)
    if not chat_id and numbers:
        chat_id = numbers[0]
    if not message_id:
        for number in numbers:
            if number != chat_id:
                message_id = number
                break

    return chat_id, message_id


def _safe_chat_callback_profile(profile_name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", profile_name or "default")


def _chat_ai_candidate_callback_data(profile_name: str, candidate: dict) -> str:
    action = CALLBACK_CHAT_AI_MANUAL_REPLY if candidate.get("allow_any") else CALLBACK_CHAT_AI_REPLY
    chat_id = str(candidate.get("chat_id") or "")
    message_id = str(candidate.get("message_id") or "")
    return f"{action}:{_safe_chat_callback_profile(profile_name)}:{chat_id}:{message_id}"


def _parse_chat_candidates_stdout(stdout: str) -> dict:
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        summary = payload.get("chat_candidates")
        if isinstance(summary, dict):
            return summary
    return {}


def _build_chat_ai_candidates_text(summary: dict) -> str:
    if summary.get("ok") is False:
        url = str(summary.get("url") or "")
        if "account/login" in url:
            return "HH просит заново войти в аккаунт, поэтому список чатов сейчас недоступен. Нажми `Вход HH`, обнови сессию и повтори `Ответ ИИ в чат`."
        message = str(summary.get("message") or "не удалось открыть chatik")
        return f"Не удалось получить список HH-чатов: {message}"
    candidates = list(summary.get("candidates") or [])
    scanned = int(summary.get("chats_scanned") or 0)
    read = int(summary.get("chats_read") or 0)
    if not candidates:
        return (
            "Не нашёл свежих входящих HH-сообщений для ручного ИИ-ответа.\n"
            f"Проверено чатов: {read}/{scanned}."
        )
    lines = [
        "Выбери HH-чат для ИИ-предпросмотра.",
        f"Проверено чатов: {read}/{scanned}.",
    ]
    for idx, item in enumerate(candidates, 1):
        title = str(item.get("title") or "—")[:120]
        company = str(item.get("company") or "—")[:80]
        kind = str(item.get("kind_label") or item.get("kind") or "HR")[:40]
        author = str(item.get("author") or "HR")[:80]
        question = str(item.get("question") or "").replace("\n", " ")[:220]
        marker = " 📝 анкета" if item.get("google_form_urls") else ""
        lines.append(f"\n{idx}. [{kind}]{marker} {title} @ {company}\n{author}: {question}")
    return "\n".join(lines)[:3900]


def _build_chat_ai_candidates_markup(profile_name: str, summary: dict) -> dict | None:
    rows = []
    for idx, item in enumerate(list(summary.get("candidates") or [])[:8], 1):
        chat_id = str(item.get("chat_id") or "")
        message_id = str(item.get("message_id") or "")
        if not chat_id or not message_id:
            continue
        callback_data = _chat_ai_candidate_callback_data(profile_name, item)
        row = []
        if len(callback_data.encode("utf-8")) <= 64:
            row.append({"text": f"🤖 {idx}", "callback_data": callback_data})
        if item.get("google_form_urls"):
            try:
                import google_form_filler as gforms
                form_callback = gforms.google_form_preview_callback_data(profile_name, chat_id, message_id)
            except Exception:
                form_callback = ""
            if form_callback and len(form_callback.encode("utf-8")) <= 64:
                row.append({"text": f"📝 Анкета {idx}", "callback_data": form_callback})
        row.append({"text": f"Открыть {idx}", "url": f"https://chatik.hh.ru/chat/{chat_id}"})
        rows.append(row)
    return {"inline_keyboard": rows} if rows else None


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

    def _append_chat_ai_audit_event(self, event: str, **payload: object) -> None:
        path = getattr(config, "TELEGRAM_BOT_DEBUG_LOG_FILE", "") or ""
        if not path:
            return
        record = {
            "event": event,
            "profile": self.profile_name,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            **payload,
        }
        try:
            import json

            parent = os.path.dirname(path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            with open(path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:
            log.warning("chat AI audit write failed: %s", exc)

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

    async def _send_text_safely(self, chat_id: int, text: str, *, reply_markup: dict | None = None) -> dict | None:
        try:
            return await self._send_text(chat_id, text, reply_markup=reply_markup)
        except Exception as exc:
            log.warning("Telegram sendMessage failed chat=%s: %s", chat_id, exc)
            return None

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

        if raw_data.startswith(f"{CALLBACK_HH_REAUTH}:"):
            profile_name = _parse_hh_reauth_callback_data(raw_data)
            if not profile_name or profile_name not in self._profile_names():
                await self._answer_callback_query(callback_id, "Не удалось определить профиль.", show_alert=True)
                return
            await self._answer_callback_query(callback_id, "Запускаю вход HH…")
            if message_id > 0:
                await self._edit_reply_markup(chat_id, message_id)
            await self._start_profile_hh_auth_capture(chat_id, principal, profile_name=profile_name)
            return

        if raw_data.startswith("gform_preview:"):
            import google_form_filler as gforms
            profile_name, hh_chat_id, hh_message_id = gforms.parse_google_form_preview_callback_data(raw_data)
            if not profile_name or profile_name not in self._profile_names():
                await self._answer_callback_query(callback_id, "Не удалось определить профиль.", show_alert=True)
                return
            await self._answer_callback_query(callback_id, "Готовлю Google Form preview…")
            if message_id > 0:
                await self._edit_reply_markup(chat_id, message_id)
            await self._start_google_form_preview(
                chat_id,
                principal,
                profile_name=profile_name,
                hh_chat_id=hh_chat_id,
                hh_message_id=hh_message_id,
            )
            return

        if raw_data.startswith("gform_submit:"):
            import google_form_filler as gforms
            profile_name, token = gforms.parse_google_form_submit_callback_data(raw_data)
            if not profile_name or profile_name not in self._profile_names() or not token:
                await self._answer_callback_query(callback_id, "Не удалось определить форму.", show_alert=True)
                return
            await self._answer_callback_query(callback_id, "Отправляю Google Form…")
            if message_id > 0:
                await self._edit_reply_markup(chat_id, message_id)
            await self._start_google_form_submit(
                chat_id,
                principal,
                profile_name=profile_name,
                token=token,
            )
            return

        if raw_data.startswith(f"{CALLBACK_CHAT_AI_MANUAL_REPLY}:"):
            profile_name, hh_chat_id, hh_message_id = _parse_chat_ai_manual_callback_data(raw_data)
            if not profile_name or profile_name not in self._profile_names():
                await self._answer_callback_query(callback_id, "Не удалось определить профиль.", show_alert=True)
                return
            self._append_chat_ai_audit_event(
                "callback",
                action="preview",
                telegram_chat_id=chat_id,
                telegram_message_id=message_id,
                user_id=user_id,
                profile_name=profile_name,
                hh_chat_id=hh_chat_id,
                hh_message_id=hh_message_id,
                allow_any=True,
            )
            await self._answer_callback_query(callback_id, "Генерирую ответ через ИИ…")
            if message_id > 0:
                await self._edit_reply_markup(chat_id, message_id)
            await self._start_chat_ai_reply(
                chat_id,
                principal,
                profile_name=profile_name,
                hh_chat_id=hh_chat_id,
                hh_message_id=hh_message_id,
                allow_any=True,
            )
            return

        if raw_data.startswith(f"{CALLBACK_CHAT_AI_MANUAL_SEND}:"):
            profile_name, hh_chat_id, hh_message_id = _parse_chat_manual_send_callback_data(raw_data)
            if not profile_name or profile_name not in self._profile_names():
                await self._answer_callback_query(callback_id, "Не удалось определить профиль.", show_alert=True)
                return
            self._append_chat_ai_audit_event(
                "callback",
                action="send",
                telegram_chat_id=chat_id,
                telegram_message_id=message_id,
                user_id=user_id,
                profile_name=profile_name,
                hh_chat_id=hh_chat_id,
                hh_message_id=hh_message_id,
                allow_any=True,
            )
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
                allow_any=True,
            )
            return

        if raw_data.startswith(f"{CALLBACK_CHAT_AI_REPLY}:"):
            profile_name, hh_chat_id, hh_message_id = _parse_chat_ai_callback_data(raw_data)
            if not profile_name or profile_name not in self._profile_names():
                await self._answer_callback_query(callback_id, "Не удалось определить профиль.", show_alert=True)
                return
            self._append_chat_ai_audit_event(
                "callback",
                action="preview",
                telegram_chat_id=chat_id,
                telegram_message_id=message_id,
                user_id=user_id,
                profile_name=profile_name,
                hh_chat_id=hh_chat_id,
                hh_message_id=hh_message_id,
            )
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
            self._append_chat_ai_audit_event(
                "callback",
                action="send",
                telegram_chat_id=chat_id,
                telegram_message_id=message_id,
                user_id=user_id,
                profile_name=profile_name,
                hh_chat_id=hh_chat_id,
                hh_message_id=hh_message_id,
            )
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

        if raw_data.startswith(f"{CALLBACK_MANUAL_APPLY}:"):
            profile_name, token = _parse_manual_apply_callback_data(raw_data)
            if not profile_name or profile_name not in self._profile_names() or not token:
                await self._answer_callback_query(callback_id, "Не удалось определить вакансию.", show_alert=True)
                return
            await self._answer_callback_query(callback_id, "Отправляю отклик через ИИ…")
            if message_id > 0:
                await self._edit_reply_markup(chat_id, message_id)
            await self._start_manual_ai_apply(
                chat_id,
                principal,
                profile_name=profile_name,
                token=token,
            )
            return

        if raw_data.startswith(f"{CALLBACK_MANUAL_FEEDBACK}:"):
            profile_name, token, feedback = _parse_manual_feedback_callback_data(raw_data)
            if not profile_name or profile_name not in self._profile_names() or not token:
                await self._answer_callback_query(callback_id, "Не удалось определить вакансию.", show_alert=True)
                return
            item = manual_apply_queue.record_feedback(token, feedback, user_id=user_id)
            if not item:
                await self._answer_callback_query(callback_id, "Не удалось записать оценку.", show_alert=True)
                return
            label = manual_apply_queue.feedback_label(feedback)
            await self._answer_callback_query(callback_id, f"Записал: {label}")
            if message_id > 0:
                if feedback == "good":
                    reply_markup = manual_apply_queue.build_manual_apply_markup(
                        item.get("vacancy") or {},
                        profile_name,
                        token,
                        include_feedback=False,
                    )
                    await self._edit_reply_markup(chat_id, message_id, reply_markup=reply_markup)
                else:
                    await self._edit_reply_markup(chat_id, message_id)
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
            if not client and role == ROLE_ADMIN:
                await self._start_profile_hh_auth_capture(chat_id, principal, profile_name=profile_name)
                return
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

        if command in {"/chat_ai", "/ai_chat", "/chat_answer"}:
            hh_chat_id, hh_message_id = _parse_manual_chat_ai_arg(arg)
            if not hh_chat_id:
                await self._start_chat_ai_candidate_list(
                    chat_id,
                    principal,
                    profile_name=profile_name,
                )
                return
            self._append_chat_ai_audit_event(
                "manual_command",
                action="preview",
                telegram_chat_id=chat_id,
                user_id=int(principal.get("user_id") or 0),
                profile_name=profile_name,
                hh_chat_id=hh_chat_id,
                hh_message_id=hh_message_id,
                allow_any=True,
            )
            await self._start_chat_ai_reply(
                chat_id,
                principal,
                profile_name=profile_name,
                hh_chat_id=hh_chat_id,
                hh_message_id=hh_message_id,
                allow_any=True,
            )
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

    async def _start_profile_hh_auth_capture(
        self,
        chat_id: int,
        principal: dict,
        *,
        profile_name: str,
    ) -> None:
        role = principal.get("role", ROLE_USER)
        reply_markup = self._menu_reply_markup(principal)
        if profile_name not in self._profile_names():
            await self._send_text(chat_id, f"❌ Неизвестный профиль: {profile_name}", reply_markup=reply_markup)
            return
        if self._has_active_command(profile_name):
            await self._send_busy_status(chat_id, principal, profile_name=profile_name)
            return

        label = "hh auth capture"
        active_command = self._mark_active_command(principal=principal, label=label, profile_name=profile_name)
        self._append_debug_log(
            "profile_hh_auth_capture_started",
            admin_user_id=principal.get("user_id"),
            profile_name=profile_name,
            argv=runtime_control.client_hh_auth_command_argv(profile_name, timeout_sec=900),
        )
        progress_message = await self._send_text(
            chat_id,
            "🔐 Запускаю вход HH для выбранного профиля. Откроется браузер; войди в HH, дальше cookies сохранятся автоматически.",
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
                command_result = await runtime_control.run_command_capture(
                    runtime_control.client_hh_auth_command_argv(profile_name, timeout_sec=900),
                    timeout=1800,
                    on_start=lambda pid: setattr(active_command, "subprocess_pid", pid),
                )
                if active_command.cancel_requested:
                    command_result["cancelled"] = True
                self._append_debug_log(
                    "profile_hh_auth_capture_command_result",
                    admin_user_id=principal.get("user_id"),
                    profile_name=profile_name,
                    command_result=command_result,
                )
                result = parse_hh_auth_command_result(command_result, profile_name=profile_name)
                self._append_debug_log(
                    "profile_hh_auth_capture_result",
                    admin_user_id=principal.get("user_id"),
                    profile_name=profile_name,
                    result=result,
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
                    build_hh_auth_result_text(result),
                    reply_markup=reply_markup,
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
                    build_hh_auth_result_text({"cancelled": True, "profile_name": profile_name}),
                    reply_markup=reply_markup,
                )
            except Exception as exc:
                log.error("Profile HH auth capture failed: %s", exc, exc_info=True)
                self._append_debug_log(
                    "profile_hh_auth_capture_exception",
                    admin_user_id=principal.get("user_id"),
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
                    f"❌ Вход HH завершился с ошибкой:\n{exc}",
                    reply_markup=reply_markup,
                )
            finally:
                self._clear_active_command(profile_name)

        active_command.task = asyncio.create_task(runner())

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

    async def _start_google_form_preview(
        self,
        chat_id: int,
        principal: dict,
        *,
        profile_name: str,
        hh_chat_id: str,
        hh_message_id: str,
    ) -> None:
        await self._start_google_form_command(
            chat_id,
            principal,
            profile_name=profile_name,
            label="google form preview",
            argv=runtime_control.agent_command_argv(
                profile_name,
                "--google-form-preview",
                hh_chat_id,
                "--chat-message-id",
                hh_message_id,
            ),
        )

    async def _start_google_form_submit(
        self,
        chat_id: int,
        principal: dict,
        *,
        profile_name: str,
        token: str,
    ) -> None:
        await self._start_google_form_command(
            chat_id,
            principal,
            profile_name=profile_name,
            label="google form submit",
            argv=runtime_control.agent_command_argv(profile_name, "--google-form-submit", token),
        )

    async def _start_google_form_command(
        self,
        chat_id: int,
        principal: dict,
        *,
        profile_name: str,
        label: str,
        argv: list[str],
    ) -> None:
        role = principal.get("role", ROLE_USER)
        reply_markup = self._menu_reply_markup(principal)
        if self._has_active_command(profile_name):
            await self._send_busy_status(chat_id, principal, profile_name=profile_name)
            return

        active_command = self._mark_active_command(principal=principal, label=label, profile_name=profile_name)
        progress_message = await self._send_text(
            chat_id,
            build_progress_text(label, profile_name=profile_name),
            reply_markup=self._menu_reply_markup(principal),
        )
        progress_message_id = int((progress_message or {}).get("message_id") or 0)

        async def runner() -> None:
            try:
                result = await runtime_control.run_command_capture(
                    argv,
                    timeout=1800,
                    on_start=lambda pid: setattr(active_command, "subprocess_pid", pid),
                )
                if active_command.cancel_requested:
                    result["cancelled"] = True
                if progress_message_id > 0:
                    await self._delete_message(chat_id, progress_message_id)
                await self._send_text(
                    chat_id,
                    format_command_result(label, result, role=role),
                    reply_markup=reply_markup,
                )
            except asyncio.CancelledError:
                if progress_message_id > 0:
                    await self._delete_message(chat_id, progress_message_id)
                await self._send_text(
                    chat_id,
                    format_command_result(label, {"cancelled": True}, role=role),
                    reply_markup=reply_markup,
                )
            except Exception as exc:
                log.error("Google Form command failed: %s", exc, exc_info=True)
                if progress_message_id > 0:
                    await self._delete_message(chat_id, progress_message_id)
                await self._send_text(
                    chat_id,
                    f"❌ Google Form команда завершилась с ошибкой:\n{exc}",
                    reply_markup=reply_markup,
                )
            finally:
                self._clear_active_command(profile_name)

        active_command.task = asyncio.create_task(runner())

    async def _start_chat_ai_candidate_list(
        self,
        chat_id: int,
        principal: dict,
        *,
        profile_name: str,
    ) -> None:
        role = principal.get("role", ROLE_USER)
        reply_markup = self._menu_reply_markup(principal)
        if self._has_active_command(profile_name):
            await self._send_busy_status(chat_id, principal, profile_name=profile_name)
            return

        label = "chat AI list"
        active_command = self._mark_active_command(principal=principal, label=label, profile_name=profile_name)
        progress_message = await self._send_text_safely(
            chat_id,
            "Смотрю последние HH-чаты и ищу входящие сообщения для ИИ-предпросмотра…",
            reply_markup=self._menu_reply_markup(principal),
        )
        progress_message_id = int((progress_message or {}).get("message_id") or 0)

        async def runner() -> None:
            try:
                argv = runtime_control.agent_command_argv(
                    profile_name,
                    "--chat-list-candidates",
                    "--chat-list-limit",
                    "8",
                    "--chat-list-max-scan",
                    "25",
                )
                self._append_chat_ai_audit_event(
                    "candidate_list_start",
                    profile_name=profile_name,
                    telegram_chat_id=chat_id,
                    user_id=int(principal.get("user_id") or 0),
                )
                result = await runtime_control.run_command_capture(
                    argv,
                    timeout=900,
                    on_start=lambda pid: setattr(active_command, "subprocess_pid", pid),
                )
                if active_command.cancel_requested:
                    result["cancelled"] = True
                if progress_message_id > 0:
                    await self._delete_message(chat_id, progress_message_id)
                if not result.get("ok") or result.get("cancelled"):
                    await self._send_text_safely(
                        chat_id,
                        format_command_result(label, result, role=role),
                        reply_markup=reply_markup,
                    )
                    return
                summary = _parse_chat_candidates_stdout(result.get("stdout") or "")
                self._append_chat_ai_audit_event(
                    "candidate_list_result",
                    profile_name=profile_name,
                    telegram_chat_id=chat_id,
                    ok=bool(summary),
                    candidates=len(summary.get("candidates") or []) if summary else 0,
                    chats_scanned=summary.get("chats_scanned") if summary else None,
                    chats_read=summary.get("chats_read") if summary else None,
                    read_failures=summary.get("read_failures") if summary else None,
                    stderr=(result.get("stderr") or "")[-1200:],
                )
                if not summary:
                    await self._send_text_safely(
                        chat_id,
                        "Не смог разобрать список HH-чатов. Посмотри /chat_log или /log.",
                        reply_markup=reply_markup,
                    )
                    return
                await self._send_text_safely(
                    chat_id,
                    _build_chat_ai_candidates_text(summary),
                    reply_markup=_build_chat_ai_candidates_markup(profile_name, summary) or reply_markup,
                )
            except asyncio.CancelledError:
                if progress_message_id > 0:
                    await self._delete_message(chat_id, progress_message_id)
                await self._send_text_safely(
                    chat_id,
                    format_command_result(label, {"cancelled": True}, role=role),
                    reply_markup=reply_markup,
                )
            except Exception as exc:
                self._append_chat_ai_audit_event(
                    "candidate_list_exception",
                    profile_name=profile_name,
                    telegram_chat_id=chat_id,
                    error=str(exc),
                )
                log.error("Chat AI candidate list failed: %s", exc, exc_info=True)
                if progress_message_id > 0:
                    await self._delete_message(chat_id, progress_message_id)
                await self._send_text_safely(
                    chat_id,
                    f"❌ Список HH-чатов для ИИ-ответа завершился с ошибкой:\n{exc}",
                    reply_markup=reply_markup,
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
        allow_any: bool = False,
    ) -> None:
        role = principal.get("role", ROLE_USER)
        reply_markup = self._menu_reply_markup(principal)
        if self._has_active_command(profile_name):
            await self._send_busy_status(chat_id, principal, profile_name=profile_name)
            return

        label = "chat AI send" if force_send else "chat AI reply"
        active_command = self._mark_active_command(principal=principal, label=label, profile_name=profile_name)
        progress_message = await self._send_text_safely(
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
                if allow_any:
                    argv.append("--chat-allow-any")
                if force_send:
                    argv.append("--chat-force-send")
                self._append_chat_ai_audit_event(
                    "command_start",
                    action="send" if force_send else "preview",
                    profile_name=profile_name,
                    hh_chat_id=hh_chat_id,
                    hh_message_id=hh_message_id,
                    force_send=force_send,
                    allow_any=allow_any,
                )
                result = await runtime_control.run_command_capture(
                    argv,
                    timeout=1200,
                    on_start=lambda pid: setattr(active_command, "subprocess_pid", pid),
                )
                if active_command.cancel_requested:
                    result["cancelled"] = True
                self._append_chat_ai_audit_event(
                    "command_result",
                    action="send" if force_send else "preview",
                    profile_name=profile_name,
                    hh_chat_id=hh_chat_id,
                    hh_message_id=hh_message_id,
                    force_send=force_send,
                    allow_any=allow_any,
                    ok=bool(result.get("ok")),
                    returncode=result.get("returncode"),
                    timeout=bool(result.get("timeout")),
                    cancelled=bool(result.get("cancelled")),
                    stdout=(result.get("stdout") or "")[-2000:],
                    stderr=(result.get("stderr") or "")[-1200:],
                )
                message = format_command_result(label, result, role=role)
                if progress_task:
                    progress_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await progress_task
                    progress_task = None
                if progress_message_id > 0:
                    await self._delete_message(chat_id, progress_message_id)
                try:
                    sent_message = await self._send_text(chat_id, message, reply_markup=reply_markup)
                    self._append_chat_ai_audit_event(
                        "delivery_ok",
                        action="send" if force_send else "preview",
                        profile_name=profile_name,
                        hh_chat_id=hh_chat_id,
                        hh_message_id=hh_message_id,
                        force_send=force_send,
                        allow_any=allow_any,
                        telegram_message_id=int((sent_message or {}).get("message_id") or 0),
                    )
                except Exception as exc:
                    self._append_chat_ai_audit_event(
                        "delivery_failed",
                        action="send" if force_send else "preview",
                        profile_name=profile_name,
                        hh_chat_id=hh_chat_id,
                        hh_message_id=hh_message_id,
                        force_send=force_send,
                        allow_any=allow_any,
                        cli_ok=bool(result.get("ok")),
                        error=str(exc),
                    )
                    log.warning("Telegram delivery failed for Chat AI reply: %s", exc)
            except asyncio.CancelledError:
                self._append_chat_ai_audit_event(
                    "command_cancelled",
                    action="send" if force_send else "preview",
                    profile_name=profile_name,
                    hh_chat_id=hh_chat_id,
                    hh_message_id=hh_message_id,
                    force_send=force_send,
                    allow_any=allow_any,
                )
                if progress_task:
                    progress_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await progress_task
                    progress_task = None
                if progress_message_id > 0:
                    await self._delete_message(chat_id, progress_message_id)
                await self._send_text_safely(
                    chat_id,
                    format_command_result(label, {"cancelled": True}, role=role),
                    reply_markup=reply_markup,
                )
            except Exception as exc:
                self._append_chat_ai_audit_event(
                    "command_exception",
                    action="send" if force_send else "preview",
                    profile_name=profile_name,
                    hh_chat_id=hh_chat_id,
                    hh_message_id=hh_message_id,
                    force_send=force_send,
                    allow_any=allow_any,
                    error=str(exc),
                )
                log.error("Chat AI reply failed: %s", exc, exc_info=True)
                if progress_task:
                    progress_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await progress_task
                    progress_task = None
                if progress_message_id > 0:
                    await self._delete_message(chat_id, progress_message_id)
                await self._send_text_safely(
                    chat_id,
                    f"❌ Ответ ИИ в чат завершился с ошибкой:\n{exc}",
                    reply_markup=reply_markup,
                )
            finally:
                if progress_task:
                    progress_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await progress_task
                self._clear_active_command(profile_name)

        active_command.task = asyncio.create_task(runner())

    async def _start_manual_ai_apply(
        self,
        chat_id: int,
        principal: dict,
        *,
        profile_name: str,
        token: str,
    ) -> None:
        role = principal.get("role", ROLE_USER)
        reply_markup = self._menu_reply_markup(principal)
        if self._has_active_command(profile_name):
            await self._send_busy_status(chat_id, principal, profile_name=profile_name)
            return

        label = "manual AI apply"
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
                result = await runtime_control.run_command_capture(
                    runtime_control.agent_command_argv(profile_name, "--manual-apply-token", token),
                    timeout=1800,
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
                log.error("Manual AI apply failed: %s", exc, exc_info=True)
                if progress_task:
                    progress_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await progress_task
                    progress_task = None
                if progress_message_id > 0:
                    await self._delete_message(chat_id, progress_message_id)
                await self._send_text(
                    chat_id,
                    f"❌ Ручной ИИ-отклик завершился с ошибкой:\n{exc}",
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
