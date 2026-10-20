"""Explicit, application-specific ownership selection in Telegram."""
from __future__ import annotations

from html import escape

import chat_context
import chat_owner_review
import google_form_filler as gforms
from telegram_bot_ui import ROLE_ADMIN


class TelegramChatOwnership:
    async def _chat_owner_callback(self, chat_id: int, principal: dict, raw_data: str) -> None:
        def usable_rows(rows: list) -> list:
            result = [row for row in rows if len(row[0]["callback_data"].encode("utf-8")) <= 64]
            if not result:
                raise ValueError("callback too long")
            return result

        def allowed(name: str) -> bool:
            return name in self._profile_names() and (
                principal.get("role") == ROLE_ADMIN or self._selected_profile(principal) == name
            )

        try:
            parts = str(raw_data).split(":")
            if len(parts) not in (3, 4) or not allowed(parts[1]):
                raise ValueError("profile unavailable")
            action, source, token = parts[:3]
            home = self._profile(source).home_dir
            actor = int(principal["user_id"])
            if action == "cbo" and len(parts) == 3:
                review = chat_owner_review.read_review(home, token)
                if review["profile_name"] != source:
                    raise ValueError("wrong source")
                options = [{"stage": "profile", "profile_name": name}
                           for name in self._profile_names() if allowed(name)]
                selection = chat_owner_review.create_selection(home, token, options, actor)
                rows = [[{"text": item["profile_name"],
                          "callback_data": f"cbs:{source}:{selection}:{index}"}]
                        for index, item in enumerate(options)]
                await self._send_text(chat_id, "Выберите профиль владельца этого отклика.",
                                      reply_markup={"inline_keyboard": usable_rows(rows)})
                return
            if action != "cbs" or len(parts) != 4 or not parts[3].isdigit():
                raise ValueError("invalid selection")
            selection = chat_owner_review.read_selection(home, token, actor)
            review = chat_owner_review.read_review(home, selection["review_token"])
            if review["profile_name"] != source:
                raise ValueError("wrong source")
            index = int(parts[3])
            if index >= len(selection["options"]):
                raise ValueError("invalid index")
            option = selection["options"][index]
            target = option["profile_name"]
            if not allowed(target):
                raise ValueError("profile unavailable")
            target_home = self._profile(target).home_dir
            if option["stage"] == "profile":
                options = [{"stage": "resume", "profile_name": target, "resume_id": resume_id,
                            "direction": direction, "title": title}
                           for resume_id, (direction, title) in chat_context.profile_resumes(target_home).items()]
                if not options:
                    await self._send_text(chat_id, "В этом профиле нет доступных резюме. Выберите другой профиль.")
                    return
                next_token = chat_owner_review.create_selection(home, selection["review_token"], options, actor)
                rows = [[{"text": f"{item['title'] or item['resume_id']} · {item['direction'] or 'без направления'}",
                          "callback_data": f"cbs:{source}:{next_token}:{idx}"}]
                        for idx, item in enumerate(options)]
                await self._send_text(
                    chat_id, "Выберите резюме и направление, с которыми был отправлен ИМЕННО ЭТОТ отклик. "
                    "Выбор закрепит владельца этого чата.", reply_markup={"inline_keyboard": usable_rows(rows)})
                return
            if option["stage"] != "resume":
                raise ValueError("invalid stage")
            chat_owner_review.confirm_selection(home, token, index, actor, target_home=target_home)
            thread, message = str(review["chat_id"]), str(review["message_id"])
            buttons = [
                {"text": "Подготовить ответ ИИ", "callback_data": f"chat_ai_any:{target}:{thread}:{message}"},
                {"text": "Предпросмотр анкеты", "callback_data": gforms.google_form_preview_callback_data(target, thread, message)},
            ]
            rows = [[button] for button in buttons if len(button["callback_data"].encode("utf-8")) <= 64]
            await self._send_text(
                chat_id, f"Владелец отклика сохранён: профиль {escape(target)}, "
                f"резюме {escape(str(option.get('title') or option['resume_id']))}, "
                f"направление {escape(str(option['direction'] or 'без направления'))}. "
                "Теперь можно подготовить ответ или посмотреть анкету.",
                reply_markup={"inline_keyboard": rows})
        except (ValueError, KeyError, TypeError, IndexError):
            await self._send_text(chat_id, "Выбор недоступен или устарел. Откройте выбор владельца заново.")
