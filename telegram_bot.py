#!/usr/bin/env python3
"""Standalone Telegram bot for Job Hunter control and status."""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import html
import json
import logging
import os
import re
import signal
import sys
import time
import traceback
from collections.abc import Callable
from datetime import datetime

import analytics
import candidate_interview
import admin_llm
import client_hh_auth
import hh_response_counter
import manual_apply_queue
import config
from private_logging import PrivateFileHandler, OperationalFormatter, chat_log_path
from state_store.private_journal import append_json, read_json_records
import company_blacklist
import profile as profile_mod
import runtime_control
import search_query_suggester
import seen
import telegram_access
import telegram_clients
import telegram_resume_limits
from runtime_context import TelegramRuntimePaths
from state_store.json_store import JsonStore
from state_store.bot_state import BotStateStore
from telegram_app.auth_bridge import (
    TelegramHHAuthBridge,
    can_answer_hh_auth_prompt as _can_answer_hh_auth_prompt,
    clean_hh_auth_code_text as _clean_hh_auth_code_text,
    looks_like_standalone_hh_auth_code as _looks_like_standalone_hh_auth_code,
)
from telegram_app.api import TelegramAPIClient, TelegramAPIError, telegram_error_summary
from telegram_app.callbacks import TelegramCallbackRouter
from telegram_app.forms import TelegramFormEditor
from telegram_app.subprocesses import (
    ActiveCommandState,
    TelegramSubprocessManager,
)
from telegram_bot_ui import *  # re-export UI helpers for existing imports/tests

log = logging.getLogger("telegram_bot")


def _telegram_runtime_paths() -> TelegramRuntimePaths:
    return TelegramRuntimePaths.from_config(config)


def _build_logging_handlers(
    runtime_paths: TelegramRuntimePaths | None = None,
) -> list[logging.Handler]:
    paths = runtime_paths or _telegram_runtime_paths()
    # The background launcher redirects stdout/stderr to the bot log. Avoid
    # duplicating every record through a StreamHandler in that same file.
    handlers: list[logging.Handler] = []
    if not os.environ.get("JOB_HUNTER_BACKGROUND") and sys.stdout.isatty():
        handlers.append(logging.StreamHandler())
    if paths.bot_log_file:
        log_dir = os.path.dirname(paths.bot_log_file)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        handlers.append(PrivateFileHandler(paths.bot_log_file, channel="bot"))
    if paths.search_log_file:
        handlers.append(PrivateFileHandler(paths.search_log_file, channel="search"))
        handlers.append(PrivateFileHandler(chat_log_path(paths.search_log_file), channel="chat"))
    return handlers


def _configure_logging(
    force: bool = False,
    runtime_paths: TelegramRuntimePaths | None = None,
) -> None:
    if not force and logging.getLogger().handlers:
        return
    handlers = _build_logging_handlers(runtime_paths)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=handlers,
        force=force,
    )
    for handler in handlers:
        handler.setFormatter(OperationalFormatter())
    logging.getLogger("chat_responder").propagate = True


_configure_logging()


def _is_menu_button_text(text: str) -> bool:
    return text in ADMIN_BUTTON_MAP or text in USER_BUTTON_MAP or text in LEGACY_BUTTON_MAP


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


class TelegramBot(
    TelegramAPIClient,
    TelegramSubprocessManager,
    TelegramHHAuthBridge,
    TelegramCallbackRouter,
    TelegramFormEditor,
):
    def __init__(
        self,
        profile_name: str,
        drop_pending: bool = True,
        *,
        runtime_paths: TelegramRuntimePaths | None = None,
    ):
        TelegramAPIClient.__init__(self, logger=log)
        TelegramSubprocessManager.__init__(self, admin_role=ROLE_ADMIN)
        TelegramHHAuthBridge.__init__(
            self,
            admin_role=ROLE_ADMIN,
            is_menu_button=_is_menu_button_text,
            logger=log,
        )
        self.profile_name = profile_name
        self.drop_pending = drop_pending
        self.runtime_paths = runtime_paths or _telegram_runtime_paths()
        self._stop_event = asyncio.Event()
        self._poll_health = {"status": "starting"}
        self._last_poll_runtime_at: float | None = None
        self._llm_test_next_at = 0.0
        self._llm_test_lock = asyncio.Lock()

    async def run(self) -> None:
        if not config.TELEGRAM_CONTROL_BOT_TOKEN:
            raise RuntimeError("Telegram bot requires HUNTER_CONTROL_BOT_TOKEN")

        telegram_access.load_registry()
        runtime_control.register_current_process(
            self.runtime_paths.bot_pid_file,
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
                    self._record_poll_result(exc)
                    await asyncio.sleep(5)
                    continue

                self._record_poll_result()
                for update in updates:
                    await self._handle_update(update)
                await self._maybe_send_daily_summary()
                try:
                    await self._maybe_send_health_alert()
                except Exception as exc:
                    log.warning("health check failed: %s", telegram_error_summary(exc))
        finally:
            self._poll_health = {**self._poll_health, "status": "stopped"}
            self._write_runtime("bot_stop", "Бот Telegram остановлен", "offline")
            runtime_control.unregister_current_process(self.runtime_paths.bot_pid_file)
            await self._close_sessions()

    def _load_state(self) -> dict:
        return BotStateStore(self.runtime_paths.bot_state_file).load()

    def _save_state(self, state: dict) -> None:
        """Explicit full replacement; application mutations use _update_state."""
        BotStateStore(self.runtime_paths.bot_state_file).save(state)

    def _update_state(self, mutator: Callable[[dict], dict | None]) -> dict:
        return BotStateStore(self.runtime_paths.bot_state_file).update(mutator)

    def _update_user_state(self, section: str, user_id: int, *, fields: dict | None = None, remove_keys: tuple = ()) -> dict:
        def mutate(state):
            entry = state.setdefault(section, {}).setdefault(str(user_id), {})
            entry.update(fields or {})
            for key in remove_keys:
                entry.pop(key, None)
            entry["updated_at"] = datetime.now().isoformat(timespec="seconds")

        state = self._update_state(mutate)
        return dict(state[section][str(user_id)])

    def _write_runtime(self, action: str, message: str, status: str) -> None:
        runtime_control.write_json_file(
            self.runtime_paths.bot_runtime_file,
            {
                "action": action,
                "message": message,
                "status": status,
                "pid": os.getpid(),
                "profile": self.profile_name,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
                "polling": dict(self._poll_health),
            },
        )

    def _record_poll_result(self, error: Exception | None = None) -> None:
        """Publish bounded diagnostic heartbeats, never raw transport payloads.

        A diagnostic write failure must not discard updates already fetched.
        The successful-poll throttle is monotonic; recovery bypasses it.
        """
        if self._stop_event.is_set():
            return
        now = time.monotonic()
        previous = self._poll_health
        if error is None and previous["status"] == "ok" and self._last_poll_runtime_at is not None:
            if now - self._last_poll_runtime_at < 30:
                return
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        if error is None:
            self._poll_health = {**previous, "status": "ok", "last_success_at": timestamp}
        else:
            error_type = type(error).__name__
            api_status = error.status if isinstance(error, TelegramAPIError) else None
            self._poll_health = {**previous, "status": "error", "last_error_at": timestamp,
                                 "last_error_type": error_type, "last_error_status": api_status}
            summary = telegram_error_summary(error)
            log.error("Failed to fetch updates: %s", summary)
        try:
            if error is None:
                # Preserve busy status while an agent command is still active.
                self._sync_active_runtime()
            else:
                self._write_runtime("bot_poll_error", f"Ошибка опроса: {summary}", "error")
        except Exception as exc:
            if error is None:
                self._poll_health = previous
            # An observed network failure remains in memory even if its
            # diagnostic publication failed (possibly after replacement).
            # The next recovery must bypass the successful-heartbeat throttle.
            log.warning("Telegram polling diagnostic write failed: %s", type(exc).__name__)
            return
        self._last_poll_runtime_at = now
        if error is None and previous["status"] == "error":
            log.info("Telegram polling recovered")

    def _append_chat_ai_audit_event(self, event: str, **payload: object) -> None:
        path = self.runtime_paths.bot_debug_log_file
        if not path:
            return
        record = {
            "event": event,
            "profile": self.profile_name,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            **payload,
        }
        try:
            append_json(path, record)
        except Exception as exc:
            log.warning("chat AI audit write failed: %s", type(exc).__name__)

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

        result = self._request_active_command_cancel(command)
        if result is not None:
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
        self._update_user_state("user_state", user_id, fields={"selected_profile": profile_name})

    def _selected_menu(self, principal: dict) -> str:
        role = principal.get("role", ROLE_USER)
        state = self._load_state()
        selected = (((state.get("user_state") or {}).get(str(principal["user_id"])) or {}).get("menu") or "").strip()
        return _normalize_menu(role, selected or MENU_MAIN)

    def _set_selected_menu(self, user_id: int, menu: str) -> None:
        self._update_user_state("user_state", user_id, fields={"menu": menu})

    def _guest_state(self, user_id: int) -> dict:
        state = self._load_state()
        return dict((state.get("guest_state") or {}).get(str(user_id)) or {})

    def _set_guest_state(self, user_id: int, **fields: object) -> dict:
        return self._update_user_state("guest_state", user_id, fields=fields)

    def _clear_guest_state(self, user_id: int) -> None:
        def mutate(state):
            state.setdefault("guest_state", {}).pop(str(user_id), None)

        self._update_state(mutate)

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
            return [chat_log_path(profile.log_file)] if profile.log_file else []
        return [profile.log_file] if profile.log_file else []

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

    async def _send_run_summary(self, chat_id, principal, *, profile_name, run_id=None):
        profile = self._profile(profile_name)
        try:
            summary = analytics.get_run_summary(run_id, events_file=profile.analytics_events_file,
                history_file=profile.run_history_file, profile=profile_name)
            text = build_run_summary_text(summary, profile_name=profile_name)
        except OSError:
            text = "📊 Запуск: данные недоступны. Попробуйте /raw_log."
        await self._send_text(chat_id, text, reply_markup=self._menu_reply_markup(principal))

    def _update_profile_schedule(self, profile_name: str, *, search_interval_min: int, invite_check_interval_min: int | None = None) -> str:
        invite_minutes = search_interval_min if invite_check_interval_min is None else invite_check_interval_min
        return profile_mod.update_profile_env(
            profile_name,
            {
                "SEARCH_INTERVAL_MIN": int(search_interval_min),
                "INVITE_CHECK_INTERVAL_MIN": int(invite_minutes),
            },
        )

    def _search_settings_state(self, user_id: int) -> dict:
        state = self._load_state()
        return dict(((state.get("user_state") or {}).get(str(user_id)) or {}))

    def _set_search_settings_state(self, user_id: int, **fields: object) -> dict:
        return self._update_user_state("user_state", user_id, fields=fields)

    def _clear_search_settings_state(self, user_id: int, *keys: str) -> None:
        self._update_user_state("user_state", user_id, remove_keys=keys)

    def _candidate_profile_dir(self, profile_name: str) -> str:
        return os.path.dirname(self._profile(profile_name).resume_file)

    def _candidate_state(self, user_id: int, profile_name: str) -> dict:
        state = self._search_settings_state(user_id).get("candidate_interview") or {}
        return dict(state) if state.get("profile_name") == profile_name else {}

    def _set_candidate_state(self, user_id: int, state: dict) -> None:
        self._set_search_settings_state(user_id, candidate_interview=state)

    def _clear_candidate_state(self, user_id: int) -> None:
        self._clear_search_settings_state(user_id, "candidate_interview")

    def _candidate_confirm_markup(self) -> dict:
        return {
            "keyboard": [
                [{"text": BUTTON_CANDIDATE_SAVE}, {"text": BUTTON_CANDIDATE_EDIT}],
                [{"text": BUTTON_CANDIDATE_SKIP}, {"text": BUTTON_BACK}],
            ],
            "resize_keyboard": True,
            "is_persistent": True,
        }

    async def _send_candidate_question(self, chat_id: int, principal: dict, profile_name: str) -> None:
        state = self._candidate_state(principal["user_id"], profile_name)
        questions = state.get("questions") or []
        index = int(state.get("index") or 0)
        if index >= len(questions):
            self._clear_candidate_state(principal["user_id"])
            await self._send_text(
                chat_id,
                "✅ Интервью завершено. Подтверждённые ответы уже используются в вашем профиле.",
                reply_markup=self._menu_reply_markup(principal, menu=MENU_CANDIDATE),
            )
            return
        question = questions[index]
        hint = str(question.get("answer_hint") or "").strip()
        text = "🧠 Вопрос {} из {}\n\n{}".format(index + 1, len(questions), question.get("question", ""))
        if hint:
            text += f"\n\nПодсказка: {hint}"
        text += "\n\nОтветьте сообщением. Ничего не сохранится без подтверждения."
        await self._send_text(chat_id, text, reply_markup=self._menu_reply_markup(principal, menu=MENU_CANDIDATE))

    async def _accept_candidate_document(self, chat_id: int, principal: dict, message: dict) -> bool:
        """Accept a UTF-8 txt/md document as one admin candidate fact."""
        if principal.get("role") != ROLE_ADMIN:
            return False
        profile_name = self._selected_profile(principal)
        state = self._candidate_state(principal["user_id"], profile_name)
        if state.get("mode") != "add":
            return False
        document = message.get("document") or {}
        file_id = str(document.get("file_id") or "").strip()
        filename = str(document.get("file_name") or "facts.txt").strip()
        if not file_id or not filename.lower().endswith((".txt", ".md", ".markdown")):
            return False
        try:
            raw = await self._download_file(file_id)
            answer = raw.decode("utf-8-sig", errors="replace").strip()
        except Exception as exc:
            log.warning("candidate facts document download failed: %s", exc)
            await self._send_text(chat_id, "❌ Не удалось прочитать файл. Пришли UTF-8 .txt или .md.", reply_markup=self._menu_reply_markup(principal, menu=MENU_CANDIDATE))
            return True
        answer = " ".join(answer.split()).strip()
        if not answer:
            await self._send_text(chat_id, "❌ Файл пустой.", reply_markup=self._menu_reply_markup(principal, menu=MENU_CANDIDATE))
            return True
        state.update(mode="confirm", pending_text=answer, pending_topic="база знаний")
        self._set_candidate_state(principal["user_id"], state)
        await self._send_text(chat_id, f"Файл «{filename}» прочитан ({len(answer)} символов).\n\nСохранить как один подтверждённый факт?", reply_markup=self._candidate_confirm_markup())
        return True

    async def _accept_candidate_interview_input(self, chat_id: int, principal: dict, text: str) -> bool:
        profile_name = self._selected_profile(principal)
        state = self._candidate_state(principal["user_id"], profile_name)
        if state.get("mode") not in {"answer", "add"}:
            return False
        if _is_menu_button_text(text) or text.startswith("/"):
            return False
        if text.strip().casefold() in {"отмена", "cancel"}:
            self._clear_candidate_state(principal["user_id"])
            await self._send_text(chat_id, "Интервью остановлено. Уже подтверждённые факты сохранены.", reply_markup=self._menu_reply_markup(principal, menu=MENU_CANDIDATE))
            return True
        answer = " ".join(text.split()).strip()
        if not answer:
            return True
        # Для обычных пользователей сохраняем короткие подтверждаемые факты.
        # Администратору разрешён расширенный ввод, чтобы перенести большую
        # базу знаний одним фактом без искусственного дробления.
        max_length = None if principal.get("role") == ROLE_ADMIN else 1200
        if max_length is not None and len(answer) > max_length:
            await self._send_text(
                chat_id,
                f"Ответ слишком длинный: максимум {max_length} символов.",
                reply_markup=self._menu_reply_markup(principal, menu=MENU_CANDIDATE),
            )
            return True
        topic = "общий"
        if state.get("mode") == "answer":
            questions = state.get("questions") or []
            index = int(state.get("index") or 0)
            if index < len(questions):
                topic = str(questions[index].get("topic") or topic)
        state.update(mode="confirm", pending_text=answer, pending_topic=topic)
        self._set_candidate_state(principal["user_id"], state)
        await self._send_text(
            chat_id,
            f"Вот что будет сохранено как подтверждённый факт:\n\n[{topic}] {answer}\n\nПроверьте формулировку.",
            reply_markup=self._candidate_confirm_markup(),
        )
        return True

    async def _candidate_advance(self, chat_id: int, principal: dict, profile_name: str) -> None:
        state = self._candidate_state(principal["user_id"], profile_name)
        if state.get("kind") == "add":
            self._clear_candidate_state(principal["user_id"])
            await self._send_text(chat_id, "✅ Факт сохранён для этого профиля.", reply_markup=self._menu_reply_markup(principal, menu=MENU_CANDIDATE))
            return
        state.update(mode="answer", index=int(state.get("index") or 0) + 1)
        state.pop("pending_text", None)
        state.pop("pending_topic", None)
        self._set_candidate_state(principal["user_id"], state)
        await self._send_candidate_question(chat_id, principal, profile_name)

    def _captcha_pending_for_principal(self, principal: dict) -> tuple[dict | None, str]:
        """Return only the pending captcha that this Telegram user may answer."""
        profile_name = self._selected_profile(principal)
        try:
            import captcha_bridge

            pending = captcha_bridge.peek_pending(profile_name)
        except Exception as exc:
            log.debug("captcha bridge peek failed for %s: %s", profile_name, exc)
            return None, profile_name
        if not pending:
            return None, profile_name
        request_profile = str(pending.get("profile_name") or profile_name).strip()
        if principal.get("role") != ROLE_ADMIN and request_profile != profile_name:
            return None, profile_name
        return pending, request_profile

    def _reload_profile_daemon(self, profile_name: str) -> bool:
        """Restart an already enabled profile daemon so it reloads profile.env."""
        state = self._daemon_state(profile_name)
        if not state.get("running"):
            return False
        profile = self._profile(profile_name)
        stopped = runtime_control.stop_process(
            profile.daemon_pid_file,
            expected_tokens=runtime_control.AGENT_DAEMON_TOKENS,
            fallback_pid=state.get("pid") or 0,
        )
        if not stopped.get("ok"):
            raise RuntimeError("Не удалось остановить текущий повтор поиска.")
        restarted = runtime_control.start_background_process(
            runtime_control.agent_command_argv(profile_name, "--daemon"),
            pid_file=profile.daemon_pid_file,
            log_file=profile.log_file,
            expected_tokens=runtime_control.AGENT_DAEMON_TOKENS,
            fallback_pid=0,
        )
        if not restarted.get("ok"):
            raise RuntimeError("Не удалось заново запустить повтор поиска.")
        return True

    def _search_settings_text(self, profile_name: str, *, user_id: int) -> str:
        profile = self._profile(profile_name)
        client = self._client_record(user_id) or {}
        queries = profile.hh.search_queries
        lines = [
            f"🎯 Мой поиск · профиль {_pretty_profile_name(profile_name)}",
            "",
            "🔑 Ключевые слова:",
        ]
        lines.extend(f"{index}. {query}" for index, query in enumerate(queries, start=1))
        lines.extend([
            "",
            f"📄 Резюме HH: {profile.hh.primary_resume_title or 'ещё не выбрано'}",
            f"🎯 Направление: {client.get('target_role') or 'не указано'}",
            f"📍 Локация: {client.get('target_location') or 'настроена в профиле'}",
            f"🔁 Повтор: {_schedule_preset_label(profile.search_interval_min)}",
            f"🛡 Режим откликов: {self._application_mode_label(profile.hh.application_mode)}",
            "",
            "ИИ только предложит черновик. Запросы сохраняются или меняются только по вашей команде.",
        ])
        return "\n".join(lines)

    @staticmethod
    def _application_mode_label(value: str) -> str:
        return {
            "preview": "только показать",
            "confirm": "подтверждать",
            "auto": "автоотклик",
        }.get(value, "автоотклик")

    @staticmethod
    def _experience_label(value: str) -> str:
        return {
            "": "любой",
            "noExperience": "без опыта",
            "between1And3": "1–3 года",
            "between3And6": "3–6 лет",
            "moreThan6": "6+ лет",
        }.get(value, "любой")

    def _search_conditions_text(self, profile_name: str) -> str:
        hh = self._profile(profile_name).hh
        salary = f"{hh.search_salary:,} ₽".replace(",", " ") if hh.search_salary else "не задана"
        return "\n".join([
            "⚙️ Условия поиска",
            "",
            f"👤 Опыт: {self._experience_label(hh.search_experience)}",
            f"💰 Зарплата от: {salary}",
            f"💵 Только с указанной зарплатой: {'да' if hh.search_only_with_salary else 'нет'}",
            f"🏠 Только удалёнка: {'да' if hh.search_remote_only else 'нет'}",
            "",
            "Изменения сохраняются в вашем профиле и подхватываются повтором сразу.",
        ])

    def _application_mode_text(self, profile_name: str) -> str:
        mode = self._profile(profile_name).hh.application_mode
        return "\n".join([
            "🛡 Режим откликов",
            "",
            f"Сейчас: {self._application_mode_label(mode)}.",
            "👀 Только показать — пришлёт подходящие вакансии, без отправки.",
            "✋ Подтверждать — отправит только после нажатия «Откликнуться с ИИ».",
            "⚡ Автоотклик — отправит подходящие вакансии сам в рамках лимитов.",
        ])

    @staticmethod
    def _review_item_text(item: dict, index: int) -> str:
        vacancy = item.get("vacancy") or {}
        evaluation = item.get("evaluation") or {}
        title = html.escape(str(vacancy.get("title") or "Вакансия"))
        company = html.escape(str(vacancy.get("company") or "Компания не указана"))
        source = html.escape(str(vacancy.get("source_label") or vacancy.get("source") or "hh.ru"))
        salary = html.escape(str(vacancy.get("salary") or "зарплата не указана"))
        score = evaluation.get("score")
        reason = " ".join(str(evaluation.get("reason") or "").split())
        if len(reason) > 380:
            reason = reason[:377].rstrip() + "..."
        lines = [
            f"<b>{index}. {title}</b>",
            f"{company} · {source}",
            f"💰 {salary}" + (f" · 📊 {score}/100" if score not in (None, "") else ""),
        ]
        if reason:
            lines.append(html.escape(reason))
        return "\n".join(lines)

    async def _show_review_queue(self, chat_id: int, principal: dict, *, profile_name: str) -> None:
        self._set_selected_menu(principal["user_id"], MENU_REVIEW)
        items = manual_apply_queue.list_candidates(profile_name, limit=8)
        if not items:
            await self._send_text(
                chat_id,
                "📋 На рассмотрении пока пусто. Подходящие вакансии появятся здесь, если выбран режим просмотра или подтверждения.",
                reply_markup=self._menu_reply_markup(principal, menu=MENU_REVIEW),
            )
            return
        total = len(manual_apply_queue.list_candidates(profile_name, limit=250))
        await self._send_text(
            chat_id,
            f"📋 На рассмотрении: {total}. Показываю последние {len(items)}.",
            reply_markup=self._menu_reply_markup(principal, menu=MENU_REVIEW),
        )
        for index, item in enumerate(items, start=1):
            vacancy = item.get("vacancy") or {}
            markup = manual_apply_queue.build_manual_apply_markup(
                vacancy,
                profile_name,
                str(item.get("token") or ""),
                allow_ai_apply=bool(item.get("allow_ai_apply", True)),
            )
            await self._send_text(
                chat_id,
                self._review_item_text(item, index),
                reply_markup=markup,
            )

    async def _save_search_setting(
        self,
        chat_id: int,
        principal: dict,
        updates: dict[str, str | int],
        message: str,
        *,
        menu: str,
    ) -> None:
        profile_name = self._selected_profile(principal)
        profile_mod.update_profile_env(profile_name, updates)
        try:
            restarted = self._reload_profile_daemon(profile_name)
            restart_note = "\n🔁 Повтор перезапущен с новыми настройками." if restarted else ""
        except RuntimeError as exc:
            restart_note = f"\n⚠️ Настройка сохранена, но повтор не перезапустился: {exc}"
        self._set_selected_menu(principal["user_id"], menu)
        await self._send_text(
            chat_id,
            f"✅ {message}" + restart_note,
            reply_markup=self._menu_reply_markup(principal, menu=menu),
        )

    async def _accept_search_query_input(self, chat_id: int, principal: dict, text: str) -> bool:
        state = self._search_settings_state(principal["user_id"])
        input_kind = state.get("search_input")
        if input_kind not in {"queries", "resume", "salary", "company_block", "company_unblock"}:
            return False
        if _is_menu_button_text(text) or text.startswith("/") and text != "/cancel":
            self._clear_search_settings_state(principal["user_id"], "search_input")
            return False
        if text.strip().casefold() in {"отмена", "/cancel"}:
            self._clear_search_settings_state(principal["user_id"], "search_input")
            await self._send_text(
                chat_id,
                "Изменение запросов отменено.",
                reply_markup=self._menu_reply_markup(
                    principal,
                    menu=MENU_SEARCH_CONDITIONS if input_kind == "salary" else MENU_SEARCH,
                ),
            )
            return True
        if input_kind in {"company_block", "company_unblock"}:
            profile_name = self._selected_profile(principal)
            try:
                company_blacklist.set_blocked(text, input_kind == "company_block", profile_name)
            except ValueError as exc:
                await self._send_text(chat_id, str(exc))
                return True
            self._clear_search_settings_state(principal["user_id"], "search_input")
            await self._send_text(chat_id, "Список компаний обновлён.", reply_markup=self._menu_reply_markup(principal, menu=MENU_SEARCH))
            return True
        if input_kind == "salary":
            normalized = text.strip().replace(" ", "").replace("₽", "")
            if not normalized.isdigit() or int(normalized) > 10_000_000:
                await self._send_text(
                    chat_id,
                    "❌ Отправьте сумму цифрами от 0 до 10 000 000. Ноль отключит фильтр. Для отмены: Отмена.",
                    reply_markup=self._menu_reply_markup(principal, menu=MENU_SEARCH_CONDITIONS),
                )
                return True
            salary = int(normalized)
            self._clear_search_settings_state(principal["user_id"], "search_input")
            await self._save_search_setting(
                chat_id,
                principal,
                {"HH_SEARCH_SALARY": salary},
                f"Минимальная зарплата: {salary:,} ₽".replace(",", " "),
                menu=MENU_SEARCH_CONDITIONS,
            )
            return True
        if input_kind == "resume":
            try:
                index = int(text.strip())
                catalog = client_hh_auth.load_hh_resume_catalog(self._selected_profile(principal))
                item = catalog[index - 1]
                resume_id = str(item.get("id") or "").strip()
                title = str(item.get("title") or "").strip()
                if not resume_id:
                    raise ValueError
                env_file = profile_mod.update_profile_env(
                    self._selected_profile(principal),
                    {
                        "HH_PRIMARY_RESUME_ID": resume_id,
                        "HH_PRIMARY_RESUME_TITLE": title,
                    },
                )
                try:
                    restarted = self._reload_profile_daemon(self._selected_profile(principal))
                    restart_note = "\n🔁 Повтор перезапущен с новым резюме." if restarted else ""
                except RuntimeError as exc:
                    restart_note = f"\n⚠️ Резюме сохранено, но повтор не перезапустился: {exc}"
            except (ValueError, IndexError, TypeError):
                await self._send_text(
                    chat_id,
                    "❌ Отправьте номер резюме из списка или «Отмена».",
                    reply_markup=self._menu_reply_markup(principal, menu=MENU_SEARCH),
                )
                return True
            self._clear_search_settings_state(principal["user_id"], "search_input")
            await self._send_text(
                chat_id,
                f"✅ Основное резюме выбрано: {title or resume_id}\nФайл настроек: {env_file}" + restart_note,
                reply_markup=self._menu_reply_markup(principal, menu=MENU_SEARCH),
            )
            return True
        try:
            queries = search_query_suggester.normalize_queries(text)
            env_file = profile_mod.update_profile_env(
                self._selected_profile(principal),
                {"HH_SEARCH_QUERIES": search_query_suggester.queries_to_env_value(queries)},
            )
            try:
                restarted = self._reload_profile_daemon(self._selected_profile(principal))
                restart_note = "\n🔁 Повтор перезапущен с новыми настройками." if restarted else ""
            except RuntimeError as exc:
                restart_note = f"\n⚠️ Запросы сохранены, но повтор не перезапустился: {exc}"
        except (search_query_suggester.SearchQueryValidationError, ValueError) as exc:
            await self._send_text(
                chat_id,
                f"❌ {exc}\n\nОтправьте список ещё раз: один запрос в строке. Для отмены: Отмена.",
                reply_markup=self._menu_reply_markup(principal, menu=MENU_SEARCH),
            )
            return True
        self._clear_search_settings_state(principal["user_id"], "search_input", "search_query_draft")
        await self._send_text(
            chat_id,
            "✅ Запросы сохранены для вашего профиля. Следующий поиск возьмёт именно этот список.\n"
            f"Файл настроек: {env_file}" + restart_note,
            reply_markup=self._menu_reply_markup(principal, menu=MENU_SEARCH),
        )
        return True

    async def _start_search_query_suggestion(self, chat_id: int, principal: dict, *, profile_name: str) -> None:
        if self._has_active_command(profile_name):
            await self._send_busy_status(chat_id, principal, profile_name=profile_name)
            return
        active_command = self._mark_active_command(
            principal=principal,
            label="suggest search queries",
            profile_name=profile_name,
        )
        await self._send_text(
            chat_id,
            "✨ Подбираю варианты по вашему резюме. Это черновик: без вашего подтверждения настройки не изменятся.",
            reply_markup=self._menu_reply_markup(principal, menu=MENU_SEARCH),
        )

        async def runner() -> None:
            try:
                profile = self._profile(profile_name)
                client = self._client_record(principal["user_id"]) or {}
                queries = await search_query_suggester.suggest_queries(
                    resume_path=profile.resume_file,
                    target_role=str(client.get("target_role") or ""),
                    current_queries=profile.hh.search_queries,
                )
                self._set_search_settings_state(principal["user_id"], search_query_draft=queries)
                rendered = "\n".join(f"{index}. {query}" for index, query in enumerate(queries, start=1))
                await self._send_text(
                    chat_id,
                    "✨ ИИ предложил черновик:\n" + rendered +
                    "\n\nЧтобы применить его, нажмите «Сохранить предложенное». "
                    "Или нажмите «Изменить запросы» и отправьте свой список.",
                    reply_markup=self._menu_reply_markup(principal, menu=MENU_SEARCH),
                )
            except Exception as exc:
                log.warning("Search query suggestion failed for %s: %s", profile_name, exc)
                await self._send_text(
                    chat_id,
                    "❌ Не удалось получить предложения ИИ. Текущие запросы не менялись.",
                    reply_markup=self._menu_reply_markup(principal, menu=MENU_SEARCH),
                )
            finally:
                self._clear_active_command(profile_name)

        active_command.task = asyncio.create_task(runner())

    def _latest_run(self, profile_name: str) -> dict | None:
        return runtime_control.latest_run_entry(self._profile(profile_name).run_history_file)

    def _profile_recipient_ids(self, profile_name: str) -> list[int]:
        recipients = []
        for item in telegram_access.list_users():
            try:
                user_id = int(item.get("user_id") or 0)
            except (TypeError, ValueError):
                continue
            if user_id <= 0 or not item.get("enabled", True):
                continue
            if str(item.get("profile") or "").strip() == profile_name:
                recipients.append(user_id)
        if recipients:
            return sorted(set(recipients))
        profile = self._profile(profile_name)
        configured = int(getattr(getattr(profile, "notify", None), "chat_id", 0) or 0)
        fallback = int(config.NOTIFY_CHAT_ID or 0)
        return sorted({item for item in (configured, fallback) if item > 0})

    @staticmethod
    def _format_age(seconds: float) -> str:
        if seconds < 90:
            return "меньше 1 мин назад"
        minutes = int(seconds // 60)
        if minutes < 90:
            return f"{minutes} мин назад"
        hours = int(minutes // 60)
        if hours < 48:
            return f"{hours} ч назад"
        return f"{int(hours // 24)} дн назад"

    @staticmethod
    def _format_duration(seconds: float) -> str:
        if seconds < 90:
            return "меньше 1 мин"
        minutes = int(seconds // 60)
        if minutes < 90:
            return f"{minutes} мин"
        hours = int(minutes // 60)
        if hours < 48:
            return f"{hours} ч"
        return f"{int(hours // 24)} дн"

    @staticmethod
    def _parse_local_dt(value: str) -> datetime | None:
        raw = (value or "").strip()
        if not raw:
            return None
        for candidate in (raw[:19], raw):
            try:
                return datetime.fromisoformat(candidate)
            except ValueError:
                pass
        try:
            return datetime.strptime(raw[:19], "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return None

    @classmethod
    def _age_from_iso(cls, value: str) -> float | None:
        parsed = cls._parse_local_dt(value)
        if not parsed:
            return None
        return max(0.0, (datetime.now() - parsed).total_seconds())

    @classmethod
    def _recent_log_matches(cls, text: str, markers: tuple[str, ...], *, max_age_min: int = 60) -> list[str]:
        now = datetime.now()
        matched = []
        for raw_line in (text or "").splitlines():
            line = raw_line.strip()
            lowered = line.casefold()
            if not any(marker.casefold() in lowered for marker in markers):
                continue
            parsed = cls._parse_local_dt(line[:19])
            if parsed and (now - parsed).total_seconds() > max_age_min * 60:
                continue
            matched.append(line)
        return matched[-3:]

    @staticmethod
    def _auth_cookie_names(cookies_file: str) -> set[str]:
        try:
            with open(cookies_file, encoding="utf-8") as f:
                cookies = json.load(f)
        except Exception:
            return set()
        if not isinstance(cookies, list):
            return set()
        auth_names = {"hhtoken", "hhuid", "crypted_hhuid", "crypted_id"}
        return {str(item.get("name") or "") for item in cookies if isinstance(item, dict)} & auth_names

    async def _collect_diagnostics(self, profile_name: str) -> list[dict]:
        checks: list[dict] = []
        profile = self._profile(profile_name)
        search_interval_min, invite_check_interval_min = self._profile_schedule(profile_name)
        daemon_state = self._daemon_state(profile_name)
        bot_state = self._bot_state()

        checks.append({
            "name": "Daemon",
            "ok": bool(daemon_state.get("running")),
            "detail": f"pid {daemon_state.get('pid') or '-'}" if daemon_state.get("running") else "остановлен",
        })
        checks.append({
            "name": "Telegram bot process",
            "ok": bool(bot_state.get("running")),
            "detail": f"pid {bot_state.get('pid') or '-'}" if bot_state.get("running") else "остановлен",
        })

        try:
            me = await self._api_request("getMe", {}, timeout=15)
            username = str((me or {}).get("username") or "bot") if isinstance(me, dict) else "bot"
            checks.append({"name": "Telegram API", "ok": True, "detail": f"getMe ok: @{username}"})
        except Exception as exc:
            checks.append({"name": "Telegram API", "ok": False, "detail": telegram_error_summary(exc)})

        recipients = self._profile_recipient_ids(profile_name)
        checks.append({
            "name": "Telegram recipients",
            "ok": bool(recipients),
            "detail": f"{len(recipients)} получател(я/ей): {', '.join(map(str, recipients[:3]))}" if recipients else "нет получателей",
        })

        proxy_values = {
            key: os.getenv(key, "").strip()
            for key in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "TELEGRAM_PROXY")
            if os.getenv(key, "").strip()
        }
        stale_proxy = [key for key, value in proxy_values.items() if "127.0.0.1:10809" in value]
        checks.append({
            "name": "Proxy env",
            "ok": False if stale_proxy else True,
            "detail": f"stale proxy: {', '.join(stale_proxy)}" if stale_proxy else "stale proxy не активен",
        })

        cookies = self._auth_cookie_names(profile.hh.cookies_file)
        checks.append({
            "name": "HH cookies",
            "ok": bool({"hhtoken", "hhuid"} & cookies),
            "detail": f"auth cookies: {', '.join(sorted(cookies))}" if cookies else "auth cookies не найдены",
        })

        log_path, log_tail = self._log_tail(profile_name, kind="log", lines=220)
        hh_login_errors = self._recent_log_matches(
            log_tail,
            ("not logged in", "redirected to HH login", "Не залогинен", "hh.ru is not logged in"),
            max_age_min=180,
        )
        checks.append({
            "name": "HH login markers",
            "ok": not hh_login_errors,
            "detail": "нет свежих редиректов на login" if not hh_login_errors else hh_login_errors[-1][-140:],
        })

        if log_path and os.path.exists(log_path):
            age = time.time() - os.path.getmtime(log_path)
            expected_log_interval_min = max(1, min(int(search_interval_min), int(invite_check_interval_min)))
            max_log_age = max(3 * 60 * 60, expected_log_interval_min * 3 * 60)
            checks.append({
                "name": "Последняя запись в логе",
                "ok": None,
                "informational": True,
                "detail": f"{self._format_age(age)} · порог {self._format_duration(max_log_age)}",
            })
        else:
            checks.append({"name": "Последняя запись в логе", "ok": None, "informational": True, "detail": "лог не найден"})

        latest = self._latest_run(profile_name) or {}
        latest_age = self._age_from_iso(str(latest.get("created_at") or latest.get("finished_at") or ""))
        if latest_age is None:
            checks.append({"name": "Last run", "ok": None, "detail": "нет истории прогонов"})
        else:
            max_age = max(6 * 60 * 60, int(search_interval_min) * 3 * 60)
            checks.append({
                "name": "Данные прогона",
                "ok": latest_age < max_age,
                "detail": self._format_age(latest_age),
            })

            checks.append({"name": "Последний прогон", "ok": bool(latest.get("ok")),
                "detail": "не завершён" if latest.get("status") == "incomplete" else
                          ("успешно" if latest.get("ok") else "ошибка")})

        bot_log = runtime_control.tail_file(self.runtime_paths.bot_log_file, lines=120, chars=12000)
        recent_poll_errors = self._recent_log_matches(
            bot_log,
            ("Failed to fetch updates", "Server disconnected", "Cannot connect to host api.telegram.org", "getUpdates failed"),
            max_age_min=60,
        )
        checks.append({
            "name": "Telegram polling log",
            "ok": None if recent_poll_errors else True,
            "detail": "есть свежие сетевые разрывы, API getMe проверен отдельно" if recent_poll_errors else "свежих ошибок polling нет",
        })
        return checks

    async def _maybe_send_health_alert(self) -> None:
        if not getattr(config, "TELEGRAM_HEALTH_CHECK_ENABLED", True):
            return
        state = self._load_state()
        health_state = state.get("health_check") if isinstance(state.get("health_check"), dict) else {}
        interval_s = max(300, int(getattr(config, "TELEGRAM_HEALTH_CHECK_INTERVAL_MIN", 60) or 60) * 60)
        last_checked = float(health_state.get("last_checked_at") or 0)
        if time.time() - last_checked < interval_s:
            return

        checks = await self._collect_diagnostics(self.profile_name)
        failed = [item for item in checks if item.get("ok") is False]
        signature = "|".join(sorted(str(item.get("name") or "") for item in failed))
        health_fields = {
            "last_checked_at": time.time(),
            "last_checked_iso": datetime.now().isoformat(timespec="seconds"),
            "last_signature": signature,
            "last_failed_count": len(failed),
        }

        def record_check(latest):
            latest.setdefault("health_check", {}).update(health_fields)

        latest = self._update_state(record_check)
        previous_alert_signature = str(latest["health_check"].get("last_alert_signature") or "")
        if not failed:
            return
        if signature == previous_alert_signature:
            return

        text = build_diagnostics_text(
            profile_name=self.profile_name,
            generated_at=datetime.now().isoformat(timespec="seconds"),
            checks=checks,
        )
        sent = 0
        for user_id in self._profile_recipient_ids(self.profile_name):
            principal = telegram_access.resolve_user(user_id)
            reply_markup = self._menu_reply_markup(principal) if principal else None
            result = await self._send_text_safely(user_id, text, reply_markup=reply_markup)
            if result is not None:
                sent += 1
        if sent:
            def record_alert(latest):
                latest.setdefault("health_check", {}).update({
                    "last_alert_signature": signature,
                    "last_alert_at": datetime.now().isoformat(timespec="seconds"),
                })

            self._update_state(record_alert)
            self._append_debug_log("health_alert_sent", profile_name=self.profile_name, failed=len(failed), recipients=sent)

    def _recent_runs(self, profile_name: str, limit: int = 5) -> list[dict]:
        profile = self._profile(profile_name)
        run_history_file = profile.run_history_file
        if limit <= 0 or not os.path.exists(run_history_file):
            return []
        try:
            items = read_json_records(run_history_file)
        except OSError:
            return []
        return list(reversed(analytics.reconcile_run_records(items, events_file=profile.analytics_events_file)[-limit:]))

    def _stats_snapshot(self, profile_name: str) -> dict:
        profile = self._profile(profile_name)
        return {
            "profile_name": profile_name,
            "seen_stats": seen.stats_from_file(profile.seen_file),
            "analytics_summary": analytics.summarize(events_file=profile.analytics_events_file, all_time=True),
            "recent_runs": self._recent_runs(profile_name, limit=3),
        }

    def _daily_summary_snapshot(self, profile_name: str, *, now: datetime | None = None) -> dict:
        profile = self._profile(profile_name)
        current = now or datetime.now()
        start_of_day = current.replace(hour=0, minute=0, second=0, microsecond=0)
        return {
            "profile_name": profile_name,
            "analytics_summary": analytics.summarize(
                events_file=profile.analytics_events_file,
                start_at=start_of_day,
                end_at=current,
            ),
            "recent_runs": self._recent_runs(profile_name, limit=1),
            "period_label": f"Сегодня, с 00:00 до {current:%H:%M}",
        }

    def _daily_summary_due(self, now: datetime | None = None) -> tuple[bool, str]:
        if not getattr(config, "TELEGRAM_DAILY_SUMMARY_ENABLED", True):
            return False, ""
        current = now or datetime.now()
        hour = max(0, min(23, int(getattr(config, "TELEGRAM_DAILY_SUMMARY_HOUR", 20) or 20)))
        today = current.date().isoformat()
        if current.hour < hour:
            return False, today
        state = self._load_state()
        daily_state = state.get("daily_summary") if isinstance(state.get("daily_summary"), dict) else {}
        return daily_state.get("last_sent_date") != today, today

    async def _maybe_send_daily_summary(self) -> None:
        due, today = self._daily_summary_due()
        if not due:
            return

        profile_names = set(self._profile_names())
        recipients: dict[int, dict] = {}
        for item in telegram_access.list_users():
            try:
                user_id = int(item.get("user_id") or 0)
            except (TypeError, ValueError):
                continue
            if user_id <= 0 or not item.get("enabled", True):
                continue
            profile_name = str(item.get("profile") or self.profile_name or "default").strip() or "default"
            if profile_name not in profile_names:
                log.warning("daily summary skipped user=%s unknown profile=%s", user_id, profile_name)
                continue
            recipients[user_id] = {**item, "profile": profile_name}

        sent = 0
        for user_id, principal in sorted(recipients.items()):
            profile_name = str(principal.get("profile") or self.profile_name or "default")
            try:
                snapshot = self._daily_summary_snapshot(profile_name, now=datetime.now())
                text = build_daily_summary_text(
                    profile_name=profile_name,
                    analytics_summary=snapshot["analytics_summary"],
                    recent_runs=snapshot["recent_runs"],
                    period_label=snapshot["period_label"],
                )
                result = await self._send_text_safely(
                    user_id,
                    text,
                    reply_markup=self._menu_reply_markup(principal),
                )
                if result is not None:
                    sent += 1
            except Exception as exc:
                log.warning("daily summary failed user=%s profile=%s: %s", user_id, profile_name, exc)

        if sent <= 0:
            return
        def record_summary(state):
            state["daily_summary"] = {
                "last_sent_date": today,
                "last_sent_at": datetime.now().isoformat(timespec="seconds"),
                "recipients": sent,
            }

        self._update_state(record_summary)
        self._append_debug_log("daily_summary_sent", date=today, recipients=sent)

    def _runtime_status(self, profile_name: str) -> dict | None:
        runtime_file = self._profile(profile_name).runtime_status_file
        runtime = runtime_control.read_json_file(runtime_file)
        normalized = _normalize_process_runtime(runtime, expected_tokens=ACTIVE_RUNTIME_TOKENS)
        if runtime and normalized and normalized != runtime:
            # A process can publish progress after the initial read. Normalize
            # the current snapshot under its writer lock, not the stale one.
            latest = JsonStore(runtime_file).update(
                lambda state: _normalize_process_runtime(state, expected_tokens=ACTIVE_RUNTIME_TOKENS) or state
            )
            return _normalize_process_runtime(latest, expected_tokens=ACTIVE_RUNTIME_TOKENS)
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
        bot_runtime = runtime_control.read_json_file(self.runtime_paths.bot_runtime_file) or {}
        fallback_pid = bot_runtime.get("pid")
        fallback_pid = int(fallback_pid) if isinstance(fallback_pid, int) else 0
        return runtime_control.describe_process(
            self.runtime_paths.bot_pid_file,
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
        def bootstrap(latest):
            if latest.get("last_update_id") is None:
                latest["last_update_id"] = int(updates[-1]["update_id"])
                latest["bootstrapped_at"] = datetime.now().isoformat(timespec="seconds")

        self._update_state(bootstrap)

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
            def advance_offset(latest):
                current = latest.get("last_update_id")
                latest["last_update_id"] = max(int(current) if current is not None else 0, int(updates[-1]["update_id"]))
                latest["updated_at"] = datetime.now().isoformat(timespec="seconds")

            self._update_state(advance_offset)
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
        path = self.runtime_paths.bot_debug_log_file
        if not path:
            return
        try:
            payload = {
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "event": event,
                **fields,
            }
            append_json(path, payload, default=str)
        except Exception as exc:
            log.warning("Failed to append Telegram debug log: %s", type(exc).__name__)

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

        if await self._accept_candidate_document(chat_id, principal, message):
            return

        if await self._maybe_accept_hh_auth_response(chat_id, principal, text):
            return

        if await self._accept_form_answer(chat_id, principal, message):
            return

        if await self._accept_search_query_input(chat_id, principal, text):
            return

        if await self._accept_candidate_interview_input(chat_id, principal, text):
            return

        # Captcha привязана к профилю: её может решить владелец своей сессии
        # либо админ в выбранном профиле.
        if (
            text
            and len(text) <= 100
            and not text.startswith("/")
            and not text.startswith("➡")
        ):
            pending, captcha_profile_name = self._captcha_pending_for_principal(principal)
            if pending:
                # Исключаем кнопки меню: их нельзя считать текстом captcha.
                is_menu_button = (
                    text in ADMIN_BUTTON_MAP
                    or text in USER_BUTTON_MAP
                    or text in LEGACY_BUTTON_MAP
                )
                if not is_menu_button:
                    try:
                        import captcha_bridge as cb
                        cb.write_response(pending["id"], text, profile_name=captcha_profile_name)
                        await self._send_text(
                            chat_id,
                            "✅ Принял текст captcha. Вставляю его в форму hh.ru…",
                        )
                        return
                    except Exception as exc:
                        log.warning("captcha response write failed: %s", exc)

        command, arg = _resolve_message_command(text, principal.get("role", ROLE_USER))
        if _looks_like_standalone_hh_auth_code(text):
            await self._send_text(
                chat_id,
                "🔐 Похоже на HH SMS-код, но активного запроса входа сейчас нет. "
                "Сначала нажми восстановление HH-сессии, дождись сообщения «HH просит SMS-код», "
                "и только потом пришли код сюда.",
                reply_markup=self._menu_reply_markup(principal),
            )
            return
        if not command:
            return
        await self._dispatch(chat_id, principal, command, arg)

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
        if command == "/back":
            parent = MENU_ADMIN if self._selected_menu(principal) == MENU_LLM else MENU_MAIN
            await self._send_menu(chat_id, principal, profile_name=profile_name, menu=parent)
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

        if command in {"/menu_llm", "/llm_balance", "/llm_test"}:
            self._set_selected_menu(principal["user_id"], MENU_LLM)
            markup = self._menu_reply_markup(principal, menu=MENU_LLM)
            if command == "/menu_llm":
                message = admin_llm.overview()
            elif command == "/llm_balance":
                message = await admin_llm.diagnostic()
            elif self._llm_test_lock.locked() or time.monotonic() < self._llm_test_next_at:
                message = "⏳ Тест доступен не чаще раза в минуту. Дождитесь завершения предыдущего."
            else:
                async with self._llm_test_lock:
                    self._llm_test_next_at = time.monotonic() + 60
                    await self._send_text(chat_id, "🧪 Проверяю DeepSeek…", reply_markup=markup)
                    message = await admin_llm.diagnostic(test=True)
            await self._send_text(chat_id, message, reply_markup=markup)
            return

        if command in {"/candidate_profile", "/candidate_facts", "/candidate_add_fact", "/candidate_interview", "/candidate_save", "/candidate_edit", "/candidate_skip"}:
            self._set_selected_menu(principal["user_id"], MENU_CANDIDATE)
            profile_dir = self._candidate_profile_dir(profile_name)
            state = self._candidate_state(principal["user_id"], profile_name)
            if command == "/candidate_profile":
                await self._send_menu(chat_id, principal, profile_name=profile_name, menu=MENU_CANDIDATE)
                return
            if command == "/candidate_facts":
                message = candidate_interview.render_facts(profile_dir=profile_dir)
                # Используем тот же профильный knowledge/, что и генератор писем.
                try:
                    from prompt_blocks import build_knowledge_base_block
                    knowledge = build_knowledge_base_block(limit_chars=7000, profile_dir=profile_dir)
                except Exception as exc:
                    log.debug("candidate knowledge display failed: %s", exc)
                    knowledge = ""
                if knowledge:
                    message += "\n\n" + knowledge
                await self._send_text(chat_id, message, reply_markup=self._menu_reply_markup(principal, menu=MENU_CANDIDATE))
                return
            if command == "/candidate_add_fact":
                self._set_candidate_state(principal["user_id"], {"profile_name": profile_name, "kind": "add", "mode": "add"})
                await self._send_text(chat_id, "Напишите один факт о себе. Он будет использован только после вашего подтверждения.\n\nНапример: «Работал с электрощитами до 1000 В; допуск III группы действовал до 2025 года».", reply_markup=self._menu_reply_markup(principal, menu=MENU_CANDIDATE))
                return
            if command == "/candidate_interview":
                if state.get("questions") and state.get("mode") in {"answer", "confirm"}:
                    if state.get("mode") == "confirm":
                        await self._send_text(chat_id, "Остался неподтверждённый ответ. Сохраните, измените или пропустите его.", reply_markup=self._candidate_confirm_markup())
                    else:
                        await self._send_candidate_question(chat_id, principal, profile_name)
                    return
                try:
                    with open(self._profile(profile_name).resume_file, encoding="utf-8") as handle:
                        resume_text = handle.read().strip()
                except OSError:
                    resume_text = ""
                client = self._client_record(principal["user_id"]) or {}
                plan = await candidate_interview.build_plan(resume_text, target_role=str(client.get("target_role") or ""), current_facts=candidate_interview.facts(profile_dir))
                self._set_candidate_state(principal["user_id"], {"profile_name": profile_name, "kind": "interview", "mode": "answer", "questions": plan.get("questions") or [], "index": 0})
                hypothesis = str(plan.get("profile_hypothesis") or "").strip()
                if hypothesis:
                    await self._send_text(chat_id, f"Собрал вопросы по резюме. Текущее направление: {hypothesis}.", reply_markup=self._menu_reply_markup(principal, menu=MENU_CANDIDATE))
                await self._send_candidate_question(chat_id, principal, profile_name)
                return
            if command == "/candidate_edit" and state.get("mode") == "confirm":
                state["mode"] = "add" if state.get("kind") == "add" else "answer"
                state.pop("pending_text", None)
                state.pop("pending_topic", None)
                self._set_candidate_state(principal["user_id"], state)
                if state.get("kind") == "add":
                    await self._send_text(chat_id, "Отправьте исправленную формулировку факта.", reply_markup=self._menu_reply_markup(principal, menu=MENU_CANDIDATE))
                else:
                    await self._send_candidate_question(chat_id, principal, profile_name)
                return
            if command == "/candidate_save" and state.get("mode") == "confirm":
                try:
                    candidate_interview.add_fact(
                        state.get("pending_text", ""),
                        topic=state.get("pending_topic", "общий"),
                        profile_dir=profile_dir,
                        max_chars=None if role == ROLE_ADMIN else 1200,
                    )
                except ValueError as exc:
                    await self._send_text(chat_id, f"❌ {exc}", reply_markup=self._menu_reply_markup(principal, menu=MENU_CANDIDATE))
                    return
                await self._candidate_advance(chat_id, principal, profile_name)
                return
            if command == "/candidate_skip" and state:
                if state.get("kind") == "add":
                    self._clear_candidate_state(principal["user_id"])
                    await self._send_text(chat_id, "Факт не сохранён.", reply_markup=self._menu_reply_markup(principal, menu=MENU_CANDIDATE))
                else:
                    await self._candidate_advance(chat_id, principal, profile_name)
                return
            await self._send_text(chat_id, "Сначала начните интервью или добавление факта.", reply_markup=self._menu_reply_markup(principal, menu=MENU_CANDIDATE))
            return

        if command == "/forms":
            await self._list_forms(chat_id, principal, profile_name)
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

        if command in {"/companies", "/company_block", "/company_unblock"}:
            self._set_selected_menu(principal["user_id"], MENU_SEARCH)
            companies = company_blacklist.list_companies(profile_name)
            message = "🚫 Чёрный список компаний\n" + ("\n".join("• " + x for x in companies) or "Пока пуст.")
            if command != "/companies":
                self._set_search_settings_state(principal["user_id"], search_input=command[1:])
                message += "\n\nПришлите точное название одной компании. Добавить её можно заранее, до поиска и первого отклика. Для отмены — Назад."
            await self._send_text(chat_id, message, reply_markup=self._menu_reply_markup(principal, menu=MENU_SEARCH))
            return

        if command == "/search_settings":
            self._set_selected_menu(principal["user_id"], MENU_SEARCH)
            await self._send_text(
                chat_id,
                self._search_settings_text(profile_name, user_id=principal["user_id"]),
                reply_markup=self._menu_reply_markup(principal, menu=MENU_SEARCH),
            )
            return

        if command == "/review":
            await self._show_review_queue(chat_id, principal, profile_name=profile_name)
            return

        if command == "/search_conditions":
            self._set_selected_menu(principal["user_id"], MENU_SEARCH_CONDITIONS)
            await self._send_text(
                chat_id,
                self._search_conditions_text(profile_name),
                reply_markup=self._menu_reply_markup(principal, menu=MENU_SEARCH_CONDITIONS),
            )
            return

        if command == "/search_experience":
            self._set_selected_menu(principal["user_id"], MENU_EXPERIENCE)
            await self._send_text(
                chat_id,
                "👤 Выберите требуемый опыт. Этот фильтр hh.ru применится ко всем вашим запросам.",
                reply_markup=self._menu_reply_markup(principal, menu=MENU_EXPERIENCE),
            )
            return

        experience_commands = {
            "/search_experience_any": ("", "любой"),
            "/search_experience_none": ("noExperience", "без опыта"),
            "/search_experience_1_3": ("between1And3", "1–3 года"),
            "/search_experience_3_6": ("between3And6", "3–6 лет"),
            "/search_experience_6_plus": ("moreThan6", "6+ лет"),
        }
        if command in experience_commands:
            value, label = experience_commands[command]
            await self._save_search_setting(
                chat_id,
                principal,
                {"HH_SEARCH_EXPERIENCE": value},
                f"Фильтр по опыту: {label}.",
                menu=MENU_SEARCH_CONDITIONS,
            )
            return

        if command == "/search_salary":
            self._set_search_settings_state(principal["user_id"], search_input="salary")
            await self._send_text(
                chat_id,
                "💰 Отправьте минимальную зарплату в рублях. Например: 150000.\n"
                "Ноль отключит этот фильтр. Для отмены: Отмена.",
                reply_markup=self._menu_reply_markup(principal, menu=MENU_SEARCH_CONDITIONS),
            )
            return

        if command == "/search_salary_toggle":
            current = self._profile(profile_name).hh.search_only_with_salary
            await self._save_search_setting(
                chat_id,
                principal,
                {"HH_SEARCH_ONLY_WITH_SALARY": 0 if current else 1},
                "Показывать только вакансии с указанной зарплатой."
                if not current else "Вакансии без указанной зарплаты снова включены.",
                menu=MENU_SEARCH_CONDITIONS,
            )
            return

        if command == "/search_remote_toggle":
            current = self._profile(profile_name).hh.search_remote_only
            await self._save_search_setting(
                chat_id,
                principal,
                {"HH_SEARCH_REMOTE_ONLY": 0 if current else 1},
                "Оставлены только удалённые вакансии."
                if not current else "Снова ищем во всех настроенных форматах работы.",
                menu=MENU_SEARCH_CONDITIONS,
            )
            return

        if command == "/search_application_mode":
            self._set_selected_menu(principal["user_id"], MENU_APPLICATION_MODE)
            await self._send_text(
                chat_id,
                self._application_mode_text(profile_name),
                reply_markup=self._menu_reply_markup(principal, menu=MENU_APPLICATION_MODE),
            )
            return

        application_modes = {
            "/search_application_preview": ("preview", "Режим «только показать» включён."),
            "/search_application_confirm": ("confirm", "Режим подтверждения включён."),
            "/search_application_auto": ("auto", "Автоотклик включён."),
        }
        if command in application_modes:
            value, message = application_modes[command]
            await self._save_search_setting(
                chat_id,
                principal,
                {"HH_APPLICATION_MODE": value},
                message,
                menu=MENU_APPLICATION_MODE,
            )
            return

        if command == "/search_edit":
            self._set_search_settings_state(principal["user_id"], search_input="queries")
            await self._send_text(
                chat_id,
                "✏️ Отправьте новый список: один поисковый запрос в строке.\n"
                "Например:\nQA engineer\nManual QA Engineer\nТестировщик ПО\n\n"
                "Список заменит текущий. Для отмены отправьте «Отмена».",
                reply_markup=self._menu_reply_markup(principal, menu=MENU_SEARCH),
            )
            return

        if command == "/search_suggest":
            await self._start_search_query_suggestion(chat_id, principal, profile_name=profile_name)
            return

        if command == "/search_use_draft":
            draft = self._search_settings_state(principal["user_id"]).get("search_query_draft") or []
            try:
                queries = search_query_suggester.normalize_queries(draft)
                env_file = profile_mod.update_profile_env(
                    profile_name,
                    {"HH_SEARCH_QUERIES": search_query_suggester.queries_to_env_value(queries)},
                )
                try:
                    restarted = self._reload_profile_daemon(profile_name)
                    restart_note = "\n🔁 Повтор перезапущен с новыми настройками." if restarted else ""
                except RuntimeError as exc:
                    restart_note = f"\n⚠️ Запросы сохранены, но повтор не перезапустился: {exc}"
            except (search_query_suggester.SearchQueryValidationError, ValueError):
                await self._send_text(
                    chat_id,
                    "ℹ️ Сначала нажмите «Предложить ИИ» или отправьте свой список через «Изменить запросы».",
                    reply_markup=self._menu_reply_markup(principal, menu=MENU_SEARCH),
                )
                return
            self._clear_search_settings_state(principal["user_id"], "search_query_draft")
            await self._send_text(
                chat_id,
                "✅ Черновик ИИ сохранён. Следующий поиск будет использовать эти запросы.\n"
                f"Файл настроек: {env_file}" + restart_note,
                reply_markup=self._menu_reply_markup(principal, menu=MENU_SEARCH),
            )
            return

        if command == "/search_resume":
            catalog = client_hh_auth.load_hh_resume_catalog(profile_name)
            if not catalog:
                await self._send_text(
                    chat_id,
                    "🧾 Пока нет списка резюме HH. Сначала нажмите «Вход HH», затем повторите попытку.",
                    reply_markup=self._menu_reply_markup(principal, menu=MENU_SEARCH),
                )
                return
            lines = ["🧾 Выберите резюме для поиска. Отправьте его номер:", ""]
            for index, item in enumerate(catalog, start=1):
                title = str(item.get("title") or item.get("id") or "Без названия")
                lines.append(f"{index}. {title}")
            lines.append("\nДля отмены отправьте «Отмена».")
            self._set_search_settings_state(principal["user_id"], search_input="resume")
            await self._send_text(
                chat_id,
                "\n".join(lines),
                reply_markup=self._menu_reply_markup(principal, menu=MENU_SEARCH),
            )
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

        if command == "/diagnostics":
            checks = await self._collect_diagnostics(profile_name)
            await self._send_text(
                chat_id,
                build_diagnostics_text(
                    profile_name=profile_name,
                    generated_at=datetime.now().isoformat(timespec="seconds"),
                    checks=checks,
                ),
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
                    bot_runtime=runtime_control.read_json_file(self.runtime_paths.bot_runtime_file),
                    last_run=self._latest_run(profile_name),
                    active_command=active_command.label if active_command else "",
                ),
                reply_markup=self._menu_reply_markup(principal),
            )
            return

        if command in {"/hh_responses", "/hh_response_count"}:
            target_profile = self._profile(profile_name)
            await self._send_chat_action(chat_id)
            try:
                snapshot = await asyncio.to_thread(
                    hh_response_counter.refresh,
                    profile_name=profile_name,
                    home_dir=target_profile.home_dir,
                    cookies_file=target_profile.hh.cookies_file,
                    base_url=config.HH_BASE_URL,
                )
            except hh_response_counter.HHResponseCounterError as exc:
                log.warning(
                    "HH response counter failed for profile %s: %s",
                    profile_name,
                    exc,
                )
                await self._send_text(
                    chat_id,
                    f"❌ Не удалось обновить счётчик HH:\n{exc}",
                    reply_markup=self._menu_reply_markup(principal),
                )
                return
            except Exception:
                log.exception("Unexpected HH response counter failure for %s", profile_name)
                await self._send_text(
                    chat_id,
                    "❌ Не удалось обновить счётчик HH. Подробности записаны в лог бота.",
                    reply_markup=self._menu_reply_markup(principal),
                )
                return

            self._append_debug_log(
                "hh_response_counter_updated",
                profile_name=profile_name,
                active=snapshot.get("active", 0),
                archived=snapshot.get("archived", 0),
                deleted=snapshot.get("deleted", 0),
                total=snapshot.get("total", 0),
            )
            await self._send_text(
                chat_id,
                build_hh_response_count_text(snapshot),
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

        if command == "/research":
            import hiring_research
            profile = self._profile(profile_name)
            report = hiring_research.summarize(profile.analytics_events_file)
            for text in hiring_research.render(report):
                await self._send_text(chat_id, text, reply_markup=self._menu_reply_markup(principal))
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
            await self._send_run_summary(chat_id, principal, profile_name=profile_name, run_id=arg or None)
            return

        if command == "/raw_log":
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
            "🔐 Запускаю вход HH для выбранного профиля. Бот откроет браузер; если hh.ru попросит телефон или SMS-код, пришли ответ сюда в Telegram.",
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
                command_result = await self._run_active_command_capture(
                    active_command,
                    runtime_control.client_hh_auth_command_argv(profile_name, timeout_sec=900),
                    timeout=1800,
                )
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
                command_result = await self._run_active_command_capture(
                    active_command,
                    runtime_control.client_hh_auth_command_argv(profile_name, timeout_sec=900),
                    timeout=1800,
                )
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
                    debug_log_path=self.runtime_paths.bot_debug_log_file,
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
                        debug_log_path=self.runtime_paths.bot_debug_log_file,
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
                        f"• Журнал отладки: {self.runtime_paths.bot_debug_log_file}"
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
                result = await self._run_active_command_capture(
                    active_command,
                    argv,
                    timeout=1800,
                )
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
                result = await self._run_active_command_capture(
                    active_command,
                    argv,
                    timeout=900,
                )
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
        alternative: bool = False,
    ) -> None:
        role = principal.get("role", ROLE_USER)
        reply_markup = self._menu_reply_markup(principal)
        if self._has_active_command(profile_name):
            await self._send_busy_status(chat_id, principal, profile_name=profile_name)
            return

        label = "chat AI send" if force_send else "chat AI alternative" if alternative else "chat AI reply"
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
                if alternative:
                    argv.append("--chat-alternative")
                self._append_chat_ai_audit_event(
                    "command_start",
                    action="send" if force_send else "preview",
                    profile_name=profile_name,
                    hh_chat_id=hh_chat_id,
                    hh_message_id=hh_message_id,
                    force_send=force_send,
                    allow_any=allow_any,
                    alternative=alternative,
                )
                result = await self._run_active_command_capture(
                    active_command,
                    argv,
                    timeout=1200,
                )
                self._append_chat_ai_audit_event(
                    "command_result",
                    action="send" if force_send else "preview",
                    profile_name=profile_name,
                    hh_chat_id=hh_chat_id,
                    hh_message_id=hh_message_id,
                    force_send=force_send,
                    allow_any=allow_any,
                    alternative=alternative,
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
                        alternative=alternative,
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
                        alternative=alternative,
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
                    alternative=alternative,
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
                    alternative=alternative,
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
                result = await self._run_active_command_capture(
                    active_command,
                    runtime_control.agent_command_argv(profile_name, "--manual-apply-token", token),
                    timeout=1800,
                )
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
                result = await self._run_active_command_capture(
                    active_command,
                    runtime_control.agent_command_argv(profile_name, flag),
                    timeout=timeout,
                )
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
    runtime_paths = _telegram_runtime_paths()
    _configure_logging(force=True, runtime_paths=runtime_paths)
    bot = TelegramBot(
        profile_name=args.profile,
        drop_pending=not args.keep_pending,
        runtime_paths=runtime_paths,
    )
    try:
        await bot.run()
    finally:
        from llm_client import close_llm_client
        await close_llm_client()


if __name__ == "__main__":
    asyncio.run(main())
