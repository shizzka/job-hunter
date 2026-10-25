"""Telegram callback handlers: navigation."""
from __future__ import annotations

from telegram_bot_ui import (
    ROLE_ADMIN,
)
from telegram_app.callback_handlers import CallbackContext


async def handle_seen_reset(self, context: CallbackContext):
    callback_id = context.callback_id
    principal = context.principal
    raw_data = context.raw_data
    chat_id = context.chat_id
    await self._answer_callback_query(callback_id)
    action, _, scope = str(raw_data)[3:].partition(":")
    if action == "no":
        await self._send_text(chat_id, "Сброс отменён.")
    elif principal.get("role") == ROLE_ADMIN:
        await self._dispatch(chat_id, principal, "/seen_reset", f"{action} {scope}".replace(":", " "))
    return


async def handle_history_scope(self, context: CallbackContext):
    callback_id = context.callback_id
    principal = context.principal
    raw_data = context.raw_data
    chat_id = context.chat_id
    from telegram_app import history
    parts = str(raw_data).split(":")
    home = self._profile(self._selected_profile(principal)).home_dir
    await self._answer_callback_query(callback_id)
    if (len(parts) != 4 or parts[1] != history.token(home)
            or "/" + parts[2] not in history.COMMANDS | {"/direction_stats"}
            or parts[3] not in {"active", "all", "unspecified"}):
        await self._send_text(chat_id, "Направление изменилось. Откройте экран заново.")
        return
    await self._dispatch(chat_id, principal, "/" + parts[2], parts[3])
    return


async def handle_candidate_upload(self, context: CallbackContext):
    callback_id = context.callback_id
    principal = context.principal
    raw_data = context.raw_data
    chat_id = context.chat_id
    await self._answer_callback_query(callback_id)
    await self._dispatch(chat_id, principal, "/candidate_upload_save" if raw_data == "kbu:save" else "/candidate_upload_cancel", "")
    return


async def handle_getting_started(self, context: CallbackContext):
    callback_id = context.callback_id
    principal = context.principal
    raw_data = context.raw_data
    chat_id = context.chat_id
    from telegram_app import onboarding
    command = onboarding.ACTIONS.get(str(raw_data)[3:])
    await self._answer_callback_query(callback_id)
    if command:
        await self._dispatch(chat_id, principal, command, "")
    return


async def handle_direction(self, context: CallbackContext):
    callback_id = context.callback_id
    principal = context.principal
    raw_data = context.raw_data
    message = context.message
    chat_id = context.chat_id
    if (message.get("chat") or {}).get("type") != "private":
        await self._answer_callback_query(callback_id, "Откройте личный чат с ботом.", show_alert=True)
        return
    await self._answer_callback_query(callback_id)
    return await self._direction_callback(chat_id, principal, raw_data)
