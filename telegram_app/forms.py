"""Telegram editing of saved Google Form drafts."""
from __future__ import annotations

import time

from google_forms import drafts
from state_store.google_forms import GoogleFormStateRepository


def button(label, action, profile, token, number=0):
    data = f"gf:{action}:{profile}:{token}:{number}"
    if len(data.encode()) > 64:
        raise ValueError("Слишком длинная ссылка на форму.")
    return {"text": label, "callback_data": data}


class TelegramFormEditor:
    def _form_home(self, profile):
        if profile not in self._profile_names():
            raise ValueError("Профиль не найден.")
        return self._profile(profile).home_dir

    async def _list_forms(self, chat_id, principal, profile):
        home = self._form_home(profile)
        items = GoogleFormStateRepository(home).load()["items"]
        edits = drafts.edits_store(home).load()
        rows, seen = [], set()
        for token, item in sorted(items.items(), key=lambda x: x[1].get("created_at", 0), reverse=True):
            if edits.get(token, {}).get("superseded_by"):
                continue
            key = (item.get("chat_id"), str(item.get("form_url", "")).split("?")[0])
            if key in seen:
                continue
            seen.add(key)
            if not item.get("questions") or item.get("status") in drafts.TERMINAL:
                continue
            title = (item.get("vacancy") or {}).get("title") or "Анкета"
            label = f"{str(item.get('chat_id', ''))[-4:]} · {title[:45]}"
            rows.append([button(label, "view", profile, token)])
            if len(rows) >= 15:
                break
        await self._send_text(
            chat_id, "Выберите черновик. Можно изменить любой ответ, затем проверить заполнение."
            if rows else "Черновиков с доступными полями пока нет.",
            reply_markup={"inline_keyboard": rows},
        )

    async def _show_form(self, chat_id, principal, profile, token, offset=0):
        home = self._form_home(profile)
        item = drafts.get_draft(home, token)
        answers = drafts.displayed_answers(home, item)
        questions = item.get("questions", [])
        offset = max(0, min(offset, max(0, len(questions) - 1)))
        page = questions[offset:offset + 5]
        lines = ["Черновик анкеты", "⚠ — нужно уточнить; ✍ — ваш ответ.\n"]
        rows = []
        for q in page:
            index = int(q["index"])
            answer = answers.get(index, {})
            marker = "✍" if answer.get("source") == "telegram_manual" else "⚠" if drafts.needs_review(q, answer) else "✓"
            source = "ваш ответ" if marker == "✍" else "подтверждённые факты" if answer.get("source") in {"contact_override", "confirmed_fact"} else "ИИ"
            lines.extend([f"{marker} {index + 1}. {str(q.get('question', ''))[:350]}",
                          f"{source}: {drafts.answer_text(answer)[:350]}", ""])
            if len(rows) == 0 or len(rows[-1]) == 3:
                rows.append([])
            rows[-1].append(button(f"✏️ {index + 1}", "field", profile, token, index))
        nav = []
        if offset:
            nav.append(button("← Назад", "view", profile, token, max(0, offset - 5)))
        if offset + 5 < len(questions):
            nav.append(button("Далее →", "view", profile, token, offset + 5))
        if nav:
            rows.append(nav)
        if any(drafts.needs_review(q, answers.get(int(q["index"]), {})) for q in questions):
            rows.append([button("⚠ Уточнить", "next", profile, token),
                         button("🔄 Проверить", "check", profile, token)])
        else:
            rows.append([button("🔄 Проверить", "check", profile, token)])
        rows.append([button("✅ Всё ок, отправить", "send", profile, token)])
        if item.get("form_url"):
            rows.append([{"text": "Открыть форму", "url": item["form_url"]}])
        await self._send_text(chat_id, "\n".join(lines), reply_markup={"inline_keyboard": rows})

    async def _ask_form_field(self, chat_id, principal, profile, token, index=None):
        home = self._form_home(profile)
        item = drafts.get_draft(home, token)
        answers = drafts.displayed_answers(home, item)
        questions = item.get("questions", [])
        if index is None:
            question = next((q for q in questions if drafts.needs_review(q, answers.get(int(q["index"]), {}))), None)
        else:
            question = next((q for q in questions if int(q["index"]) == index), None)
        if question is None:
            await self._show_form(chat_id, principal, profile, token)
            return
        index = int(question["index"])
        text = (f"Поле {index + 1}: {question.get('question', '')}\n\n"
                f"Сейчас: {drafts.answer_text(answers.get(index, {}))}\n\n")
        options = question.get("options") or []
        if options:
            text += "\n".join(f"{i + 1}. {o}" for i, o in enumerate(options)) + "\n\n"
            text += "Пришлите номер варианта"
            text += " (можно несколько через запятую)." if question.get("type") == "checkbox" else "."
        else:
            text += "Пришлите новый ответ текстом."
        text += "\nОтветьте именно на это сообщение. /cancel_form — выйти."
        if not question.get("required"):
            text += "\n/skip — оставить поле пустым."
        if len(text) > 3900:
            raise ValueError("Слишком длинное поле: заполните его по ссылке на форму.")
        sent = await self._send_text(chat_id, text, reply_markup={"force_reply": True, "selective": True})
        state = self._load_state()
        state.setdefault("form_pending", {})[f"{principal['user_id']}:{chat_id}"] = {
            "profile": profile, "token": token, "index": index,
            "prompt_id": int((sent or {}).get("message_id") or 0), "created_at": time.time(),
        }
        self._save_state(state)

    async def _accept_form_answer(self, chat_id, principal, message):
        key = f"{principal['user_id']}:{chat_id}"
        state = self._load_state()
        pending = state.get("form_pending", {}).get(key)
        if not pending:
            return False
        text = (message.get("text") or "").strip()
        if text == "/cancel_form":
            state["form_pending"].pop(key, None)
            self._save_state(state)
            await self._send_text(chat_id, "Редактирование приостановлено. Сохранённые ответы доступны через /forms.")
            return True
        if time.time() - pending.get("created_at", 0) > 86400:
            state["form_pending"].pop(key, None)
            self._save_state(state)
            return False
        # Never consume an SMS/captcha or a reply to another field.
        reply_id = int((message.get("reply_to_message") or {}).get("message_id") or 0)
        if not pending.get("prompt_id") or reply_id != pending["prompt_id"]:
            return False
        if principal.get("role") != "admin":
            return True
        if text.startswith("/") and text != "/skip":
            return False
        if not text:
            await self._send_text(chat_id, "Для этого поля нужен текстовый ответ.")
            return True
        profile, token = pending["profile"], pending["token"]
        if self._has_active_command(profile):
            await self._send_busy_status(chat_id, principal, profile_name=profile)
            return True
        try:
            home = self._form_home(profile)
            drafts.save_answer(home, token, pending["index"], text, principal["user_id"])
        except ValueError as exc:
            await self._send_text(chat_id, str(exc))
            return True
        state = self._load_state()
        state.get("form_pending", {}).pop(key, None)
        self._save_state(state)
        await self._send_text(chat_id, "Ответ сохранён.")
        item = drafts.get_draft(home, token)
        position = next(i for i, q in enumerate(item["questions"]) if int(q["index"]) == pending["index"])
        await self._show_form(chat_id, principal, profile, token, position // 5 * 5)
        return True

    async def _form_callback(self, chat_id, principal, data):
        parts = str(data).split(":")
        if len(parts) != 5:
            raise ValueError("Некорректная кнопка формы.")
        _, action, profile, token, number = parts
        if not number.isdigit():
            raise ValueError("Некорректный номер поля.")
        home = self._form_home(profile)
        drafts.get_draft(home, token)
        if self._has_active_command(profile):
            await self._send_busy_status(chat_id, principal, profile_name=profile)
            return
        if action == "view":
            await self._show_form(chat_id, principal, profile, token, int(number))
        elif action in {"field", "next"}:
            await self._ask_form_field(chat_id, principal, profile, token, int(number) if action == "field" else None)
        elif action in {"check", "send"}:
            import runtime_control
            if action == "send":
                item = drafts.get_draft(home, token)
                answers = drafts.displayed_answers(home, item)
                if any(drafts.needs_review(q, answers.get(int(q["index"]), {})) for q in item["questions"]):
                    await self._send_text(chat_id, "Сначала уточните поля с ⚠. Затем можно отправить анкету.")
                    await self._show_form(chat_id, principal, profile, token)
                    return
            state = self._load_state()
            state.get("form_pending", {}).pop(f"{principal['user_id']}:{chat_id}", None)
            self._save_state(state)
            await self._start_google_form_command(
                chat_id, principal, profile_name=profile,
                label="Проверка и отправка Google Form" if action == "send" else "Проверка черновика Google Form",
                argv=runtime_control.agent_command_argv(
                    profile, "--google-form-recheck-submit" if action == "send" else "--google-form-recheck", token),
            )
        else:
            raise ValueError("Неизвестное действие формы.")
