"""In-memory FIFO admission for bot commands. Agent validation remains authoritative."""
from __future__ import annotations

import asyncio
import contextlib
import contextvars
from dataclasses import dataclass, field
from datetime import datetime, timezone
from functools import wraps
import inspect
import json
import logging
import os
import time
import uuid

import telegram_access
import profile_access
from state_store.locking import StateLockBusy, nonblocking_state_locks
from telegram_app.updates import STATE_BUSY
from telegram_app.guards import AI_LABELS, BUSY, reserve, release_entry, release_callback

log = logging.getLogger(__name__)
_invocation = contextvars.ContextVar("bot_queue_invocation", default=None)

# Button commands and their direct starter equivalents share these keys.
PERIODIC = {
    "--search": "search", "--dry-run": "dry-run",
    "--check": "check invitations", "--digest": "digest",
}
DISPATCH = {
    "/search": "search", "/dryrun": "dry-run", "/check": "check invitations",
    "/analyze": "analyze resume", "/backfill": "analytics backfill",
    "/grabresume": "grab resume", "/refresh_resume": "refresh resume",
    "/search_suggest": "suggest search queries", "/chat_ai": "chat AI reply",
    "/ai_chat": "chat AI reply", "/chat_answer": "chat AI reply",
}
CALLBACKS = {
    "manual_apply": "manual AI apply", "chat_ai": "chat AI reply",
    "chat_ai_any": "chat AI reply", "chat_ai_alt": "chat AI alternative",
    "chat_ai_any_alt": "chat AI alternative", "chat_send": "chat AI send",
    "chat_send_any": "chat AI send", "gform_preview": "google form preview",
    "gform_submit": "google form submit",
}


@dataclass
class QueueEntry:
    profile: str
    chat_id: int
    user_id: int
    label: str
    invoke: object
    key: str = ""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:16])
    created_at: float = field(default_factory=time.monotonic)
    queued: bool = False
    source: dict | None = None
    last_message: str = ""
    outcome: str = ""
    ready: bool = False
    recovery: bool = False
    request_key: tuple | None = None


def current_entry():
    value = _invocation.get()
    return value[1] if value and value[0] is asyncio.current_task() else None


def profile_command(fn):
    """Wrap original entry points, retaining the full callback/dispatch for replay."""
    signature = inspect.signature(fn)

    @wraps(fn)
    async def wrapped(self, *args, **kwargs):
        if current_entry() is not None:
            if self._commands_stopping():
                entry = current_entry()
                await self._queue_notice(entry.chat_id, "Очередь сброшена при перезапуске, нажмите снова.")
                return False
            return await fn(self, *args, **kwargs)
        bound = signature.bind(self, *args, **kwargs)
        bound.apply_defaults()
        values = bound.arguments
        source = None
        if fn.__name__ == "_handle_callback_query":
            callback = values["callback"]
            parts = str(callback.get("data") or "").split(":")
            if parts[0] == "queue":
                return await self._queue_callback(callback)
            label = CALLBACKS.get(parts[0])
            if parts[0] == "gf" and len(parts) == 5 and parts[1] in {"check", "send"}:
                label = "Проверка и отправка Google Form" if parts[1] == "send" else "Проверка черновика Google Form"
                profile = parts[2]
            elif label and len(parts) >= 3:
                profile = parts[1]
            else:
                return await fn(self, *args, **kwargs)
            principal = telegram_access.resolve_user(int((callback.get("from") or {}).get("id") or 0))
            message = callback.get("message") or {}
            chat_id = int((message.get("chat") or {}).get("id") or 0)
            # Keep the router's existing rejection messages for invalid access.
            if (not principal or profile not in self._profile_names() or chat_id <= 0
                    or (principal.get("role") != "admin" and (
                        self._selected_profile(principal) != profile
                        or (message.get("chat") or {}).get("type") != "private"))):
                return await fn(self, *args, **kwargs)
            source = callback
        else:
            principal = values["principal"]
            chat_id = values["chat_id"]
            profile = values.get("profile_name") or self._selected_profile(principal)
            if fn.__name__ == "_start_hh_auth_capture":
                client = values["client"]
                profile = client.get("profile_name") or self._default_client_profile_name(int(client["user_id"]))
            if fn.__name__ == "_dispatch":
                label = DISPATCH.get(values["command"])
                if not label:
                    return await fn(self, *args, **kwargs)
            else:
                labels = {
                    "_start_profile_hh_auth_capture": "hh auth capture",
                    "_start_hh_auth_capture": "hh auth capture",
                    "_start_manual_ai_apply": "manual AI apply",
                    "_start_search_query_suggestion": "suggest search queries",
                    "_start_chat_ai_candidate_list": "chat AI list",
                    "_start_chat_ai_reply": ("chat AI send" if values.get("force_send") else
                                             "chat AI alternative" if values.get("alternative") else "chat AI reply"),
                }
                label = values["label"] if "label" in values else labels[fn.__name__]
        key = label if label in PERIODIC.values() else ""

        async def invoke(revalidate):
            # A drainer may have been scheduled by a different user's request.
            # Rebind to this entry's authenticated principal before any lookup.
            with profile_access.principal_scope(None):
                fresh = telegram_access.resolve_user(int(principal["user_id"])) if revalidate else principal
                with profile_access.principal_scope(fresh):
                    if revalidate:
                        if (not fresh or profile not in self._profile_names()
                                or (fresh.get("role") != "admin" and self._selected_profile(fresh) != profile)
                                or (fn.__name__ == "_dispatch" and self._selected_profile(fresh) != profile)):
                            await self._send_text(chat_id, "Пропуск: профиль или доступ изменились.")
                            return
                        if "principal" in values:
                            values["principal"] = fresh
                    return await fn(*bound.args, **bound.kwargs)

        entry = QueueEntry(profile, chat_id, int(principal["user_id"]), label, invoke, key, source=source,
                           recovery=fn.__name__ in {"_start_profile_hh_auth_capture", "_start_hh_auth_capture"})
        if label in AI_LABELS:
            identity = source.get("data") if source else tuple(
                (name, values[name]) for name in ("arg", "token", "hh_chat_id", "hh_message_id") if name in values)
            entry.request_key = (entry.user_id, "ai", label, identity)
            if not reserve(self, entry.request_key):
                if source:
                    await self._answer_callback_query(str(source.get("id") or ""), BUSY)
                else:
                    await self._send_text(chat_id, BUSY)
                return False
        try:
            accepted = await self._admit_command(entry)
        except BaseException:
            release_entry(self, entry)
            raise
        if not accepted:
            release_entry(self, entry)
        return accepted
    return wraps(fn)(telegram_access.scoped_request(wrapped))


class TelegramCommandQueue:
    def _init_command_queue(self):
        self._command_queues = {}
        self._queue_launching = {}
        self._queue_drainers = {}
        self._queue_paused = {}
        self._queue_stop_reasons = {}
        self._queue_resume_profiles = {}
        self._queue_history = {}
        self._queue_stopping = False

    def _commands_stopping(self):
        return self._queue_stopping or bool(getattr(self, "_stop_event", None) and self._stop_event.is_set())

    def _queue_length(self, profile):
        return len(self._command_queues.get(profile, []))

    async def _queue_notice(self, chat_id, text, **kwargs):
        try:
            return await self._send_text(chat_id, text, **kwargs)
        except Exception:
            log.warning("Queue notification failed for chat %s", chat_id, exc_info=True)

    async def _queue_markup(self, entry, *, restore=False):
        if not entry.source:
            return
        message = entry.source.get("message") or {}
        message_id = int(message.get("message_id") or 0)
        if not message_id:
            return
        markup = message.get("reply_markup")
        if restore and not markup:
            markup = {"inline_keyboard": [[{"text": "Откликнуться с ИИ" if entry.label == "manual AI apply" else entry.label,
                                            "callback_data": entry.source["data"]}]]}
        try:
            await self._edit_reply_markup(entry.chat_id, message_id, reply_markup=markup if restore else None)
        except Exception:
            log.warning("Queue button update failed", exc_info=True)

    async def _admit_command(self, entry):
        profile = entry.profile
        profile_access.require_profile(profile)
        active = self._active_command(profile)
        launching = self._queue_launching.get(profile)
        waiting = self._command_queues.setdefault(profile, [])
        if self._commands_stopping():
            await self._queue_notice(entry.chat_id, "Очередь сброшена при перезапуске, нажмите снова.")
            return False
        if entry.key and (any(item.key == entry.key for item in waiting)
                          or launching and launching.key == entry.key
                          or active and active.label == entry.key):
            await self._queue_notice(entry.chat_id, f"{entry.label}: уже идёт/в очереди.")
            return False
        if entry.recovery:
            # Explicit session repair is outside FIFO, so a paused queue can be repaired.
            # It still reserves the same profile before any network/authorization await.
            if active or launching:
                await self._send_busy_status(entry.chat_id, {"user_id": entry.user_id}, profile_name=profile)
                return False
            entry.ready = True
            self._queue_launching[profile] = entry
            return await self._invoke_queue_entry(entry, revalidate=False)
        if active or launching or waiting or profile in self._queue_paused:
            limit = max(0, int(os.environ.get("BOT_COMMAND_QUEUE_MAX", "10")))
            if len(waiting) >= limit:
                await self._queue_notice(entry.chat_id, "Очередь полна. Нажмите снова позже.")
                return False
            # No awaits between checking admission and appending: one event-loop owner.
            entry.queued = True
            waiting.append(entry)
            self._queue_history.setdefault(profile, []).append(entry)
            position = len(waiting)
            after = active.label if active else launching.label if launching else "пауза" if profile in self._queue_paused else waiting[0].label
            try:
                await self._queue_markup(entry)
                if self._commands_stopping():
                    await self._queue_markup(entry, restore=True)
                    return False
                await self._queue_notice(entry.chat_id, f"📥 В очереди №{position} после «{after}»",
                                         reply_markup={"inline_keyboard": [[{"text": "Убрать из очереди", "callback_data": f"queue:remove:{entry.id}"}]]})
                if entry.source:
                    with contextlib.suppress(Exception):
                        await self._answer_callback_query(entry.source.get("id", ""))
                if self._commands_stopping():
                    await self._queue_markup(entry, restore=True)
                    return False
                entry.ready = True
                self._schedule_queue(profile)
                return True
            finally:
                if not entry.ready:
                    # Cancellation can interrupt any notification above. Roll
                    # back admission without another network await in cleanup.
                    if entry in waiting:
                        waiting.remove(entry)
                    history = self._queue_history.get(profile, [])
                    if entry in history:
                        history.remove(entry)
                    self._schedule_queue(profile)
        entry.ready = True
        self._queue_launching[profile] = entry
        return await self._invoke_queue_entry(entry, revalidate=False)

    async def _invoke_queue_entry(self, entry, *, revalidate):
        token = _invocation.set((asyncio.current_task(), entry))
        try:
            await entry.invoke(revalidate)
            active = self._active_commands.get(entry.profile)
            if active:
                await self._queue_markup(entry)
                return True
            entry.outcome = "пропуск: " + (entry.last_message.replace("\n", " ")[:180] or "запуск отклонён проверками")
            if entry.queued:
                await self._queue_markup(entry, restore=True)
            return False
        except asyncio.CancelledError:
            active = self._active_commands.get(entry.profile)
            if active and active.task is None:
                self._active_commands.pop(entry.profile, None)
            raise
        except Exception as exc:
            entry.outcome = f"ошибка запуска: {type(exc).__name__}"
            # A progress delivery error must not leave a taskless active slot behind.
            active = self._active_commands.get(entry.profile)
            if active and active.task is None:
                self._active_commands.pop(entry.profile, None)
            await self._queue_markup(entry, restore=True)
            await self._queue_notice(entry.chat_id, STATE_BUSY if isinstance(exc, StateLockBusy)
                                     else entry.outcome + ". Нажмите снова.")
            if isinstance(exc, StateLockBusy) and entry.source:
                await self._answer_callback_query(str(entry.source.get("id") or ""), STATE_BUSY)
            log.exception("Queue launch failed")
            return False
        finally:
            _invocation.reset(token)
            self._queue_launching.pop(entry.profile, None)
            if not self._active_commands.get(entry.profile):
                release_entry(self, entry)
            self._schedule_queue(entry.profile)

    def _schedule_queue(self, profile):
        if self._queue_stopping:
            return
        existing = self._queue_drainers.get(profile)
        if existing and not existing.done():
            return
        if not self._command_queues.get(profile) and not self._queue_history.get(profile):
            return
        try:
            self._queue_drainers[profile] = asyncio.get_running_loop().create_task(self._drain_queue(profile))
        except RuntimeError:
            pass

    def _queue_guard_reason(self, profile):
        """Read explicit profile state; never activate process-global config or clear guards."""
        import hh_guard
        try:
            import captcha_bridge
            if captcha_bridge.peek_pending(profile_name=profile):
                return "HH ожидает решения captcha"
            path = os.path.join(self._profile(profile).home_dir, "hh_guard_state.json")
            if not os.path.exists(path):
                return ""
            state = hh_guard._store(path).load()
            blocked = hh_guard._parse_datetime(state.get("blocked_until", ""))
            if blocked and blocked > datetime.now(timezone.utc):
                return state.get("last_reason") or state.get("last_kind") or "Защита HH заблокирована"
            return ""
        except Exception:
            return "Состояние HH-guard недоступно"

    async def _pause_queue(self, profile, reason):
        if profile in self._queue_paused:
            return
        self._queue_paused[profile] = reason
        resume_token = uuid.uuid4().hex[:16]
        self._queue_resume_profiles[resume_token] = profile
        entries = self._command_queues.get(profile, [])
        chats = {entry.chat_id for entry in entries}
        try:
            chats.update(self._profile_recipient_ids(profile))
        except Exception:
            log.warning("Queue owner lookup failed for %s", profile, exc_info=True)
        for chat in chats:
            await self._queue_notice(chat, f"⏸ Очередь профиля {profile} на паузе: {reason}",
                                     reply_markup={"inline_keyboard": [[{"text": "Продолжить очередь", "callback_data": f"queue:resume:{resume_token}"}]]})

    @nonblocking_state_locks
    async def _drain_queue(self, profile):
        try:
            while not self._queue_stopping and not (getattr(self, "_stop_event", None) and self._stop_event.is_set()):
                if self._active_commands.get(profile) or profile in self._queue_launching:
                    return
                waiting = self._command_queues.get(profile, [])
                if not waiting:
                    self._queue_paused.pop(profile, None)
                    await self._queue_summary(profile)
                    return
                if profile in self._queue_paused:
                    return
                if profile in self._queue_stop_reasons:
                    await self._pause_queue(profile, self._queue_stop_reasons[profile])
                    return
                entry = waiting[0]
                if not entry.ready:
                    return
                ttl = max(0, float(os.environ.get("BOT_COMMAND_QUEUE_TTL_MINUTES", "120"))) * 60
                if time.monotonic() - entry.created_at >= ttl:
                    waiting.pop(0)
                    release_entry(self, entry)
                    release_callback(self, entry.source)
                    entry.outcome = "снята по TTL"
                    await self._queue_notice(entry.chat_id, f"{entry.label}: срок ожидания истёк (TTL), нажмите снова.")
                    await self._queue_markup(entry, restore=True)
                    continue
                reason = await asyncio.to_thread(self._queue_guard_reason, profile)
                # Admission/removal/shutdown can run while the read worker
                # waits for its lock. Revalidate the head before consuming it.
                if self._commands_stopping() or self._active_commands.get(profile) or profile in self._queue_launching:
                    return
                if not waiting or waiting[0] is not entry or profile in self._queue_paused or profile in self._queue_stop_reasons:
                    continue
                if reason:
                    await self._pause_queue(profile, reason)
                    return
                # Reserve before the first await. New arrivals must join the tail.
                self._queue_launching[profile] = entry
                waiting.pop(0)
                await self._invoke_queue_entry(entry, revalidate=True)
        finally:
            self._queue_drainers.pop(profile, None)

    async def _queue_summary(self, profile):
        if self._commands_stopping() or profile in self._queue_paused:
            return
        self._queue_resume_profiles = {token: name for token, name in self._queue_resume_profiles.items() if name != profile}
        history = self._queue_history.pop(profile, [])
        for chat_id in dict.fromkeys(entry.chat_id for entry in history):
            own = [entry for entry in history if entry.chat_id == chat_id]
            await self._queue_notice(chat_id, f"✅ Очередь выполнена · {profile} · задач: {len(own)}\n" +
                                     "\n".join(f"• {entry.label}: {entry.outcome or 'завершена'}" for entry in own))

    async def _queue_callback(self, callback):
        principal = telegram_access.resolve_user(int((callback.get("from") or {}).get("id") or 0))
        parts = str(callback.get("data") or "").split(":")
        if not principal or len(parts) != 3:
            await self._answer_callback_query(callback.get("id", ""), "Доступ закрыт.", show_alert=True)
            return
        action, target = parts[1:]
        entry = next((item for items in self._command_queues.values() for item in items if item.id == target), None)
        profile = entry.profile if entry else self._queue_resume_profiles.get(target, target)
        if not profile_access.profile_allowed(profile):
            await self._answer_callback_query(callback.get("id", ""), "Профиль недоступен.", show_alert=True)
            return
        allowed = principal.get("role") == "admin" or (entry and entry.user_id == int(principal["user_id"]))
        if action == "resume":
            allowed = principal.get("role") == "admin" or (self._selected_profile(principal) == profile and profile in self._queue_paused)
        if not allowed:
            await self._answer_callback_query(callback.get("id", ""), "Эта запись вам недоступна.", show_alert=True)
            return
        if action == "remove" and entry:
            self._command_queues[profile].remove(entry)
            release_entry(self, entry)
            release_callback(self, entry.source)
            entry.outcome = "удалена пользователем"
            await self._queue_markup(entry, restore=True)
            await self._answer_callback_query(callback.get("id", ""), "Удалена из очереди.")
        elif action == "resume" and profile in self._queue_paused:
            reason = self._queue_guard_reason(profile)
            if reason:
                await self._answer_callback_query(callback.get("id", ""), f"HH-guard: {reason}"[:180], show_alert=True)
                return
            self._queue_paused.pop(profile, None)
            self._queue_stop_reasons.pop(profile, None)
            self._queue_resume_profiles = {token: name for token, name in self._queue_resume_profiles.items() if name != profile}
            await self._answer_callback_query(callback.get("id", ""), "Продолжаю очередь.")
        else:
            await self._answer_callback_query(callback.get("id", ""), "Запись уже не ожидает.")
        self._schedule_queue(profile)

    async def _shutdown_command_queue(self):
        if self._queue_stopping:
            return
        self._queue_stopping = True
        launching = [entry for entry in self._queue_launching.values()
                     if entry.profile not in self._active_commands
                     or self._active_commands[entry.profile].task is None]
        for task in list(self._queue_drainers.values()):
            task.cancel()
        for task in list(self._queue_drainers.values()):
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        # A task cancelled before its first step never reaches _drain_queue's finally.
        self._queue_drainers.clear()
        waiting = launching + [entry for items in self._command_queues.values() for entry in items]
        self._command_queues.clear()
        self._queue_history.clear()
        self._queue_resume_profiles.clear()
        for entry in waiting:
            release_entry(self, entry)
            release_callback(self, entry.source)
            await self._queue_notice(entry.chat_id, f"{entry.label}: очередь сброшена при перезапуске, нажмите снова.")
            await self._queue_markup(entry, restore=True)

    def _queue_capture_evidence(self, profile):
        try:
            target = self._profile(profile)
            paths = [getattr(target, name, "") for name in
                     ("run_history_file", "analytics_events_file", "log_file", "error_log_file")]
            return {path: (os.stat(path).st_ino, os.path.getsize(path)) if os.path.exists(path) else (0, 0)
                    for path in paths if path}
        except Exception:
            log.warning("Could not snapshot queue outcome evidence", exc_info=True)
            return {}

    def _queue_capture_finished(self, command, evidence):
        result = command.result or {}
        texts = [str(result.get("stdout") or ""), str(result.get("stderr") or "")]
        reason = ("hh_hard_stop" if result.get("hh_hard_stop") else
                  str(result.get("anti_bot_kind") or "") if result.get("anti_bot_kind") else
                  "HH: потеря авторизации" if result.get("auth_required") else "")
        for path, (inode, offset) in evidence.items():
            try:
                if not os.path.exists(path):
                    continue
                stat = os.stat(path)
                start = offset if stat.st_ino == inode and stat.st_size >= offset else 0
                with open(path, "rb") as stream:
                    stream.seek(start)
                    # Scan the complete new segment in bounded chunks/lines; never old runs.
                    for raw in stream:
                        text = raw.decode("utf-8", errors="replace")
                        if path.endswith(".jsonl"):
                            try:
                                payload = json.loads(text)
                            except ValueError:
                                continue
                            recovery = payload.get("hh_recovery") or {}
                            hh = (payload.get("source_stats") or {}).get("hh") or {}
                            if recovery.get("stopped") or hh.get("stop_reason"):
                                reason = recovery.get("reason") or hh.get("stop_reason") or "hh_hard_stop"
                            elif hh and payload.get("finished_at") and payload.get("status") == "finished":
                                # A finished run in which hh worked proves the session again.
                                command.hh_proven = True
                            if payload.get("error_kind") == "HHUnexpectedUI" or payload.get("error") == "hh_unexpected_ui":
                                reason = "HHUnexpectedUI"
                            if payload.get("event") == "hh_ui_isolation" and payload.get("stopped"):
                                reason = payload.get("reason") or "hh_hard_stop"
                            if payload.get("note") == "manual_ai:hh_ui":
                                reason = "HHUnexpectedUI при ручном отклике"
                        else:
                            detected = self._queue_text_stop(text)
                            if detected:
                                reason = detected
            except Exception:
                reason = "Не удалось прочитать результат HH-проверки"
                log.warning("Could not read queue outcome evidence", exc_info=True)
        for text in texts:
            reason = self._queue_text_stop(text) or reason
        command.pause_reason = reason

    @staticmethod
    def _queue_text_stop(text):
        # Explicit failure phrases only: e.g. a successful 'captcha solved' log is harmless.
        lowered = text.lower()
        for marker in ("hh_hard_stop", "hhunexpectedui", "hh_unexpected_ui", "не залогинен",
                       "auth_session_lost", "auth_cookie_missing", "captcha_or_antibot",
                       "authentication was not proven", "целостность сессии не подтверждена",
                       "нестандартный ui: вакансия оставлена", "captcha_human_timeout",
                       "browser flow остановлен", "не авторизован", "not logged in",
                       "not authenticated", "login required"):
            if marker in lowered:
                return marker
        if "account/login" in lowered and '"ok": false' in lowered:
            return "HH: потеря авторизации"
        if any(marker in lowered for marker in ("captcha", "anti-bot", "antibot", "капч")) and any(
                marker in lowered for marker in ("blocked", "hard_stop", "failed", "timeout", "заблок", "останов", "не реш", "потер")):
            return "HH: captcha/анти-бот"
        return ""

    def _queue_command_finished(self, profile, command):
        if command:
            entry = command.queue_entry
            if entry:
                release_entry(self, entry)
            if entry and entry.queued:
                from telegram_bot_ui import format_command_result, _pretty_command_label
                result = command.result
                if result is not None:
                    message = format_command_result(command.label, result)
                    first_line = message.splitlines()[0]
                    entry.outcome = first_line.partition(" ")[2].removeprefix(_pretty_command_label(command.label)).lstrip(": ")
                    stdout = str(result.get("stdout") or "").strip()
                    if not result.get("ok") and stdout:
                        entry.outcome += ": " + stdout.splitlines()[-1][:160]
                    if entry.label == "manual AI apply" and not result.get("ok") and not stdout:
                        import manual_apply_queue
                        data = entry.source.get("data", "") if entry.source else ""
                        token = data.split(":")[-1]
                        with contextlib.suppress(Exception):
                            item = manual_apply_queue.get_candidate(token, profile_name=profile)
                            if item:
                                entry.outcome = "пропуск: " + f"статус карточки: {item.get('status')}"
                                if item.get("message"):
                                    entry.outcome += "; " + str(item["message"])[:140]
                else:
                    entry.outcome = "остановлена пользователем" if command.cancel_requested else "ошибка выполнения (см. сообщение задачи)"
            if command.pause_reason:
                self._queue_stop_reasons[profile] = command.pause_reason
            elif getattr(command, "hh_proven", False) and self._queue_stop_reasons.pop(profile, None):
                # 19.10: a stop from a 14:22 login glitch paused the queue at 15:37 although a
                # search at 15:27 had worked with hh. A later healthy hh run clears it.
                log.info("Queue stop reason for %s cleared by a healthy hh run", profile)
        self._schedule_queue(profile)
