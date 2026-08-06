"""Callback routing for the Telegram control bot."""

from __future__ import annotations

import logging

import manual_apply_queue
import runtime_control
import telegram_access
import telegram_clients
from telegram_bot_ui import (
    CALLBACK_CHAT_AI_MANUAL_REPLY,
    CALLBACK_CHAT_AI_MANUAL_SEND,
    CALLBACK_CHAT_AI_REPLY,
    CALLBACK_CHAT_AI_SEND,
    CALLBACK_CLIENT_APPROVE,
    CALLBACK_CLIENT_HH_AUTH,
    CALLBACK_CLIENT_REJECT,
    CALLBACK_HH_REAUTH,
    CALLBACK_MANUAL_APPLY,
    CALLBACK_MANUAL_BLOCK_COMPANY,
    CALLBACK_MANUAL_FEEDBACK,
    CALLBACK_MANUAL_WHY,
    ROLE_ADMIN,
    ROLE_USER,
    _parse_callback_data,
    _parse_chat_ai_callback_data,
    _parse_chat_ai_manual_callback_data,
    _parse_chat_manual_send_callback_data,
    _parse_chat_send_callback_data,
    _parse_hh_reauth_callback_data,
    _parse_manual_apply_callback_data,
    _parse_manual_block_company_callback_data,
    _parse_manual_feedback_callback_data,
    _parse_manual_why_callback_data,
    format_command_result,
)


log = logging.getLogger("telegram_bot")


class TelegramCallbackRouter:
    """Route stable callback payloads to TelegramBot operations."""

    async def _handle_callback_query(self, callback: dict) -> None:
        sender = callback.get("from") or {}
        callback_id = str(callback.get("id") or "")
        user_id = int(sender.get("id") or 0)
        principal = telegram_access.resolve_user(user_id)
        if not principal:
            await self._answer_callback_query(callback_id, "🔒 Доступ закрыт.", show_alert=True)
            return
        if principal.get("role") != ROLE_ADMIN:
            await self._answer_callback_query(callback_id, "🔒 Нужны права администратора.", show_alert=True)
            return

        raw_data = callback.get("data") or ""
        message = callback.get("message") or {}
        chat_id = int(((message.get("chat") or {}).get("id")) or 0)
        message_id = int(message.get("message_id") or 0)
        if chat_id <= 0:
            await self._answer_callback_query(callback_id, "Не удалось определить чат.", show_alert=True)
            return

        if raw_data.startswith(f"{CALLBACK_HH_REAUTH}:"):
            profile_name = _parse_hh_reauth_callback_data(raw_data)
            if not profile_name or profile_name not in self._profile_names():
                await self._answer_callback_query(callback_id, "Не удалось определить профиль.", show_alert=True)
                return
            await self._answer_callback_query(callback_id, "Запускаю вход HH…")
            if message_id > 0:
                await self._edit_reply_markup(chat_id, message_id)
            await self._start_profile_hh_auth_capture(chat_id, principal, profile_name=profile_name)
            return

        if raw_data.startswith("gform_preview:"):
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

        if raw_data.startswith("gform_submit:"):
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

        if raw_data.startswith(f"{CALLBACK_CHAT_AI_MANUAL_REPLY}:"):
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
            if message_id > 0:
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

        if raw_data.startswith(f"{CALLBACK_CHAT_AI_MANUAL_SEND}:"):
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

        if raw_data.startswith(f"{CALLBACK_CHAT_AI_REPLY}:"):
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
            if message_id > 0:
                await self._edit_reply_markup(chat_id, message_id)
            await self._start_chat_ai_reply(
                chat_id,
                principal,
                profile_name=profile_name,
                hh_chat_id=hh_chat_id,
                hh_message_id=hh_message_id,
            )
            return

        if raw_data.startswith(f"{CALLBACK_CHAT_AI_SEND}:"):
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

        if raw_data.startswith(f"{CALLBACK_MANUAL_APPLY}:"):
            profile_name, token = _parse_manual_apply_callback_data(raw_data)
            if not profile_name or profile_name not in self._profile_names() or not token:
                await self._answer_callback_query(callback_id, "Не удалось определить вакансию.", show_alert=True)
                return
            await self._answer_callback_query(callback_id, "Отправляю отклик через ИИ…")
            if message_id > 0:
                await self._edit_reply_markup(chat_id, message_id)
            await self._start_manual_ai_apply(
                chat_id,
                principal,
                profile_name=profile_name,
                token=token,
            )
            return

        if raw_data.startswith(f"{CALLBACK_MANUAL_FEEDBACK}:"):
            profile_name, token, feedback = _parse_manual_feedback_callback_data(raw_data)
            if not profile_name or profile_name not in self._profile_names() or not token:
                await self._answer_callback_query(callback_id, "Не удалось определить вакансию.", show_alert=True)
                return
            item = manual_apply_queue.record_feedback(token, feedback, user_id=user_id)
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
                    )
                    await self._edit_reply_markup(chat_id, message_id, reply_markup=reply_markup)
                else:
                    await self._edit_reply_markup(chat_id, message_id)
            return

        if raw_data.startswith(f"{CALLBACK_MANUAL_WHY}:"):
            profile_name, token = _parse_manual_why_callback_data(raw_data)
            if not profile_name or profile_name not in self._profile_names() or not token:
                await self._answer_callback_query(callback_id, "Не удалось определить вакансию.", show_alert=True)
                return
            item = manual_apply_queue.get_candidate(token)
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

        if raw_data.startswith(f"{CALLBACK_MANUAL_BLOCK_COMPANY}:"):
            profile_name, token = _parse_manual_block_company_callback_data(raw_data)
            if not profile_name or profile_name not in self._profile_names() or not token:
                await self._answer_callback_query(callback_id, "Не удалось определить вакансию.", show_alert=True)
                return
            item = manual_apply_queue.get_candidate(token)
            vacancy = (item or {}).get("vacancy") or {}
            company = str(vacancy.get("company") or "").strip()
            if not item or not company:
                await self._answer_callback_query(callback_id, "Не удалось определить компанию.", show_alert=True)
                return
            await self._answer_callback_query(callback_id, "Ставлю компанию в retry blocklist…")
            result = await runtime_control.run_command_capture(
                runtime_control.agent_command_argv(profile_name, "--hh-retry-block-company", company),
                timeout=60,
            )
            if result.get("ok"):
                manual_apply_queue.mark_candidate(token, "company_blocked", f"retry company blocked: {company}")
                if message_id > 0:
                    await self._edit_reply_markup(chat_id, message_id)
                await self._send_text(
                    chat_id,
                    f"🛑 Retry-отклики в компанию {company} отключены для профиля {profile_name}.",
                    reply_markup=self._menu_reply_markup(principal),
                )
            else:
                message = format_command_result("retry block company", result, role=principal.get("role", ROLE_USER))
                await self._send_text(chat_id, message, reply_markup=self._menu_reply_markup(principal))
            return

        # captcha-retry: ручной перезапуск поиска из TG (после пропущенного окна captcha).
        if raw_data.startswith("captcha_retry:"):
            request_id = raw_data.split(":", 1)[1].strip()
            await self._answer_callback_query(callback_id, "🔁 Запускаю поиск заново…")
            try:
                import hh_guard
                hh_guard.clear_cooldown()
            except Exception as exc:
                log.warning("clear_cooldown failed: %s", exc)
            if message_id > 0:
                await self._edit_reply_markup(chat_id, message_id)
            await self._send_text(
                chat_id,
                f"🔁 Поиск перезапущен (request_id <code>{request_id}</code>). Жди новую captcha — отвечу ниже.",
                reply_markup=self._menu_reply_markup(principal),
            )
            await self._start_cli_command(
                chat_id,
                principal,
                self._selected_profile(principal),
                "--search",
                "search",
                3600,
            )
            return

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
