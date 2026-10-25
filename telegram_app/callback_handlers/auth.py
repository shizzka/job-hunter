"""Telegram callback handlers: auth."""
from __future__ import annotations

import logging

import telegram_clients
from telegram_bot_ui import (
    CALLBACK_CLIENT_APPROVE,
    CALLBACK_CLIENT_HH_AUTH,
    CALLBACK_CLIENT_REJECT,
    ROLE_ADMIN,
    _parse_callback_data,
    _parse_hh_reauth_callback_data,
)
from telegram_app.callback_handlers import CallbackContext

log = logging.getLogger("telegram_bot")


async def handle_hh_reauth(self, context: CallbackContext):
    callback_id = context.callback_id
    principal = context.principal
    raw_data = context.raw_data
    message = context.message
    chat_id = context.chat_id
    message_id = context.message_id
    can_access_profile = context.can_access_profile
    profile_name = _parse_hh_reauth_callback_data(raw_data)
    if (
        not profile_name
        or profile_name not in self._profile_names()
        or not can_access_profile(profile_name)
        or (
            principal.get("role") != ROLE_ADMIN
            and (message.get("chat") or {}).get("type") != "private"
        )
    ):
        await self._answer_callback_query(callback_id, "Эта captcha относится к другому профилю.", show_alert=True)
        return
    await self._answer_callback_query(callback_id, "Запускаю вход HH…")
    if message_id > 0:
        await self._edit_reply_markup(chat_id, message_id)
    await self._start_profile_hh_auth_capture(chat_id, principal, profile_name=profile_name)
    return


async def handle_captcha_retry(self, context: CallbackContext):
    callback_id = context.callback_id
    principal = context.principal
    raw_data = context.raw_data
    message = context.message
    chat_id = context.chat_id
    message_id = context.message_id
    can_access_profile = context.can_access_profile
    payload = raw_data.split(":")
    if len(payload) == 3:
        _, profile_name, request_id = payload
    elif len(payload) == 2:  # callbacks sent before profile binding
        _, request_id = payload
        profile_name = self._selected_profile(principal)
    else:
        await self._answer_callback_query(callback_id, "Не удалось определить captcha.", show_alert=True)
        return
    if (
        not request_id
        or profile_name not in self._profile_names()
        or not can_access_profile(profile_name)
        or (
            principal.get("role") != ROLE_ADMIN
            and (message.get("chat") or {}).get("type") != "private"
        )
    ):
        await self._answer_callback_query(callback_id, "Эта captcha относится к другому профилю.", show_alert=True)
        return
    await self._answer_callback_query(callback_id, "🔁 Запускаю поиск заново…")
    try:
        import hh_guard
        hh_guard.clear_cooldown_for_profile(profile_name)
    except Exception as exc:
        log.warning("clear_cooldown failed for %s: %s", profile_name, exc)
    if message_id > 0:
        await self._edit_reply_markup(chat_id, message_id)
    await self._send_text(
        chat_id,
        "🔁 Поиск перезапущен. Если HH снова попросит captcha, пришлю её сюда.",
        reply_markup=self._menu_reply_markup(principal),
    )
    await self._start_cli_command(
        chat_id,
        principal,
        profile_name,
        "--search",
        "search",
        3600,
    )
    return


async def handle_client_action(self, context: CallbackContext):
    callback_id = context.callback_id
    principal = context.principal
    raw_data = context.raw_data
    chat_id = context.chat_id
    message_id = context.message_id
    action, target_user_id = _parse_callback_data(raw_data)
    if not action or target_user_id <= 0:
        await self._answer_callback_query(callback_id, "Неизвестное действие.", show_alert=True)
        return
    if action == CALLBACK_CLIENT_APPROVE:
        await self._answer_callback_query(callback_id, "Одобряю клиента…")
        client = await self._approve_client(chat_id, principal, target_user_id)
        if client and message_id > 0:
            await self._edit_reply_markup(chat_id, message_id)
        return
    if action == CALLBACK_CLIENT_REJECT:
        await self._answer_callback_query(callback_id, "Отклоняю заявку…")
        client = await self._reject_client(chat_id, principal, target_user_id)
        if client and message_id > 0:
            await self._edit_reply_markup(chat_id, message_id)
        return
    if action == CALLBACK_CLIENT_HH_AUTH:
        await self._answer_callback_query(callback_id, "Запускаю вход HH…")
        if message_id > 0:
            await self._edit_reply_markup(chat_id, message_id)
        client = self._client_record(target_user_id)
        if not client:
            await self._send_text(chat_id, f"❌ Клиент не найден: {target_user_id}", reply_markup=self._menu_reply_markup(principal))
            return
        if client.get("status") != telegram_clients.STATUS_APPROVED:
            await self._send_text(
                chat_id,
                "❌ Вход HH можно запускать только для одобренного клиента.",
                reply_markup=self._menu_reply_markup(principal),
            )
            return
        await self._start_hh_auth_capture(chat_id, principal, client)
        return
