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
from direction_views import DirectionView
import config
from private_logging import PrivateFileHandler, NonblockingLogHandler, OperationalFormatter, chat_log_path
from state_store.private_journal import append_json, read_json_records
from state_store.locking import StateLockBusy
import company_blacklist
import profile as profile_mod
import profile_access
import runtime_control
import search_query_suggester
import seen
from chat_reply_result import parse_reply_result
import telegram_access
import telegram_clients
import telegram_resume_limits
from runtime_context import TelegramRuntimePaths
from state_store.json_store import JsonStore
from state_store.locking import nonblocking_state_locks
from state_store.bot_state import BotStateStore
from telegram_app.auth_bridge import (
    TelegramHHAuthBridge,
    can_answer_hh_auth_prompt as _can_answer_hh_auth_prompt,
    clean_hh_auth_code_text as _clean_hh_auth_code_text,
    looks_like_standalone_hh_auth_code as _looks_like_standalone_hh_auth_code,
)
from telegram_app.api import TelegramAPIClient, TelegramAPIError, telegram_error_summary
from telegram_app.search import TelegramSearch
from telegram_app.review import TelegramReview
from telegram_app.responses import TelegramResponses, _build_chat_ai_candidates_markup, _build_chat_ai_candidates_text, _chat_ai_candidate_callback_data, _parse_chat_candidates_stdout, _parse_manual_chat_ai_arg, _safe_chat_callback_profile  # compatibility aliases
from telegram_app.candidate import TelegramCandidate
from telegram_app.monitoring import TelegramMonitoring
from telegram_app.schedule import TelegramSchedule
from telegram_app.admin import TelegramAdmin
from telegram_app.callbacks import TelegramCallbackRouter
from telegram_app.forms import TelegramFormEditor
from telegram_app.directions import TelegramDirections
from telegram_app.chat_ownership import TelegramChatOwnership
from telegram_app.command_queue import TelegramCommandQueue, profile_command, current_entry
from telegram_app.updates import TelegramUpdateSupervisor
from telegram_app.routing import COMMAND_ROUTES, route_for_command
from telegram_app.guards import read_command
from telegram_app.subprocesses import (
    ActiveCommandState,
    TelegramSubprocessManager,
)
from telegram_bot_ui import _is_menu_button_text  # compatibility alias
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
    for handler in handlers:
        handler.setFormatter(OperationalFormatter())
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[NonblockingLogHandler(handlers)],
        force=force,
    )
    logging.getLogger("chat_responder").propagate = True


_configure_logging()


class TelegramBot(
    TelegramUpdateSupervisor,
    TelegramAPIClient,
    TelegramSubprocessManager,
    TelegramCommandQueue,
    TelegramHHAuthBridge,
    TelegramCallbackRouter,
    TelegramFormEditor,
    TelegramDirections,
    TelegramChatOwnership,
    TelegramAdmin,
    TelegramSchedule,
    TelegramMonitoring,
    TelegramCandidate,
    TelegramResponses,
    TelegramReview,
    TelegramSearch,
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
        self._init_command_queue()
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
        self._foreground_task: asyncio.Task | None = None

    @nonblocking_state_locks
    async def run(self) -> None:
        if not config.TELEGRAM_CONTROL_BOT_TOKEN:
            raise RuntimeError("Telegram bot requires HUNTER_CONTROL_BOT_TOKEN")

        telegram_access.load_registry()
        runtime_control.register_current_process(
            self.runtime_paths.bot_pid_file,
            expected_tokens=runtime_control.BOT_TOKENS,
        )
        self._write_runtime("bot_start", "Бот Telegram запущен", "idle")
        await asyncio.to_thread(self._recover_manual_applies)
        log.info("Telegram consumer started profile=%s log_file=%s update_timeout=%ss",
                 self.profile_name, self.runtime_paths.bot_log_file, self.UPDATE_TIMEOUT_SECONDS)

        loop = asyncio.get_running_loop()
        shutdown_task: asyncio.Task | None = None
        polling_task: asyncio.Task | None = None

        async def _poll(awaitable):
            nonlocal polling_task
            polling_task = loop.create_task(awaitable)
            try:
                return await polling_task
            except asyncio.CancelledError:
                if not self._stop_event.is_set():
                    raise
                return []
            finally:
                polling_task = None

        def _signal_handler() -> None:
            nonlocal shutdown_task
            self._stop_event.set()
            # Cancel only the request: shared sessions must remain open for notices.
            if polling_task is not None:
                polling_task.cancel()
            if self._foreground_task is not None and not self._foreground_task.cancelling():
                self._foreground_task.cancel()
            if shutdown_task is None:
                shutdown_task = loop.create_task(self._shutdown_command_queue())

        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _signal_handler)

        try:
            await _poll(self._bootstrap_offset())
            while not self._stop_event.is_set():
                try:
                    updates = await _poll(self._get_updates())
                except Exception as exc:
                    if self._stop_event.is_set():
                        break
                    self._record_poll_result(exc)
                    await asyncio.sleep(5)
                    continue

                if self._stop_event.is_set():
                    break
                self._record_poll_result()
                for update in updates:
                    if self._stop_event.is_set():
                        break
                    await self._run_update(update)
                if not self._stop_event.is_set():
                    self._schedule_daily_summary()
                    await self._run_foreground(self._maybe_send_health_alert)
        finally:
            if shutdown_task is not None:
                await shutdown_task
            await self._shutdown_read_updates()
            await self._shutdown_command_queue()
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
                f"✅ Для профиля {_pretty_profile_name(target_profile)} сейчас нет активной команды.\n"
                f"В очереди: {self._queue_length(target_profile)}"
                + (" · ⏸ на паузе" if target_profile in self._queue_paused else ""),
                reply_markup=self._menu_reply_markup(principal),
            )
            return
        await self._send_text(
            chat_id,
            build_busy_status_text(
                active_command=command.label,
                queue_length=self._queue_length(target_profile),
                profile_name=command.profile_name,
                elapsed_sec=self._active_elapsed_sec(command),
                pid=command.subprocess_pid,
                can_cancel=self._can_cancel_active(principal, command),
            ),
            reply_markup=self._menu_reply_markup(principal),
        )

    def _selected_profile(self, principal: dict) -> str:
        if principal.get("role") == telegram_access.ROLE_TESTER:
            profile_name, _ = profile_access.require_tester_binding(principal)
            return profile_name
        default_profile = self._default_profile_name()
        if principal.get("role") != ROLE_ADMIN:
            available_profiles = set(self._profile_names())
            user_id = int(principal.get("user_id") or 0)
            client = self._client_record(user_id) if user_id > 0 else None
            client_profile = (client.get("profile_name") or "").strip() if client else ""
            if client_profile in available_profiles:
                profile_access.require_profile(client_profile)
                return client_profile
            selected = (principal.get("profile") or "").strip()
            if selected == "default" and default_profile != "default":
                return default_profile
            profile_access.require_profile(selected or default_profile)
            return selected or default_profile
        state = self._load_state()
        selected = (((state.get("user_state") or {}).get(str(principal["user_id"])) or {}).get("selected_profile") or "").strip()
        principal_profile = (principal.get("profile") or "").strip()
        if principal_profile == "default" and default_profile != "default":
            principal_profile = default_profile
        resolved = selected or principal_profile or default_profile
        if not profile_access.profile_allowed(resolved):
            # A shared registry or state left by another instance must not
            # strand the admin before they can open /profiles again.
            resolved = default_profile
        profile_access.require_profile(resolved)
        return resolved

    def _set_selected_profile(self, user_id: int, profile_name: str) -> None:
        profile_access.require_profile(profile_name)
        if profile_access.current_scope()[2]:
            raise PermissionError("Tester не может переключать профиль.")
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

    def _effective_application_mode(self, principal: dict) -> str:
        _, profile, direction = self._direction_context(principal)
        return (direction or {}).get("application_mode") or profile.hh.application_mode

    def _menu_reply_markup(self, principal: dict, *, menu: str | None = None, profile_buttons: list[str] | None = None) -> dict:
        role = principal.get("role", ROLE_USER)
        selected_menu = _normalize_menu(role, menu or self._selected_menu(principal))
        profile_name = self._selected_profile(principal)
        active_command = self._active_command(profile_name)
        active = active_command is not None
        can_stop = self._can_cancel_active(principal, active_command) if active else False
        mode = self._effective_application_mode(principal) if selected_menu == MENU_APPLICATION_MODE else ""
        return build_reply_markup(
            role,
            menu=selected_menu,
            profile_buttons=profile_buttons,
            active=active,
            can_stop=can_stop,
            application_mode=mode,
        )

    def _default_client_profile_name(self, user_id: int) -> str:
        suffix = "-dev" if profile_access.instance_kind() == "dev" else ""
        return f"client_{int(user_id)}{suffix}"

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
        return [name for name in visible if profile_access.profile_allowed(name)]

    def _recover_manual_applies(self) -> None:
        for name in self._profile_names():
            try:
                manual_apply_queue.recover_orphaned_applies(profile_name=name)
            except Exception:
                log.exception("Manual apply recovery failed profile=%s", name)

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

    async def _send_getting_started(self, chat_id: int, principal: dict, *, only_if_incomplete: bool = False) -> None:
        from telegram_app import onboarding
        import directions as directions_mod
        profile_name = self._selected_profile(principal)
        profile = self._profile(profile_name)
        from hh.session_probe import probe, save_catalog, OK
        checked = await asyncio.to_thread(probe, profile.hh.cookies_file)
        resumes = checked["resumes"] if checked["status"] == OK else []
        if checked["status"] == OK:
            try:
                save_catalog(client_hh_auth.hh_resume_catalog_path(profile_name), resumes)
            except Exception as exc:
                log.warning("resume catalog cache refresh failed: %s", type(exc).__name__)
        try:
            items = directions_mod.list_directions(profile.home_dir)
            active = directions_mod.active_direction(profile.home_dir)
        except Exception:
            items, active = [], None
        text, rows, complete = onboarding.build(
            hh_ok=checked["status"] == OK, hh_message=checked["message"],
            resumes=[r for r in resumes if isinstance(r, dict)],
            directions=items, active=active,
            mode=str((active or {}).get("application_mode") or profile.hh.application_mode or ""),
            has_search=runtime_control.has_completed_search(profile.run_history_file),
        )
        if only_if_incomplete and complete:
            return
        markup = {"inline_keyboard": [[{"text": label, "callback_data": onboarding.CALLBACK_PREFIX + action}
                                       for label, action in row] for row in rows]}
        await self._send_text(chat_id, text, reply_markup=markup)

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

    def _daily_summary_report(self, profile_name, *, now):
        snapshot = self._daily_summary_snapshot(profile_name, now=now)
        return build_daily_summary_text(
            profile_name=profile_name, analytics_summary=snapshot["analytics_summary"],
            recent_runs=snapshot["recent_runs"], period_label=snapshot["period_label"])

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

        daily = self._load_state().get("daily_summary") or {}
        delivered = dict(daily.get("delivered") or {}) if daily.get("delivery_date") == today else {}
        ordered = sorted(recipients)
        cursor = daily.get("cursor") if daily.get("delivery_date") == today else None
        if cursor in ordered:
            position = ordered.index(cursor) + 1
            ordered = ordered[position:] + ordered[:position]

        def checkpoint(state, user_id, delivered_profile=None):
            summary = state.setdefault("daily_summary", {})
            if summary.get("delivery_date") != today:
                summary.update(delivery_date=today, delivered={})
            summary["cursor"] = user_id
            if delivered_profile is not None:
                summary["delivered"][str(user_id)] = delivered_profile

        for user_id in ordered:
            principal = recipients[user_id]
            profile_name = str(principal.get("profile") or self.profile_name or "default")
            if delivered.get(str(user_id)) == profile_name:
                continue
            # Persist the cursor before the cancellable send. On the next poll,
            # later recipients get a turn before retrying this recipient.
            self._update_state(lambda state: checkpoint(state, user_id))
            try:
                with profile_access.principal_scope(principal):
                    # A timed-out supervisor must not reopen admission while its
                    # report thread still waits for the shared journal index.
                    report = asyncio.create_task(asyncio.to_thread(
                        self._daily_summary_report, profile_name, now=datetime.now()))
                    self._daily_summary_report_task = report
                    report.add_done_callback(lambda task: task.exception() if not task.cancelled() else None)
                    text = await asyncio.shield(report)
                    result = await self._send_text_safely(
                        user_id,
                        text,
                        reply_markup=self._menu_reply_markup(principal),
                    )
                if result is not None:
                    self._update_state(lambda state: checkpoint(state, user_id, profile_name))
                    delivered[str(user_id)] = profile_name
            except Exception as exc:
                log.warning("daily summary failed user=%s profile=%s: %s", user_id, profile_name, exc)

        if not recipients or any(delivered.get(str(uid)) != item["profile"] for uid, item in recipients.items()):
            return
        def record_summary(state):
            state["daily_summary"].update({
                "last_sent_date": today,
                "last_sent_at": datetime.now().isoformat(timespec="seconds"),
                "recipients": len(delivered),
            })

        self._update_state(record_summary)
        self._append_debug_log("daily_summary_sent", date=today, recipients=len(delivered))

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
        # A busy bot-state lock must not stop polling (T31 critic): the offset of a fetched batch is kept in
        # memory, so the batch is handled once and answered now; the durable checkpoint is retried every poll.
        try:
            saved = self._load_state().get("last_update_id")
        except StateLockBusy:
            saved = None
            log.warning("Telegram offset read deferred: bot state lock is busy")
        polled = getattr(self, "_polled_update_id", None)
        known = [int(x) for x in (saved, polled) if x is not None]
        payload = {
            "timeout": config.TELEGRAM_BOT_POLL_TIMEOUT if timeout is None else timeout,
            "allowed_updates": ["message", "callback_query"],
        }
        if known:
            payload["offset"] = max(known) + 1
        result = await self._api_request("getUpdates", payload, timeout=payload["timeout"] + 20)
        updates = result if isinstance(result, list) else []
        if not save_state:
            return updates
        if updates:
            self._polled_update_id = max(known + [int(updates[-1]["update_id"])])
        if getattr(self, "_polled_update_id", None) is not None and (updates or getattr(self, "_offset_unsaved", False)):
            self._checkpoint_offset()
        return updates

    def _checkpoint_offset(self) -> None:
        polled = self._polled_update_id

        def advance_offset(latest):
            current = latest.get("last_update_id")
            latest["last_update_id"] = max(int(current) if current is not None else 0, polled)
            latest["updated_at"] = datetime.now().isoformat(timespec="seconds")

        try:
            self._update_state(advance_offset)
            self._offset_unsaved = False
        except StateLockBusy:
            self._offset_unsaved = True
            log.warning("Telegram offset checkpoint deferred: bot state lock is busy")

    async def _send_text(self, chat_id: int, text: str, *, reply_markup: dict | None = None) -> dict | None:
        entry = current_entry()
        if entry is not None:
            entry.last_message = text
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

    async def _send_menu(self, chat_id: int, principal: dict, *, profile_name: str, menu: str = MENU_MAIN, help_section: bool = False) -> None:
        role = principal.get("role", ROLE_USER)
        selected_menu = _normalize_menu(role, menu)
        self._set_selected_menu(principal["user_id"], selected_menu)
        await self._send_text(
            chat_id,
            build_help_text(role, profile_name=profile_name, help_section=help_section) if selected_menu == MENU_MAIN else build_menu_section_text(selected_menu, role=role, profile_name=profile_name),
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

    @telegram_access.scoped_request
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

        if await self._accept_employer_question_text(chat_id, principal, text):
            return

        if await self._accept_direction_input(chat_id, principal, text):
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

    @read_command
    @profile_command
    @telegram_access.scoped_request
    async def _dispatch(self, chat_id: int, principal: dict, command: str, arg: str) -> None:
        role = principal.get("role", ROLE_USER)
        profile_name = self._selected_profile(principal)
        active_command = self._active_command(profile_name)
        route = route_for_command(command)

        if route and route.phase == "impersonation" and not (role == ROLE_ADMIN or principal.get("impersonating")):
            route = None
        if route and route.phase in {"early", "impersonation"}:
            await getattr(self, route.handler)(chat_id, principal, command, arg, profile_name, None)
            return
        if (current_entry() is None and (active_command or self._queue_launching.get(profile_name)
                                         or (self._queue_length(profile_name) and command not in {"/hh_auth", "/client_hh_auth"}))
                and _command_conflicts_with_active(command)):
            await self._send_busy_status(chat_id, principal, profile_name=profile_name)
            return

        if route and route.phase == "public":
            await getattr(self, route.handler)(chat_id, principal, command, arg, profile_name, None)
            return

        if command in ADMIN_ONLY_COMMANDS and role != ROLE_ADMIN:
            await self._send_text(chat_id, "🔒 Нужны права администратора.", reply_markup=self._menu_reply_markup(principal))
            return

        if route and route.phase == "direction":
            await getattr(self, route.handler)(chat_id, principal, command, arg, profile_name, None)
            return

        if route and route.direction_command:
            import directions
            pending = self._direction_sessions().get(principal["user_id"])
            if directions.active_direction(self._profile(profile_name).home_dir) or (
                command == "/search_use_draft" and pending
            ):
                direction_menu = reply_menu_for_command(role, command)
                if direction_menu is not None:
                    self._set_selected_menu(principal["user_id"], direction_menu)
                await self._dispatch_direction(chat_id, principal, route.direction_command, arg)
                return

        reply_menu = reply_menu_for_command(role, command)
        if reply_menu is not None:
            self._set_selected_menu(principal["user_id"], reply_menu)

        handler = route.handler if route else "_route_unknown"
        await getattr(self, handler)(chat_id, principal, command, arg, profile_name, reply_menu)

    # Hh & resumes routes.

    async def _route_search_resume(self, chat_id, principal, command, arg, profile_name, reply_menu):
        self._set_selected_menu(principal["user_id"], reply_menu)
        catalog = client_hh_auth.load_hh_resume_catalog(profile_name)
        if not catalog:
            await self._send_text(
                chat_id,
                "🧾 Пока нет списка резюме HH. Сначала нажмите «Вход HH», затем повторите попытку.",
                reply_markup=self._menu_reply_markup(principal, menu=reply_menu),
            )
            return
        lines = ["🧾 Выберите резюме для поиска. Отправьте его номер:", ""]
        for index, item in enumerate(catalog, start=1):
            title = str(item.get("title") or "Резюме без названия")
            lines.append(f"{index}. {title}")
        lines.append("\nДля отмены отправьте «Отмена».")
        self._set_search_settings_state(principal["user_id"], search_input="resume", search_input_profile=profile_name)
        await self._send_text(
            chat_id,
            "\n".join(lines),
            reply_markup=self._menu_reply_markup(principal, menu=reply_menu),
        )
        return

    async def _route_hh_auth(self, chat_id, principal, command, arg, profile_name, reply_menu):
        role = principal.get("role", ROLE_USER)
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

    async def _route_hh_resumes(self, chat_id, principal, command, arg, profile_name, reply_menu):
        client = self._client_record(principal["user_id"])
        target_profile = profile_name
        if client and client.get("profile_name"):
            target_profile = client["profile_name"]
        from hh.session_probe import probe, save_catalog, OK
        checked = await asyncio.to_thread(probe, self._profile(target_profile).hh.cookies_file)
        note = ""
        if checked["status"] == OK:
            try:
                save_catalog(client_hh_auth.hh_resume_catalog_path(target_profile), checked["resumes"])
            except Exception as exc:
                log.warning("resume catalog cache refresh failed: %s", type(exc).__name__)
        else:
            note = "⚠️ " + checked["message"] + "\nНиже — последний сохранённый список.\n\n"
        resumes = client_hh_auth.load_hh_resume_catalog(target_profile)
        await self._send_text(
            chat_id,
            note + build_hh_resumes_text(target_profile, resumes),
            reply_markup=self._menu_reply_markup(principal),
        )
        return

    # Navigation routes.

    async def _route_busy(self, chat_id, principal, command, arg, profile_name, reply_menu):
        await self._send_busy_status(chat_id, principal, profile_name=profile_name)
        return

    async def _route_back(self, chat_id, principal, command, arg, profile_name, reply_menu):
        parent = MENU_PARENTS.get(self._selected_menu(principal), MENU_MAIN)
        await self._send_menu(chat_id, principal, profile_name=profile_name, menu=parent)
        return

    async def _route_main(self, chat_id, principal, command, arg, profile_name, reply_menu):
        if principal.get("impersonating"):
            await self._send_text(chat_id, f"👤 Режим «смотреть как клиент»: профиль {profile_name}. Вернуться: /act_as_self")
        await self._send_menu(chat_id, principal, profile_name=profile_name, menu=MENU_MAIN, help_section=command == "/help")
        if command == "/start":
            await self._send_getting_started(chat_id, principal, only_if_incomplete=True)
        return

    async def _route_getting_started(self, chat_id, principal, command, arg, profile_name, reply_menu):
        role = principal.get("role", ROLE_USER)
        owner_menu = reply_menu_for_command(role, "/getting_started") or MENU_MAIN
        self._set_selected_menu(principal["user_id"], owner_menu)
        await self._send_text(chat_id, "🚀 С чего начать — пошаговая настройка.",
                              reply_markup=self._menu_reply_markup(principal, menu=owner_menu))
        await self._send_getting_started(chat_id, principal)
        return

    async def _route_menu(self, chat_id, principal, command, arg, profile_name, reply_menu):
        await self._send_menu(chat_id, principal, profile_name=profile_name, menu=COMMAND_ROUTES[command].args[0])
        return

    async def _route_unknown(self, chat_id, principal, command, arg, profile_name, reply_menu):
        await self._send_text(chat_id, f"❓ Неизвестная команда: {command}", reply_markup=self._menu_reply_markup(principal))

    @profile_command
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

        active_command.task = self._create_active_task(active_command, runner)

    @profile_command
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
                reply_markup=build_reply_markup(ROLE_USER, menu=MENU_HH),
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
                        reply_markup=build_reply_markup(ROLE_USER, menu=MENU_HH),
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
                        reply_markup=build_reply_markup(ROLE_USER, menu=MENU_HH),
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
                        reply_markup=build_reply_markup(ROLE_USER, menu=MENU_HH),
                    )
            finally:
                self._clear_active_command(profile_name)

        active_command.task = self._create_active_task(active_command, runner)

    async def _authorize_ai_usage(self, chat_id: int, principal: dict, *, profile_name: str, action: str,
                                 consume: bool = True, check_only: bool = False) -> bool:
        if self._commands_stopping():
            return False
        if principal.get("role") == ROLE_ADMIN:
            return True
        if profile_name != self._selected_profile(principal):
            await self._send_text(chat_id, "🔒 Этот профиль вам недоступен.")
            return False
        # Preflight before progress delivery; charge atomically inside the runner.
        if not consume:
            return True
        if check_only:
            allowed = telegram_resume_limits.get_user_snapshot(principal["user_id"])["available_soft"] > 0
        else:
            allowed = telegram_resume_limits.consume_ai_usage(
                principal["user_id"], profile_name=profile_name, action=action)
        if allowed:
            return True
        await self._send_text(chat_id, "🎁 Лимит ИИ исчерпан. Попросите администратора добавить доступ через «Лимиты ИИ».",
                              reply_markup=self._menu_reply_markup(principal))
        return False

    @profile_command
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
                f"🟢 Поиск уже работает для профиля {profile_name}. Сначала остановите его через /daemon_off.",
                reply_markup=reply_markup,
            )
            return

        if flag == "--analyze-resume" and not await self._authorize_ai_usage(
            chat_id, principal, profile_name=profile_name, action="analyze_ai_used", check_only=True,
        ):
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
                if flag == "--analyze-resume" and not await self._authorize_ai_usage(
                    chat_id, principal, profile_name=profile_name, action="analyze_ai_used",
                ):
                    return
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
                        consume_credit=False,
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
                        message = f"{message}\n\n🕓 Последний поиск:\n{format_run_summary(last_run)}"
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

        active_command.task = self._create_active_task(active_command, runner)


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
