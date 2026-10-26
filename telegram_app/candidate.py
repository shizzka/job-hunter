"""Telegram candidate handlers extracted without changing outcomes."""
from __future__ import annotations

import logging
import os
import secrets

from telegram_bot_ui import (
    BUTTON_BACK,
    BUTTON_CANDIDATE_EDIT,
    BUTTON_CANDIDATE_SAVE,
    BUTTON_CANDIDATE_SKIP,
    MENU_CANDIDATE,
    ROLE_ADMIN,
    ROLE_USER,
    _is_menu_button_text,
    reply_menu_for_command,
)
import candidate_interview

log = logging.getLogger("telegram_bot")


class TelegramCandidate:
    def _candidate_profile_dir(self, profile_name: str) -> str:
        return self._profile(profile_name).home_dir

    def _candidate_state(self, user_id: int, profile_name: str) -> dict:
        state = self._search_settings_state(user_id).get("candidate_interview") or {}
        return dict(state) if state.get("profile_name") == profile_name else {}

    def _set_candidate_state(self, user_id: int, state: dict) -> None:
        self.__dict__.get("_candidate_confirmations", {}).pop(user_id, None)
        self._set_search_settings_state(user_id, candidate_interview=state)

    def _clear_candidate_state(self, user_id: int) -> None:
        self.__dict__.get("_candidate_confirmations", {}).pop(user_id, None)
        self._clear_search_settings_state(user_id, "candidate_interview")

    def _candidate_confirmation_current(self, principal, revision: str) -> bool:
        from telegram_app.history import token
        profile_name = self._selected_profile(principal)
        proof = self.__dict__.get("_candidate_confirmations", {}).get(principal["user_id"])
        state = self._candidate_state(principal["user_id"], profile_name)
        return bool(proof and state.get("mode") == "confirm"
                    and proof["revision"] == revision and proof["profile_name"] == profile_name
                    and proof["binding"] == token(self._profile(profile_name).home_dir)
                    and proof["state"] == state)

    def _candidate_confirm_markup(self, principal) -> dict:
        from telegram_app.history import token
        user_id = principal["user_id"]
        profile_name = self._selected_profile(principal)
        home = self._profile(profile_name).home_dir
        proofs = self.__dict__.setdefault("_candidate_confirmations", {})
        proof = proofs.get(user_id, {})
        if not self._candidate_confirmation_current(principal, proof.get("revision", "")):
            # A fresh draft gets a fresh capability even if its text is identical.
            proof = {"revision": secrets.token_hex(6), "profile_name": profile_name,
                     "binding": token(home), "state": self._candidate_state(user_id, profile_name)}
            proofs[user_id] = proof
        prefix = f"cf:{user_id}:{proof['revision']}"
        return {"inline_keyboard": [
            [{"text": BUTTON_CANDIDATE_SAVE, "callback_data": f"{prefix}:save"},
             {"text": BUTTON_CANDIDATE_EDIT, "callback_data": f"{prefix}:edit"}],
            [{"text": BUTTON_CANDIDATE_SKIP, "callback_data": f"{prefix}:skip"},
             {"text": BUTTON_BACK, "callback_data": f"nav:{token(home)}:back"}],
        ]}

    async def _send_candidate_question(self, chat_id: int, principal: dict, profile_name: str) -> None:
        reply_menu = reply_menu_for_command(principal.get("role", ROLE_USER), "/candidate_interview")
        state = self._candidate_state(principal["user_id"], profile_name)
        questions = state.get("questions") or []
        index = int(state.get("index") or 0)
        if index >= len(questions):
            self._clear_candidate_state(principal["user_id"])
            await self._send_text(
                chat_id,
                "✅ Интервью завершено. Подтверждённые ответы уже используются в вашем профиле.",
                reply_markup=self._menu_reply_markup(principal, menu=reply_menu),
            )
            return
        question = questions[index]
        hint = str(question.get("answer_hint") or "").strip()
        text = "🧠 Вопрос {} из {}\n\n{}".format(index + 1, len(questions), question.get("question", ""))
        if hint:
            text += f"\n\nПодсказка: {hint}"
        text += "\n\nОтветьте сообщением. Ничего не сохранится без подтверждения."
        await self._send_text(chat_id, text, reply_markup=self._menu_reply_markup(principal, menu=reply_menu))

    @staticmethod
    def _full_facts_document(profile_dir: str) -> str:
        """All of the profile's knowledge/*.md, unabridged, for «📋 Мои факты» as a file."""
        folder = os.path.join(profile_dir, "knowledge")
        parts = []
        try:
            names = sorted(name for name in os.listdir(folder) if name.endswith(".md"))
        except OSError:
            return ""
        for name in names:
            try:
                with open(os.path.join(folder, name), encoding="utf-8") as stream:
                    parts.append(f"<!-- {name} -->\n" + stream.read().strip())
            except OSError:
                continue
        return "\n\n".join(parts) + "\n" if parts else ""

    async def _accept_facts_upload(self, chat_id: int, principal: dict, message: dict) -> bool:
        """«📥 Загрузить базу фактов»: a whole .md/.txt becomes the profile's facts base after preview."""
        from telegram_app import facts_upload
        profile_name = self._selected_profile(principal)
        state = self._candidate_state(principal["user_id"], profile_name)
        if state.get("mode") != "upload" or not message.get("document"):
            return False
        document = message.get("document") or {}
        filename = str(document.get("file_name") or "facts.md").strip()
        markup = self._menu_reply_markup(principal, menu=MENU_CANDIDATE)
        if not filename.lower().endswith((".md", ".txt", ".markdown")):
            await self._send_text(chat_id, "❌ Нужен текстовый файл .md или .txt.", reply_markup=markup)
            return True
        if int(document.get("file_size") or 0) > facts_upload.MAX_BYTES:
            await self._send_text(chat_id, "❌ Файл больше 400 КБ — разбейте его или сократите.", reply_markup=markup)
            return True
        try:
            text = (await self._download_file(str(document.get("file_id") or ""))).decode("utf-8-sig")
        except UnicodeDecodeError:
            await self._send_text(chat_id, "❌ Файл не в UTF-8. Сохраните его как UTF-8 и пришлите ещё раз.", reply_markup=markup)
            return True
        except Exception as exc:
            log.warning("facts upload download failed: %s", type(exc).__name__)
            await self._send_text(chat_id, "❌ Не удалось скачать файл. Попробуйте ещё раз.", reply_markup=markup)
            return True
        if not text.strip():
            await self._send_text(chat_id, "❌ Файл пустой.", reply_markup=markup)
            return True
        self._facts_uploads = getattr(self, "_facts_uploads", {})
        self._facts_uploads[principal["user_id"]] = {"profile_name": profile_name, "text": text}
        state.update(mode="upload_confirm")
        self._set_candidate_state(principal["user_id"], state)
        info = facts_upload.summarize(text)
        buttons = {"inline_keyboard": [[{"text": "✅ Сохранить базу", "callback_data": "kbu:save"},
                                        {"text": "❌ Отмена", "callback_data": "kbu:cancel"}]]}
        await self._send_text(chat_id, str(facts_upload.preview_text(filename, info, profile_name)),
                              reply_markup=buttons)
        return True

    async def _finish_facts_upload(self, chat_id: int, principal: dict, save: bool) -> None:
        from telegram_app import facts_upload
        pending = getattr(self, "_facts_uploads", {}).pop(principal["user_id"], None)
        self._clear_candidate_state(principal["user_id"])
        markup = self._menu_reply_markup(principal, menu=MENU_CANDIDATE)
        if not save or not pending:
            await self._send_text(chat_id, "Загрузка отменена. База фактов не менялась." if not save
                                  else "Файл устарел — пришлите его ещё раз.", reply_markup=markup)
            return
        if pending["profile_name"] != self._selected_profile(principal):
            await self._send_text(chat_id, "Профиль изменился — загрузка отменена.", reply_markup=markup)
            return
        try:
            knowledge_dir = os.path.join(self._candidate_profile_dir(pending["profile_name"]), "knowledge")
            facts_upload.save(knowledge_dir, pending["text"])
        except (OSError, ValueError) as exc:
            await self._send_text(chat_id, f"❌ {str(str(exc))}", reply_markup=markup)
            return
        await self._send_text(chat_id, "✅ База фактов сохранена. Бот будет отбирать из неё факты под каждую вакансию. "
                              "Посмотреть: «📋 Мои факты».", reply_markup=markup)

    async def _accept_candidate_document(self, chat_id: int, principal: dict, message: dict) -> bool:
        """Accept a UTF-8 txt/md document as one admin candidate fact."""
        if await self._accept_facts_upload(chat_id, principal, message):
            return True
        if principal.get("role") != ROLE_ADMIN:
            return False
        profile_name = self._selected_profile(principal)
        state = self._candidate_state(principal["user_id"], profile_name)
        if state.get("mode") != "add":
            return False
        document = message.get("document") or {}
        file_id = str(document.get("file_id") or "").strip()
        filename = str(document.get("file_name") or "facts.txt").strip()
        if not file_id or not filename.lower().endswith((".txt", ".md", ".markdown")):
            return False
        try:
            raw = await self._download_file(file_id)
            answer = raw.decode("utf-8-sig", errors="replace").strip()
        except Exception as exc:
            log.warning("candidate facts document download failed: %s", exc)
            await self._send_text(chat_id, "❌ Не удалось прочитать файл. Пришли UTF-8 .txt или .md.", reply_markup=self._menu_reply_markup(principal, menu=MENU_CANDIDATE))
            return True
        answer = " ".join(answer.split()).strip()
        if not answer:
            await self._send_text(chat_id, "❌ Файл пустой.", reply_markup=self._menu_reply_markup(principal, menu=MENU_CANDIDATE))
            return True
        state.update(mode="confirm", pending_text=answer, pending_topic="база знаний")
        self._set_candidate_state(principal["user_id"], state)
        await self._send_text(chat_id, f"Файл «{filename}» прочитан ({len(answer)} символов).\n\nСохранить как один подтверждённый факт?", reply_markup=self._candidate_confirm_markup(principal))
        return True

    async def _accept_candidate_interview_input(self, chat_id: int, principal: dict, text: str) -> bool:
        profile_name = self._selected_profile(principal)
        state = self._candidate_state(principal["user_id"], profile_name)
        if state.get("mode") not in {"answer", "add"}:
            return False
        if _is_menu_button_text(text) or text.startswith("/"):
            return False
        if text.strip().casefold() in {"отмена", "cancel"}:
            self._clear_candidate_state(principal["user_id"])
            await self._send_text(chat_id, "Интервью остановлено. Уже подтверждённые факты сохранены.", reply_markup=self._menu_reply_markup(principal, menu=MENU_CANDIDATE))
            return True
        answer = " ".join(text.split()).strip()
        if not answer:
            return True
        # Для обычных пользователей сохраняем короткие подтверждаемые факты.
        # Администратору разрешён расширенный ввод, чтобы перенести большую
        # базу знаний одним фактом без искусственного дробления.
        max_length = None if principal.get("role") == ROLE_ADMIN else 1200
        if max_length is not None and len(answer) > max_length:
            await self._send_text(
                chat_id,
                f"Ответ слишком длинный: максимум {max_length} символов.",
                reply_markup=self._menu_reply_markup(principal, menu=MENU_CANDIDATE),
            )
            return True
        topic = "общий"
        if state.get("mode") == "answer":
            questions = state.get("questions") or []
            index = int(state.get("index") or 0)
            if index < len(questions):
                topic = str(questions[index].get("topic") or topic)
        state.update(mode="confirm", pending_text=answer, pending_topic=topic)
        self._set_candidate_state(principal["user_id"], state)
        await self._send_text(
            chat_id,
            f"Вот что будет сохранено как подтверждённый факт:\n\n[{topic}] {answer}\n\nПроверьте формулировку.",
            reply_markup=self._candidate_confirm_markup(principal),
        )
        return True

    async def _candidate_advance(self, chat_id: int, principal: dict, profile_name: str) -> None:
        state = self._candidate_state(principal["user_id"], profile_name)
        if state.get("kind") == "add":
            self._clear_candidate_state(principal["user_id"])
            await self._send_text(chat_id, "✅ Факт сохранён для этого профиля.", reply_markup=self._menu_reply_markup(principal, menu=MENU_CANDIDATE))
            return
        state.update(mode="answer", index=int(state.get("index") or 0) + 1)
        state.pop("pending_text", None)
        state.pop("pending_topic", None)
        self._set_candidate_state(principal["user_id"], state)
        await self._send_candidate_question(chat_id, principal, profile_name)

    async def _route_candidate_upload(self, chat_id, principal, command, arg, profile_name, reply_menu):
        role = principal.get("role", ROLE_USER)
        if command == "/candidate_upload":
            owner_menu = reply_menu_for_command(role, "/candidate_upload") or MENU_CANDIDATE
            self._set_selected_menu(principal["user_id"], owner_menu)
            self._set_candidate_state(principal["user_id"], {"profile_name": profile_name, "kind": "upload", "mode": "upload"})
            await self._send_text(
                chat_id,
                "📥 Пришлите файл с фактами о себе (.md или .txt, до 400 КБ) — как документ.\n\n"
                "Это может быть ваша «база фактов»: опыт, навыки, проекты, образование. "
                "Строки, которые бот НИКОГДА не должен про вас писать, пометьте [DO_NOT_CLAIM].\n"
                "Перед сохранением покажу, что прочитал.",
                reply_markup=self._menu_reply_markup(principal, menu=owner_menu),
            )
        else:
            await self._finish_facts_upload(chat_id, principal, save=command == "/candidate_upload_save")
        return

    async def _route_candidate(self, chat_id, principal, command, arg, profile_name, reply_menu):
        role = principal.get("role", ROLE_USER)
        if reply_menu is None or command == "/candidate_profile":
            self._set_selected_menu(principal["user_id"], MENU_CANDIDATE)
        profile_dir = self._candidate_profile_dir(profile_name)
        state = self._candidate_state(principal["user_id"], profile_name)
        if arg.startswith("cf:") and not self._candidate_confirmation_current(principal, arg[3:]):
            await self._send_text(chat_id, "Ответ изменился. Откройте подтверждение заново.")
            return
        if command == "/candidate_profile":
            await self._send_menu(chat_id, principal, profile_name=profile_name, menu=MENU_CANDIDATE)
            return
        if command == "/candidate_facts":
            message = candidate_interview.render_facts(profile_dir=profile_dir)
            # Используем тот же профильный knowledge/, что и генератор писем.
            try:
                from prompt_blocks import build_knowledge_base_block
                knowledge = build_knowledge_base_block(limit_chars=7000, profile_dir=profile_dir)
            except Exception as exc:
                log.debug("candidate knowledge display failed: %s", exc)
                knowledge = ""
            if knowledge:
                message += "\n\n" + knowledge
            await self._send_text(chat_id, message, reply_markup=self._menu_reply_markup(principal, menu=reply_menu))
            full = self._full_facts_document(profile_dir)
            if full:
                await self._send_document(chat_id, filename=f"facts_{profile_name}.md", content=full.encode("utf-8"),
                                          caption="Полная база фактов профиля (на экране выше — сокращённо).")
            return
        if command == "/candidate_add_fact":
            self._set_candidate_state(principal["user_id"], {"profile_name": profile_name, "kind": "add", "mode": "add"})
            await self._send_text(chat_id, "Напишите один факт о себе. Он будет использован только после вашего подтверждения.\n\nНапример: «Работал с электрощитами до 1000 В; допуск III группы действовал до 2025 года».", reply_markup=self._menu_reply_markup(principal, menu=reply_menu))
            return
        if command == "/candidate_interview":
            if state.get("questions") and state.get("mode") in {"answer", "confirm"}:
                if state.get("mode") == "confirm":
                    await self._send_text(chat_id, "Остался неподтверждённый ответ. Сохраните, измените или пропустите его.", reply_markup=self._candidate_confirm_markup(principal))
                else:
                    await self._send_candidate_question(chat_id, principal, profile_name)
                return
            try:
                with open(self._profile(profile_name).resume_file, encoding="utf-8") as handle:
                    resume_text = handle.read().strip()
            except OSError:
                resume_text = ""
            client = self._client_record(principal["user_id"]) or {}
            plan = await candidate_interview.build_plan(resume_text, target_role=str(client.get("target_role") or ""), current_facts=candidate_interview.facts(profile_dir))
            self._set_candidate_state(principal["user_id"], {"profile_name": profile_name, "kind": "interview", "mode": "answer", "questions": plan.get("questions") or [], "index": 0})
            hypothesis = str(plan.get("profile_hypothesis") or "").strip()
            if hypothesis:
                await self._send_text(chat_id, f"Собрал вопросы по резюме. Текущее направление: {hypothesis}.", reply_markup=self._menu_reply_markup(principal, menu=reply_menu))
            await self._send_candidate_question(chat_id, principal, profile_name)
            return
        if command == "/candidate_edit" and state.get("mode") == "confirm":
            state["mode"] = "add" if state.get("kind") == "add" else "answer"
            state.pop("pending_text", None)
            state.pop("pending_topic", None)
            self._set_candidate_state(principal["user_id"], state)
            if state.get("kind") == "add":
                await self._send_text(chat_id, "Отправьте исправленную формулировку факта.", reply_markup=self._menu_reply_markup(principal, menu=reply_menu))
            else:
                await self._send_candidate_question(chat_id, principal, profile_name)
            return
        if command == "/candidate_save" and state.get("mode") == "confirm":
            try:
                candidate_interview.add_fact(
                    state.get("pending_text", ""),
                    topic=state.get("pending_topic", "общий"),
                    profile_dir=profile_dir,
                    max_chars=None if role == ROLE_ADMIN else 1200,
                )
            except ValueError as exc:
                await self._send_text(chat_id, f"❌ {exc}", reply_markup=self._menu_reply_markup(principal, menu=reply_menu))
                return
            await self._candidate_advance(chat_id, principal, profile_name)
            return
        if command == "/candidate_skip" and state:
            if state.get("kind") == "add":
                self._clear_candidate_state(principal["user_id"])
                await self._send_text(chat_id, "Факт не сохранён.", reply_markup=self._menu_reply_markup(principal, menu=reply_menu))
            else:
                await self._candidate_advance(chat_id, principal, profile_name)
            return
        await self._send_text(chat_id, "Сначала начните интервью или добавление факта.", reply_markup=self._menu_reply_markup(principal, menu=reply_menu))
        return
