"""Callback routing for the Telegram control bot."""
from __future__ import annotations

import manual_apply_queue
import telegram_access
from telegram_app.callback_handlers import CallbackContext
from telegram_app.callback_handlers.chats import _lists_several_chats  # compatibility export
from telegram_app.callback_routing import route_for_callback
from telegram_app.command_queue import profile_command
from telegram_app.guards import callback_guard
from telegram_bot_ui import ROLE_ADMIN


class TelegramCallbackRouter:
    """Route stable callback payloads to TelegramBot operations."""

    @callback_guard
    @profile_command
    @telegram_access.scoped_request
    async def _handle_callback_query(self, callback: dict) -> None:
        sender = callback.get("from") or {}
        callback_id = str(callback.get("id") or "")
        user_id = int(sender.get("id") or 0)
        principal = telegram_access.resolve_user(user_id)
        if not principal:
            await self._answer_callback_query(callback_id, "🔒 Доступ закрыт.", show_alert=True)
            return

        raw_data = callback.get("data") or ""
        message = callback.get("message") or {}
        chat_id = int(((message.get("chat") or {}).get("id")) or 0)
        message_id = int(message.get("message_id") or 0)
        if chat_id <= 0:
            await self._answer_callback_query(callback_id, "Не удалось определить чат.", show_alert=True)
            return

        def can_access_profile(profile_name: str) -> bool:
            return (
                principal.get("role") == ROLE_ADMIN
                or self._selected_profile(principal) == profile_name
            )

        context = CallbackContext(
            callback_id=callback_id, user_id=user_id, principal=principal,
            raw_data=raw_data, message=message, chat_id=chat_id,
            message_id=message_id, can_access_profile=can_access_profile,
        )
        route = route_for_callback(raw_data)
        if route.phase == "navigation":
            return await route.handler(self, context)

        if str(raw_data).startswith(("manual_apply:", "stub:", "mad:")):
            profile_name, token = manual_apply_queue.parse_apply_launch_callback_data(raw_data)
            if (profile_name and token and can_access_profile(profile_name)
                    and (principal.get("role") == ROLE_ADMIN or (message.get("chat") or {}).get("type") == "private")
                    and await self._show_uncertain_manual_apply(chat_id, principal, profile_name, token)):
                await self._answer_callback_query(callback_id, "Нужна сверка на hh.ru")
                return

        if route.phase == "profile":
            return await route.handler(self, context)

        manual_callback = route.user_access == "manual"
        user_ai_callback = route.user_access == "ai"
        if principal.get("role") != ROLE_ADMIN:
            private = (message.get("chat") or {}).get("type") == "private"
            if not private or not (manual_callback or user_ai_callback):
                await self._answer_callback_query(callback_id, "🔒 Нужны права администратора.", show_alert=True)
                return
            if user_ai_callback:
                parts = raw_data.split(":")
                profile_index = 2 if raw_data.startswith("gf:") else 1
                if len(parts) <= profile_index or not can_access_profile(parts[profile_index]):
                    await self._answer_callback_query(callback_id, "🔒 Этот профиль вам недоступен.", show_alert=True)
                    return

        return await route.handler(self, context)
