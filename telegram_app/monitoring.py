"""Telegram monitoring handlers extracted without changing outcomes."""
from __future__ import annotations

from datetime import datetime
import os
import time

from private_logging import chat_log_path
from state_store.private_journal import read_json_records
from telegram_app.api import telegram_error_summary
from telegram_app.command_queue import notification_delivery, notification_delivery_allowed
from telegram_bot_ui import (
    ROLE_USER,
    _tail_text_file,
    build_diagnostics_text,
    build_log_text,
    build_status_text,
    reply_menu_for_command,
)
import analytics
import config
import profile_access
import runtime_control
import seen
import telegram_access


class TelegramMonitoring:
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
        if profile_access.current_scope()[1] or principal.get("role") != telegram_access.ROLE_ADMIN:
            raise PermissionError("Сырой лог недоступен. /log — записи направления QA.")
        path, content = self._log_tail(profile_name, kind=kind, lines=lines)
        await self._send_text(
            chat_id,
            build_log_text(profile_name, kind=kind, path=path, content=content, lines=lines),
            reply_markup=self._menu_reply_markup(principal),
        )

    async def _send_run_summary(self, chat_id, principal, *, profile_name, run_id=None):
        from telegram_app import history
        await history.show(self, chat_id, principal, profile_name, "/log", run_id or "")

    def _latest_run(self, profile_name: str) -> dict | None:
        return runtime_control.latest_run_entry(self._profile(profile_name).run_history_file)

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
            checks.append({"name": "Last run", "ok": None, "detail": "нет истории поисков"})
        else:
            max_age = max(6 * 60 * 60, int(search_interval_min) * 3 * 60)
            checks.append({
                "name": "Данные поиска",
                "ok": latest_age < max_age,
                "detail": self._format_age(latest_age),
            })

            checks.append({"name": "Последний поиск", "ok": bool(latest.get("ok")),
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
            try:
                principal = telegram_access.resolve_user(user_id)
            except PermissionError:
                # Deny this recipient without blocking alerts to other users.
                continue
            if not principal or principal.get("role") == telegram_access.ROLE_TESTER:
                # Diagnostics contain profile-wide log evidence; they cannot
                # establish ownership of each line by the tester's direction.
                continue
            with notification_delivery(self.profile_name):
                if not notification_delivery_allowed(user_id):
                    continue
                reply_markup = self._menu_reply_markup(principal)
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

    def _recent_runs(self, profile_name: str, limit: int = 5, *, scope="active") -> list[dict]:
        profile = self._profile(profile_name)
        run_history_file = profile.run_history_file
        if limit <= 0 or not os.path.exists(run_history_file):
            return []
        try:
            items = read_json_records(run_history_file)
        except OSError:
            return []
        items = analytics.scoped_runs(items, events_file=profile.analytics_events_file, scope=scope)
        return list(reversed(analytics.reconcile_run_records(items, events_file=profile.analytics_events_file)[-limit:]))

    def _stats_snapshot(self, profile_name: str, *, scope="active") -> dict:
        import application_history
        profile = self._profile(profile_name)
        summary = analytics.summarize(events_file=profile.analytics_events_file, all_time=True, scope=scope)
        summary["applications"] = application_history.summarize(profile.home_dir, scope=scope,
            events_file=profile.analytics_events_file, seen_file=profile.seen_file)
        return {
            "profile_name": profile_name,
            "seen_stats": seen.stats_from_file(profile.seen_file, scope=scope),
            "analytics_summary": summary,
            "recent_runs": self._recent_runs(profile_name, limit=3, scope=scope),
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

    async def _route_diagnostics(self, chat_id, principal, command, arg, profile_name, reply_menu):
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

    async def _route_status(self, chat_id, principal, command, arg, profile_name, reply_menu):
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

    async def _route_history(self, chat_id, principal, command, arg, profile_name, reply_menu):
        role = principal.get("role", ROLE_USER)
        from telegram_app import history
        if reply_menu is None:
            canonical = {"/hunter_log": "/log", "/hunter_chat_log": "/chat_log"}.get(command, command)
            owner_menu = reply_menu_for_command(role, canonical)
            if owner_menu is not None:
                self._set_selected_menu(principal["user_id"], owner_menu)
        await history.show(self, chat_id, principal, profile_name, command, arg)
        return

    async def _route_why_no_apply(self, chat_id, principal, command, arg, profile_name, reply_menu):
        def diagnose():  # reads the whole run journal: off the event loop (24.10: 6–8 s froze the bot)
            runs = self._recent_runs(profile_name, limit=1)
            run = runs[0] if runs else None
            return run, (analytics.zero_apply_diagnosis(run) if run else "")
        from telegram_app.guards import read_thread
        run, diagnosis = await read_thread(diagnose)
        await self._send_text(chat_id, diagnosis or (
            "В последнем поиске есть подтверждённые отклики." if run and (run.get("funnel") or run).get("applied")
            else "Нет завершённого поиска с точным нулём откликов для диагностики."),
            reply_markup=self._menu_reply_markup(principal))
        return

    async def _route_raw_log(self, chat_id, principal, command, arg, profile_name, reply_menu):
        role = principal.get("role", ROLE_USER)
        if role == telegram_access.ROLE_TESTER:
            await self._send_text(chat_id, "🔒 Сырой лог недоступен. /log — сводка доступного направления.")
            return
        if role != telegram_access.ROLE_ADMIN:
            await self._send_run_summary(chat_id, principal, profile_name=profile_name)
            return
        await self._send_log_tail(chat_id, principal, profile_name=profile_name, kind="log")
        return
