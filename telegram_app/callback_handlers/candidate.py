"""Candidate confirmations bound to the user and the exact displayed draft."""
from telegram_app.callback_handlers import CallbackContext


async def handle_confirmation(self, context: CallbackContext):
    commands = {"save": "/candidate_save", "edit": "/candidate_edit", "skip": "/candidate_skip"}
    parts = str(context.raw_data).split(":")
    if (context.message.get("chat") or {}).get("type") != "private":
        await self._answer_callback_query(context.callback_id, "Откройте личный чат с ботом.", show_alert=True)
        return
    if (len(parts) != 4 or parts[1] != str(context.user_id) or parts[3] not in commands
            or not context.can_access_profile(self._selected_profile(context.principal))
            or not self._candidate_confirmation_current(context.principal, parts[2])):
        await self._answer_callback_query(context.callback_id, "Ответ изменился. Откройте подтверждение заново.", show_alert=True)
        return
    # Rechecked by the command handler after its guards, before any mutation.
    await self._dispatch(context.chat_id, context.principal, commands[parts[3]], f"cf:{parts[2]}")
    await self._answer_callback_query(context.callback_id)
