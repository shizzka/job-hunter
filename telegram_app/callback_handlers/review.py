"""Telegram callback handlers: review."""
from __future__ import annotations

import manual_apply_queue
from telegram_bot_ui import (
    _parse_manual_apply_callback_data,
    _parse_manual_block_company_callback_data,
    _parse_manual_feedback_callback_data,
    _parse_manual_why_callback_data,
    _parse_manual_snooze_callback_data,
)
from telegram_app.callback_handlers import CallbackContext


async def handle_stub_cover(self, context: CallbackContext):
    callback_id = context.callback_id
    principal = context.principal
    raw_data = context.raw_data
    chat_id = context.chat_id
    message_id = context.message_id
    can_access_profile = context.can_access_profile
    parts = str(raw_data).split(":")
    await self._answer_callback_query(callback_id)
    if len(parts) != 3 or parts[1] not in self._profile_names() or not can_access_profile(parts[1]):
        await self._send_text(chat_id, "Карточка недоступна.")
        return
    if not manual_apply_queue.allow_stub_cover(parts[2], profile_name=parts[1]):
        await self._send_text(chat_id, "Эта карточка уже обработана.")
        return
    if message_id > 0:
        await self._edit_reply_markup(chat_id, message_id)
    await self._start_manual_ai_apply(chat_id, principal, profile_name=parts[1], token=parts[2])
    return


async def handle_review_queue(self, context: CallbackContext):
    callback_id = context.callback_id
    principal = context.principal
    raw_data = context.raw_data
    message = context.message
    chat_id = context.chat_id
    can_access_profile = context.can_access_profile
    parts = str(raw_data).split(":")
    await self._answer_callback_query(callback_id)
    if len(parts) not in (4, 5) or parts[2] not in self._profile_names() or not can_access_profile(parts[2]):
        await self._send_text(chat_id, "Список недоступен.")
        return
    if parts[1] == "o":
        await self._open_review_card(chat_id, parts[2], parts[3])
    elif parts[1] in ("p", "u") and parts[3].isdigit():
        await self._show_review_queue(chat_id, principal, profile_name=parts[2], page=int(parts[3]),
                                      message_id=int(message.get("message_id") or 0),
                                      sort="new" if parts[4:] == ["n"] else "score",
                                      unspecified=parts[1] == "u")
    elif parts[1] in ("d", "D") and parts[3].isdigit():
        await self._review_dismiss_below(chat_id, principal, parts[2], int(parts[3]),
                                         confirmed=parts[1] == "D",
                                         expected_scope=parts[4] if len(parts) == 5 else "")
    return


async def handle_manual_direction(self, context: CallbackContext):
    callback_id = context.callback_id
    principal = context.principal
    raw_data = context.raw_data
    chat_id = context.chat_id
    can_access_profile = context.can_access_profile
    parts = str(raw_data).split(":")
    await self._answer_callback_query(callback_id)
    if parts[1:] == ["no"]:
        await self._send_text(chat_id, "Отклик отменён.")
        return
    if len(parts) != 3 or parts[1] not in self._profile_names() or not can_access_profile(parts[1]):
        await self._send_text(chat_id, "Карточка недоступна.")
        return
    await self._start_manual_ai_apply(chat_id, principal, profile_name=parts[1], token=parts[2],
                                      direction_override=True)
    return


async def handle_manual_recovery(self, context: CallbackContext):
    callback_id = context.callback_id
    user_id = context.user_id
    raw_data = context.raw_data
    message = context.message
    chat_id = context.chat_id
    message_id = context.message_id
    can_access_profile = context.can_access_profile
    parts = raw_data.split(":")
    if (len(parts) != 4 or parts[1] not in self._profile_names() or not can_access_profile(parts[1])
            or parts[3] not in {"handled", "retry", "confirm"}
            or (message.get("chat") or {}).get("type") != "private"):
        await self._answer_callback_query(callback_id, "Вакансия недоступна.", show_alert=True)
        return
    _, profile_name, token, action = parts
    item = manual_apply_queue.get_candidate(token, profile_name=profile_name)
    if not item or item.get("status") != "uncertain" or not item.get("orphan_recovered"):
        await self._answer_callback_query(callback_id, "Карточка уже разобрана.")
        return
    await self._answer_callback_query(callback_id)
    if action == "retry":
        await self._send_text(chat_id, "Проверьте отклики на hh.ru. Подтверждаете, что отклик не отправлен? "
                              "После подтверждения появится новая карточка для ручного запуска.",
                              reply_markup={"inline_keyboard": [[
                                  {"text": "Подтверждаю: отклик не отправлен",
                                   "callback_data": f"mar:{profile_name}:{token}:confirm"}]]})
        return
    resolved = manual_apply_queue.resolve_orphaned_apply(
        token, profile_name=profile_name, user_id=user_id, confirmed_not_sent=action == "confirm")
    if resolved:
        if message_id > 0:
            await self._edit_reply_markup(chat_id, message_id)
        if action == "confirm":
            await self._open_review_card(chat_id, profile_name, resolved["token"])
        else:
            await self._send_text(chat_id, "Карточка отмечена как разобранная.")
    else:
        await self._send_text(chat_id, "Повтор пока недоступен: есть другая или живая попытка либо "
                              "подтверждённый отклик. Проверьте на hh.ru.")
    return


async def handle_manual_apply(self, context: CallbackContext):
    callback_id = context.callback_id
    principal = context.principal
    raw_data = context.raw_data
    chat_id = context.chat_id
    can_access_profile = context.can_access_profile
    profile_name, token = _parse_manual_apply_callback_data(raw_data)
    if not profile_name or profile_name not in self._profile_names() or not token or not can_access_profile(profile_name):
        await self._answer_callback_query(callback_id, "Не удалось определить вакансию.", show_alert=True)
        return
    await self._answer_callback_query(callback_id, "Отправляю отклик через ИИ…")
    await self._start_manual_ai_apply(
        chat_id,
        principal,
        profile_name=profile_name,
        token=token,
    )
    return


async def handle_manual_feedback(self, context: CallbackContext):
    callback_id = context.callback_id
    user_id = context.user_id
    raw_data = context.raw_data
    chat_id = context.chat_id
    message_id = context.message_id
    can_access_profile = context.can_access_profile
    profile_name, token, feedback = _parse_manual_feedback_callback_data(raw_data)
    if not profile_name or profile_name not in self._profile_names() or not token or not can_access_profile(profile_name):
        await self._answer_callback_query(callback_id, "Не удалось определить вакансию.", show_alert=True)
        return
    item = manual_apply_queue.record_feedback(
        token,
        feedback,
        user_id=user_id,
        profile_name=profile_name,
    )
    if not item:
        await self._answer_callback_query(callback_id, "Не удалось записать оценку.", show_alert=True)
        return
    label = manual_apply_queue.feedback_label(feedback)
    await self._answer_callback_query(callback_id, f"Записал: {label}")
    if message_id > 0:
        if feedback == "good":
            reply_markup = manual_apply_queue.build_manual_apply_markup(
                item.get("vacancy") or {},
                profile_name,
                token,
                include_feedback=False,
                allow_ai_apply=item.get("allow_ai_apply", True),
            )
            await self._edit_reply_markup(chat_id, message_id, reply_markup=reply_markup)
        else:
            await self._edit_reply_markup(chat_id, message_id)
    return


async def handle_manual_why(self, context: CallbackContext):
    callback_id = context.callback_id
    principal = context.principal
    raw_data = context.raw_data
    chat_id = context.chat_id
    can_access_profile = context.can_access_profile
    profile_name, token = _parse_manual_why_callback_data(raw_data)
    if not profile_name or profile_name not in self._profile_names() or not token or not can_access_profile(profile_name):
        await self._answer_callback_query(callback_id, "Не удалось определить вакансию.", show_alert=True)
        return
    item = manual_apply_queue.get_candidate(token, profile_name=profile_name)
    if not item:
        await self._answer_callback_query(callback_id, "Решение уже не найдено.", show_alert=True)
        return
    await self._answer_callback_query(callback_id, "Показываю причину")
    await self._send_text(
        chat_id,
        manual_apply_queue.build_manual_why_text(item),
        reply_markup=self._menu_reply_markup(principal),
    )
    return


async def handle_manual_snooze(self, context: CallbackContext):
    callback_id = context.callback_id
    raw_data = context.raw_data
    chat_id = context.chat_id
    message_id = context.message_id
    can_access_profile = context.can_access_profile
    profile_name, token = _parse_manual_snooze_callback_data(raw_data)
    if not profile_name or profile_name not in self._profile_names() or not token or not can_access_profile(profile_name):
        await self._answer_callback_query(callback_id, "Не удалось определить вакансию.", show_alert=True)
        return
    item = manual_apply_queue.snooze_candidate(token, profile_name=profile_name)
    if not item:
        await self._answer_callback_query(callback_id, "Вакансия уже не ждёт решения.", show_alert=True)
        return
    await self._answer_callback_query(callback_id, "Отложил на сутки.")
    if message_id > 0:
        await self._edit_reply_markup(chat_id, message_id)
    return


async def handle_manual_block_company(self, context: CallbackContext):
    callback_id = context.callback_id
    principal = context.principal
    raw_data = context.raw_data
    chat_id = context.chat_id
    message_id = context.message_id
    can_access_profile = context.can_access_profile
    profile_name, token = _parse_manual_block_company_callback_data(raw_data)
    if not profile_name or profile_name not in self._profile_names() or not token or not can_access_profile(profile_name):
        await self._answer_callback_query(callback_id, "Не удалось определить вакансию.", show_alert=True)
        return
    item = manual_apply_queue.get_candidate(token, profile_name=profile_name)
    vacancy = (item or {}).get("vacancy") or {}
    company = str(vacancy.get("company") or "").strip()
    if not item or not company:
        await self._answer_callback_query(callback_id, "Не удалось определить компанию.", show_alert=True)
        return
    import company_blacklist
    company_blacklist.set_blocked(company, profile_name=profile_name)
    manual_apply_queue.mark_candidate(token, "company_blocked", company, profile_name=profile_name)
    await self._answer_callback_query(callback_id, "Компания в чёрном списке.")
    if message_id > 0:
        await self._edit_reply_markup(chat_id, message_id)
    await self._send_text(chat_id, f"🚫 Новые отклики в {company} запрещены для профиля {profile_name}.", reply_markup=self._menu_reply_markup(principal))
    return
