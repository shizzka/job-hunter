"""Telegram callback handlers: chats."""
from __future__ import annotations

from telegram_bot_ui import (
    _parse_chat_ai_callback_data,
    _parse_chat_ai_alternative_callback_data,
    _parse_chat_ai_manual_callback_data,
    _parse_chat_ai_manual_alternative_callback_data,
    _parse_chat_manual_send_callback_data,
    _parse_chat_send_callback_data,
)
from telegram_app.callback_handlers import CallbackContext


def _lists_several_chats(message: dict) -> bool:
    """The «Ответ ИИ в чат» list carries one button per chat: keep it after a tap (B15)."""
    rows = ((message or {}).get("reply_markup") or {}).get("inline_keyboard") or []
    chats = {str(button.get("callback_data") or "").split(":")[2] for row in rows for button in row
             if str(button.get("callback_data") or "").startswith(("chat_ai:", "chat_ai_any:"))
             and len(str(button.get("callback_data") or "").split(":")) > 2}
    return len(chats) > 1


async def handle_chat_owner(self, context: CallbackContext):
    callback_id = context.callback_id
    principal = context.principal
    raw_data = context.raw_data
    message = context.message
    chat_id = context.chat_id
    if (message.get("chat") or {}).get("type") != "private":
        await self._answer_callback_query(callback_id, "Откройте личный чат с ботом.", show_alert=True)
        return
    await self._answer_callback_query(callback_id)
    await self._chat_owner_callback(chat_id, principal, raw_data)
    return


async def handle_chat_owner_profile(self, context: CallbackContext):
    callback_id = context.callback_id
    principal = context.principal
    raw_data = context.raw_data
    chat_id = context.chat_id
    message_id = context.message_id
    can_access_profile = context.can_access_profile
    parts = str(raw_data).split(":")
    await self._answer_callback_query(callback_id)
    if (len(parts) != 4 or parts[1] not in self._profile_names() or not can_access_profile(parts[1])
            or not parts[2].isdigit() or not parts[3].isdigit()):
        await self._send_text(chat_id, "Профиль недоступен.")
        return
    if message_id > 0:
        await self._edit_reply_markup(chat_id, message_id)
    self._set_selected_profile(principal["user_id"], parts[1])
    await self._send_text(chat_id, f"📁 Переключил на профиль {parts[1]} — готовлю ответ в этот чат.")
    await self._start_chat_ai_reply(chat_id, principal, profile_name=parts[1], hh_chat_id=parts[2],
                                    hh_message_id=parts[3], allow_any=True)
    return


async def handle_employer_question(self, context: CallbackContext):
    callback_id = context.callback_id
    principal = context.principal
    raw_data = context.raw_data
    message = context.message
    chat_id = context.chat_id
    can_access_profile = context.can_access_profile
    parts = str(raw_data).split(":")
    if (len(parts) != 4 or parts[1] not in self._profile_names() or not can_access_profile(parts[1])
            or (message.get("chat") or {}).get("type") != "private"):
        await self._answer_callback_query(callback_id, "Вопрос недоступен.", show_alert=True)
        return
    await self._answer_callback_query(callback_id)
    await self._answer_employer_question(chat_id, principal, parts[1], parts[2], parts[3])
    return


async def handle_chat_ai_manual_reply(self, context: CallbackContext):
    callback_id = context.callback_id
    user_id = context.user_id
    principal = context.principal
    raw_data = context.raw_data
    message = context.message
    chat_id = context.chat_id
    message_id = context.message_id
    profile_name, hh_chat_id, hh_message_id = _parse_chat_ai_manual_callback_data(raw_data)
    if not profile_name or profile_name not in self._profile_names():
        await self._answer_callback_query(callback_id, "Не удалось определить профиль.", show_alert=True)
        return
    self._append_chat_ai_audit_event(
        "callback",
        action="preview",
        telegram_chat_id=chat_id,
        telegram_message_id=message_id,
        user_id=user_id,
        profile_name=profile_name,
        hh_chat_id=hh_chat_id,
        hh_message_id=hh_message_id,
        allow_any=True,
    )
    await self._answer_callback_query(callback_id, "Генерирую ответ через ИИ…")
    if message_id > 0 and not _lists_several_chats(message):
        await self._edit_reply_markup(chat_id, message_id)
    await self._start_chat_ai_reply(
        chat_id,
        principal,
        profile_name=profile_name,
        hh_chat_id=hh_chat_id,
        hh_message_id=hh_message_id,
        allow_any=True,
    )
    return


async def handle_chat_ai_manual_alternative(self, context: CallbackContext):
    callback_id = context.callback_id
    user_id = context.user_id
    principal = context.principal
    raw_data = context.raw_data
    message = context.message
    chat_id = context.chat_id
    message_id = context.message_id
    profile_name, hh_chat_id, hh_message_id = _parse_chat_ai_manual_alternative_callback_data(raw_data)
    if not profile_name or profile_name not in self._profile_names():
        await self._answer_callback_query(callback_id, "Не удалось определить профиль.", show_alert=True)
        return
    self._append_chat_ai_audit_event(
        "callback", action="alternative", telegram_chat_id=chat_id,
        telegram_message_id=message_id, user_id=user_id, profile_name=profile_name,
        hh_chat_id=hh_chat_id, hh_message_id=hh_message_id, allow_any=True,
    )
    await self._answer_callback_query(callback_id, "Генерирую другой вариант…")
    if message_id > 0 and not _lists_several_chats(message):
        await self._edit_reply_markup(chat_id, message_id)
    await self._start_chat_ai_reply(
        chat_id, principal, profile_name=profile_name, hh_chat_id=hh_chat_id,
        hh_message_id=hh_message_id, allow_any=True, alternative=True,
    )
    return


async def handle_chat_ai_manual_send(self, context: CallbackContext):
    callback_id = context.callback_id
    user_id = context.user_id
    principal = context.principal
    raw_data = context.raw_data
    chat_id = context.chat_id
    message_id = context.message_id
    profile_name, hh_chat_id, hh_message_id = _parse_chat_manual_send_callback_data(raw_data)
    if not profile_name or profile_name not in self._profile_names():
        await self._answer_callback_query(callback_id, "Не удалось определить профиль.", show_alert=True)
        return
    self._append_chat_ai_audit_event(
        "callback",
        action="send",
        telegram_chat_id=chat_id,
        telegram_message_id=message_id,
        user_id=user_id,
        profile_name=profile_name,
        hh_chat_id=hh_chat_id,
        hh_message_id=hh_message_id,
        allow_any=True,
    )
    await self._answer_callback_query(callback_id, "Отправляю ответ в HH…")
    if message_id > 0:
        await self._edit_reply_markup(chat_id, message_id)
    await self._start_chat_ai_reply(
        chat_id,
        principal,
        profile_name=profile_name,
        hh_chat_id=hh_chat_id,
        hh_message_id=hh_message_id,
        force_send=True,
        allow_any=True,
    )
    return


async def handle_chat_ai_reply(self, context: CallbackContext):
    callback_id = context.callback_id
    user_id = context.user_id
    principal = context.principal
    raw_data = context.raw_data
    message = context.message
    chat_id = context.chat_id
    message_id = context.message_id
    profile_name, hh_chat_id, hh_message_id = _parse_chat_ai_callback_data(raw_data)
    if not profile_name or profile_name not in self._profile_names():
        await self._answer_callback_query(callback_id, "Не удалось определить профиль.", show_alert=True)
        return
    self._append_chat_ai_audit_event(
        "callback",
        action="preview",
        telegram_chat_id=chat_id,
        telegram_message_id=message_id,
        user_id=user_id,
        profile_name=profile_name,
        hh_chat_id=hh_chat_id,
        hh_message_id=hh_message_id,
    )
    await self._answer_callback_query(callback_id, "Генерирую ответ через ИИ…")
    if message_id > 0 and not _lists_several_chats(message):
        await self._edit_reply_markup(chat_id, message_id)
    await self._start_chat_ai_reply(
        chat_id,
        principal,
        profile_name=profile_name,
        hh_chat_id=hh_chat_id,
        hh_message_id=hh_message_id,
    )
    return


async def handle_chat_ai_alternative(self, context: CallbackContext):
    callback_id = context.callback_id
    user_id = context.user_id
    principal = context.principal
    raw_data = context.raw_data
    message = context.message
    chat_id = context.chat_id
    message_id = context.message_id
    profile_name, hh_chat_id, hh_message_id = _parse_chat_ai_alternative_callback_data(raw_data)
    if not profile_name or profile_name not in self._profile_names():
        await self._answer_callback_query(callback_id, "Не удалось определить профиль.", show_alert=True)
        return
    self._append_chat_ai_audit_event(
        "callback", action="alternative", telegram_chat_id=chat_id,
        telegram_message_id=message_id, user_id=user_id, profile_name=profile_name,
        hh_chat_id=hh_chat_id, hh_message_id=hh_message_id,
    )
    await self._answer_callback_query(callback_id, "Генерирую другой вариант…")
    if message_id > 0 and not _lists_several_chats(message):
        await self._edit_reply_markup(chat_id, message_id)
    await self._start_chat_ai_reply(
        chat_id, principal, profile_name=profile_name, hh_chat_id=hh_chat_id,
        hh_message_id=hh_message_id, alternative=True,
    )
    return


async def handle_chat_ai_send(self, context: CallbackContext):
    callback_id = context.callback_id
    user_id = context.user_id
    principal = context.principal
    raw_data = context.raw_data
    chat_id = context.chat_id
    message_id = context.message_id
    profile_name, hh_chat_id, hh_message_id = _parse_chat_send_callback_data(raw_data)
    if not profile_name or profile_name not in self._profile_names():
        await self._answer_callback_query(callback_id, "Не удалось определить профиль.", show_alert=True)
        return
    self._append_chat_ai_audit_event(
        "callback",
        action="send",
        telegram_chat_id=chat_id,
        telegram_message_id=message_id,
        user_id=user_id,
        profile_name=profile_name,
        hh_chat_id=hh_chat_id,
        hh_message_id=hh_message_id,
    )
    await self._answer_callback_query(callback_id, "Отправляю ответ в HH…")
    if message_id > 0:
        await self._edit_reply_markup(chat_id, message_id)
    await self._start_chat_ai_reply(
        chat_id,
        principal,
        profile_name=profile_name,
        hh_chat_id=hh_chat_id,
        hh_message_id=hh_message_id,
        force_send=True,
    )
    return
