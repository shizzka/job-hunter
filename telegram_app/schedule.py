"""Telegram schedule handlers extracted without changing outcomes."""
from __future__ import annotations

import profile as profile_mod

from telegram_app.routing import COMMAND_ROUTES
from telegram_bot_ui import (
    MENU_REPEAT,
    build_schedule_text,
)
import config
import runtime_control


class TelegramSchedule:
    def _update_profile_schedule(self, profile_name: str, *, search_interval_min: int, invite_check_interval_min: int | None = None) -> str:
        invite_minutes = search_interval_min if invite_check_interval_min is None else invite_check_interval_min
        return profile_mod.update_profile_env(
            profile_name,
            {
                "SEARCH_INTERVAL_MIN": int(search_interval_min),
                "INVITE_CHECK_INTERVAL_MIN": int(invite_minutes),
            },
        )

    async def _route_schedule(self, chat_id, principal, command, arg, profile_name, reply_menu):
        self._set_selected_menu(principal["user_id"], MENU_REPEAT)
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

    async def _route_daemon_on(self, chat_id, principal, command, arg, profile_name, reply_menu):
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

    async def _route_repeat(self, chat_id, principal, command, arg, profile_name, reply_menu):
        label, minutes = COMMAND_ROUTES[command].args
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

    async def _route_repeat_off(self, chat_id, principal, command, arg, profile_name, reply_menu):
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

    async def _route_daemon_off(self, chat_id, principal, command, arg, profile_name, reply_menu):
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
