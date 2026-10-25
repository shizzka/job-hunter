"""Telegram callback handlers: forms."""
from __future__ import annotations


from telegram_app.callback_handlers import CallbackContext


async def handle_form(self, context: CallbackContext):
    callback_id = context.callback_id
    principal = context.principal
    raw_data = context.raw_data
    message = context.message
    chat_id = context.chat_id
    if (message.get("chat") or {}).get("type") != "private":
        await self._answer_callback_query(callback_id, "Откройте личный чат бота.", show_alert=True)
        return
    await self._answer_callback_query(callback_id)
    try:
        await self._form_callback(chat_id, principal, raw_data)
    except ValueError as exc:
        await self._send_text(chat_id, str(exc))
    return


async def handle_google_form_preview(self, context: CallbackContext):
    callback_id = context.callback_id
    principal = context.principal
    raw_data = context.raw_data
    chat_id = context.chat_id
    message_id = context.message_id
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


async def handle_google_form_submit(self, context: CallbackContext):
    callback_id = context.callback_id
    principal = context.principal
    raw_data = context.raw_data
    chat_id = context.chat_id
    message_id = context.message_id
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
