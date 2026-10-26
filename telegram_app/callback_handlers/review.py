"""Telegram callback handlers: review."""
from __future__ import annotations

import time

import manual_apply_queue
from telegram_bot_ui import (
    _parse_manual_apply_callback_data,
    _parse_manual_block_company_callback_data,
    _parse_manual_feedback_callback_data,
    _parse_manual_why_callback_data,
    _parse_manual_snooze_callback_data,
)
from telegram_app.callback_handlers import CallbackContext
from telegram_app.guards import read_thread


def _current_card_state(item):
    labels = {"pending": "ждёт решения", "applying": "отклик выполняется",
              "applied": "отклик отправлен", "already_applied": "уже откликались",
              "uncertain": "нужна сверка на hh.ru", "dismissed": "решение сохранено",
              "company_blocked": "компания заблокирована"}
    if not item:
        return "не найдена"
    if float(item.get("snoozed_until") or 0) > time.time():
        return "отложена"
    return labels.get(item.get("status"), "не ждёт решения")


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
    if len(parts) >= 2 and parts[1] in {"h", "v", "c", "m", "w", "l", "L", "U", "g", "b", "s", "x", "X", "e"}:
        return await handle_review_card(self, context)
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
    if not item or item.get("status") != "uncertain":
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
    resolved = await read_thread(manual_apply_queue.resolve_orphaned_apply,
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
    item = manual_apply_queue.get_candidate(token, profile_name=profile_name)
    if (not item or item.get("status") != "pending" or not item.get("allow_ai_apply", True)
            or float(item.get("snoozed_until") or 0) > time.time()
            or manual_apply_queue.company_blacklist.is_blocked((item.get("vacancy") or {}).get("company", ""), profile_name)):
        status = (item or {}).get("status")
        state = {"applied": "отклик отправлен", "already_applied": "уже откликались",
                 "applying": "отклик выполняется", "uncertain": "нужна сверка на hh.ru"}.get(status, "не ждёт решения")
        await self._answer_callback_query(callback_id, "Карточка уже обработана: " + state + ".", show_alert=True)
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
    current = manual_apply_queue.get_candidate(token, profile_name=profile_name)
    if not current or current.get("status") != "pending":
        state = _current_card_state(current)
        await self._answer_callback_query(callback_id, f"Карточка уже не ждёт решения: {state}.", show_alert=True)
        return
    # Old keyboards use the same pending-only decision semantics as the new card.
    item = manual_apply_queue.record_feedback(
        token, feedback, user_id=user_id, profile_name=profile_name, consume_decision=True)
    if not item:
        current = manual_apply_queue.get_candidate(token, profile_name=profile_name)
        state = _current_card_state(current)
        await self._answer_callback_query(callback_id, f"Карточка уже не ждёт решения: {state}.", show_alert=True)
        return
    await self._answer_callback_query(callback_id,
        "Запомнил: вакансия хорошая. Отклик не отправлен." if feedback == "good" else "Запомнил: вакансия мимо.")
    hint = self._review_first_good_hint(user_id) if feedback == "good" else False
    await self._show_review_queue(chat_id, context.principal, profile_name=profile_name,
                                 message_id=message_id, advance_from=token, hint=hint)
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
    answer = "Показываю причину" if item.get("status") in (None, "pending") else "Карточка: " + _current_card_state(item) + ". Показываю причину."
    await self._answer_callback_query(callback_id, answer)
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
    profile_name, token = _parse_manual_block_company_callback_data(context.raw_data)
    if not profile_name or not token:
        await self._answer_callback_query(context.callback_id, "Не удалось определить вакансию.", show_alert=True)
        return
    return await handle_review_card(self, CallbackContext(
        context.callback_id, context.user_id, context.principal, f"rq:x:{profile_name}:{token}",
        context.message, context.chat_id, context.message_id, context.can_access_profile))


async def handle_review_card(self, context: CallbackContext):
    """F27 card actions share the existing scoped rq route and persistent guards."""
    parts = str(context.raw_data).split(":")
    if (len(parts) not in {4, 5} or parts[2] not in self._profile_names()
            or not context.can_access_profile(parts[2])
            or context.principal.get("role") != "admin"
            and (context.message.get("chat") or {}).get("type") != "private"):
        await self._answer_callback_query(context.callback_id, "Карточка недоступна.", show_alert=True)
        return
    _, action, profile_name, token = parts[:4]
    if len(parts) == 5 and action not in {"L", "U"}:
        await self._answer_callback_query(context.callback_id, "Карточка недоступна.", show_alert=True)
        return
    if action in {"l", "L", "U"}:
        await self._answer_callback_query(context.callback_id)
        await self._show_review_list(context.chat_id, context.principal, profile_name=profile_name,
                                     page=int(token) if token.isdigit() else 0,
                                     message_id=context.message_id, unspecified=action == "U",
                                     sort="new" if parts[4:] == ["n"] else "score")
        return
    item = manual_apply_queue.get_candidate(token, profile_name=profile_name)
    from direction_views import DirectionView
    view = DirectionView(self._profile(profile_name).home_dir)
    if not item:
        await self._answer_callback_query(context.callback_id, "Карточка недоступна. Обновите список.", show_alert=True)
        return
    status = item.get("status")
    if action == "e":
        cover = item.get("sent_cover")
        if status != "applied" or cover is None:
            await self._answer_callback_query(context.callback_id, "Подтверждённого отклика с письмом нет.", show_alert=True)
            return
        await self._answer_callback_query(context.callback_id)
        await self._send_text(context.chat_id, str(cover))
        return
    if view.item_direction(item) not in (None, view.name):
        await self._answer_callback_query(context.callback_id, "Выбрано другое направление. Обновите список.", show_alert=True)
        return
    if action == "w" and status == "uncertain":
        await self._answer_callback_query(context.callback_id)
        text = manual_apply_queue.build_manual_why_text(item)
        markup = {"inline_keyboard": [[{"text": "◀ Назад",
                   "callback_data": f"rq:c:{profile_name}:{token}"}]]}
        if context.message_id:
            await self._edit_text(context.chat_id, context.message_id, text, reply_markup=markup)
        else:
            await self._send_text(context.chat_id, text, reply_markup=markup)
        return
    unspecified = (view.item_direction(item) is None and token not in {
        i["token"] for i in await read_thread(self._review_visible_items, profile_name)})
    show = dict(profile_name=profile_name, message_id=context.message_id, unspecified=unspecified)
    confirmation_key = f"{context.user_id}:{profile_name}:{token}:{context.message_id}"
    confirmations = self.__dict__.setdefault("_review_block_confirmations", set())
    if action in {"c", "m", "h", "v"}:
        confirmations.discard(confirmation_key)
    if action in {"h", "v", "c"} and status in {"pending", "uncertain"}:
        items = await read_thread(self._review_visible_items, profile_name, unspecified=unspecified)
        tokens = [i['token'] for i in items]
        if token not in tokens:
            await self._answer_callback_query(context.callback_id, "Карточка уже не ждёт решения.")
            return
        target = token if action == 'c' else tokens[(tokens.index(token) + (1 if action == 'v' else -1)) % len(tokens)]
        await self._answer_callback_query(context.callback_id)
        await self._show_review_queue(context.chat_id, context.principal, token=target, **show)
        return
    if (status != "pending" or float(item.get("snoozed_until") or 0) > time.time()
            or manual_apply_queue.company_blacklist.is_blocked((item.get("vacancy") or {}).get("company", ""), profile_name)):
        labels = {"applied": "отклик отправлен", "already_applied": "уже откликались",
                  "applying": "отклик выполняется", "uncertain": "результат не подтверждён, проверьте на hh.ru",
                  "dismissed": "решение сохранено", "company_blocked": "компания заблокирована"}
        await self._answer_callback_query(context.callback_id,
            "Карточка уже обработана: " + labels.get(status, "не ждёт решения") + ".")
        return
    if action == "X":
        if confirmation_key not in confirmations:
            await self._answer_callback_query(context.callback_id, "Подтверждение устарело. Откройте «Заблокировать компанию» снова.", show_alert=True)
            return False
        confirmations.discard(confirmation_key)
    if action in {"g", "b", "s", "X"}:
        if action in {"g", "b"}:
            result = manual_apply_queue.record_feedback(token, "good" if action == "g" else "bad",
                user_id=context.user_id, profile_name=profile_name, consume_decision=True)
            answer = "Запомнил: вакансия хорошая. Отклик не отправлен." if action == "g" else "Запомнил: вакансия мимо."
        elif action == "s":
            result = manual_apply_queue.snooze_candidate(token, profile_name=profile_name)
            answer = "Отложил на сутки."
        else:
            company = str((item.get("vacancy") or {}).get("company") or "").strip()
            if not company:
                await self._answer_callback_query(context.callback_id, "Компания не указана.", show_alert=True)
                return
            import company_blacklist
            company_blacklist.set_blocked(company, profile_name=profile_name)
            result = manual_apply_queue.mark_candidate(token, "company_blocked", company, profile_name=profile_name)
            answer = "Компания заблокирована."
        if not result:
            await self._answer_callback_query(context.callback_id, "Карточка уже не ждёт решения.")
            return
        hint = self._review_first_good_hint(context.user_id) if action == "g" else False
        await self._answer_callback_query(context.callback_id, answer)
        await self._show_review_queue(context.chat_id, context.principal, advance_from=token, hint=hint, **show)
        return
    await self._answer_callback_query(context.callback_id)
    def button(text, value):
        return {"text": text, "callback_data": f"rq:{value}:{profile_name}:{token}"}
    back = [button("◀ Назад", "c")]
    if action == "c":
        await self._show_review_queue(context.chat_id, context.principal, token=token, **show)
        return
    if action == "x":
        confirmations.add(confirmation_key)
        company = (item.get("vacancy") or {}).get("company") or "компанию"
        text = f"Заблокировать {company}? Новые отклики в эту компанию будут запрещены."
        markup = {"inline_keyboard": [[button("🚫 Заблокировать", "X")], [button("◀ Назад", "m")]]}
    elif action == "w":
        text = manual_apply_queue.build_manual_why_text(item)
        markup = {"inline_keyboard": [back]}
    else:
        items = await read_thread(self._review_visible_items, profile_name, unspecified=unspecified)
        key = (int(context.principal["user_id"]), profile_name, view.callback_scope, unspecified)
        session = self.__dict__.get("_review_sessions", {}).get(key, {})
        tokens = session.get("tokens") or [i["token"] for i in items]
        position = tokens.index(token) + 1 if token in tokens else 1
        text = manual_apply_queue.build_review_card_text(item, position=position, total=len(tokens))
        rows = []
        url = (item.get("vacancy") or {}).get("url")
        if url:
            rows.append([{"text": "🔗 Открыть вакансию", "url": url}])
        rows.extend([[button("⏰ Отложить на сутки", "s")],
                     [{"text": "☰ Вся очередь", "callback_data": f"rq:l:{profile_name}:0"}]])
        if (item.get("vacancy") or {}).get("company"):
            rows.append([button("🚫 Заблокировать компанию", "x")])
        rows.append(back)
        markup = {"inline_keyboard": rows}
    if context.message_id:
        await self._edit_text(context.chat_id, context.message_id, text, reply_markup=markup)
    else:
        await self._send_text(context.chat_id, text, reply_markup=markup)
