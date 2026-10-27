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


async def handle_application_page(self, context: CallbackContext):
    from telegram_app import history
    parts = str(context.raw_data).split(":")
    profile = self._selected_profile(context.principal)
    command = next((route for route, (selected, _) in history.APPLICATION_FILTERS.items()
                    if len(parts) == 4 and selected == parts[2]), None)
    if ((context.message.get("chat") or {}).get("type") != "private"
            or not context.can_access_profile(profile) or len(parts) != 4 or not parts[3].isdigit()
            or command is None or parts[1] != history.token(self._profile(profile).home_dir)):
        await self._answer_callback_query(context.callback_id, "Экран изменился или недоступен. Откройте отклики заново.")
        return
    await self._answer_callback_query(context.callback_id)
    await self._dispatch(context.chat_id, context.principal, command, parts[3])


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


async def handle_navigation(self, context: CallbackContext):
    """Bound, read-only navigation delegates actions through all command guards."""
    from telegram_bot_ui import ADMIN_BUTTON_MAP, USER_BUTTON_MAP, ADMIN_ONLY_COMMANDS, RESPONSE_FILTER_COMMANDS
    from telegram_app import history
    if (context.message.get("chat") or {}).get("type") != "private":
        await self._answer_callback_query(context.callback_id, "Откройте личный чат с ботом.", show_alert=True)
        return
    await self._answer_callback_query(context.callback_id)
    profile_name = self._selected_profile(context.principal)
    if not context.can_access_profile(profile_name):
        await self._send_text(context.chat_id, "🔒 Нет доступа к этому профилю.")
        return
    parts = str(context.raw_data).split(":")
    if len(parts) == 4 and parts[2] == "why_no_apply":
        if parts[1] != history.token(self._profile(profile_name).home_dir):
            await self._send_text(context.chat_id, "Экран изменился или недоступен. Откройте сводку поиска заново.")
            return
        await self._dispatch(context.chat_id, context.principal, "/why_no_apply", f"{parts[1]} {parts[3]}")
        return
    if len(parts) == 4 and parts[2] == "profile":
        profiles = self._profile_names()
        if context.principal.get("role") != ROLE_ADMIN or parts[1] != history.profile_picker_token(self._profile(profile_name).home_dir, profiles):
            await self._send_text(context.chat_id, "Экран изменился или недоступен. Откройте раздел заново.")
            return
        if not parts[3].isdigit() or int(parts[3]) >= len(profiles):
            await self._send_text(context.chat_id, "Список профилей изменился. Откройте его заново.")
            return
        await self._dispatch(context.chat_id, context.principal, "/profile", profiles[int(parts[3])])
        return
    role = context.principal.get("role")
    mapping = ADMIN_BUTTON_MAP if role == ROLE_ADMIN else USER_BUTTON_MAP
    allowed = set(mapping.values()) | set(RESPONSE_FILTER_COMMANDS.values()) | {"/directions"}
    command = "/" + parts[2] if len(parts) == 3 else ""
    if (len(parts) != 3 or parts[1] != history.token(self._profile(profile_name).home_dir)
            or command not in allowed or (command in ADMIN_ONLY_COMMANDS and role != ROLE_ADMIN)):
        await self._send_text(context.chat_id, "Экран изменился или недоступен. Откройте раздел заново.")
        return
    if command in {"/candidate_save", "/candidate_edit", "/candidate_skip"}:
        # Old navigation payloads contain no draft identity and cannot confirm it.
        await self._send_text(context.chat_id, "Ответ изменился. Откройте подтверждение заново.")
        return
    await self._dispatch(context.chat_id, context.principal, command, "")
