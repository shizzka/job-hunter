"""Telegram pending-decision cards and manual apply workflow."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import time
import uuid

from direction_views import DirectionView
from telegram_app import pending_inbox
from telegram_app.command_queue import profile_command
from telegram_app.guards import read_thread
from telegram_bot_ui import (
    ROLE_ADMIN,
    ROLE_USER,
    build_progress_text,
    format_command_result,
    reply_menu_for_command,
)
import manual_apply_queue
import runtime_control

log = logging.getLogger("telegram_bot")


class TelegramReview:
    REVIEW_DISMISS_THRESHOLDS = (40, 50, 60)

    REVIEW_PAGE_SIZE = 6

    async def _show_uncertain_manual_apply(self, chat_id: int, principal: dict,
                                         profile_name: str, token: str) -> bool:
        # Also used by the duplicate callback guard: this path can only show a card.
        if (profile_name not in self._profile_names() or
                principal.get("role") != ROLE_ADMIN and self._selected_profile(principal) != profile_name):
            return False
        import profile_access
        try:
            with profile_access.principal_scope(principal):
                item = manual_apply_queue.get_candidate(token, profile_name=profile_name)
                if not item or item.get("status") != "uncertain":
                    return False
                await self._open_review_card(chat_id, profile_name, token)
                return True
        except PermissionError:
            return False

    _review_item_text = staticmethod(manual_apply_queue.build_review_item_text)
    _review_score_label = staticmethod(manual_apply_queue.review_score_label)

    def _review_visible_items(self, profile_name: str, *, unspecified=False) -> list[dict]:
        view = DirectionView(self._profile(profile_name).home_dir)
        items = pending_inbox.review_vacancies(
            view, manual_apply_queue.list_candidates(profile_name, limit=2**31 - 1), unspecified=unspecified)
        if not unspecified:
            questions, linked = pending_inbox.native_questions(view, items, profile_name)
            items = [item for item in items if item['token'] not in linked]
            items += questions + pending_inbox.pending_workflows(view)
        return sorted(items, key=pending_inbox.oldest_key)

    def _review_accessible_items(self, profile_name: str) -> list[dict]:
        """All permitted pending decisions plus the unspecified group, once per token."""
        items = self._review_visible_items(profile_name)
        items += self._review_visible_items(profile_name, unspecified=True)
        return list({item["token"]: item for item in items}.values())

    async def _show_review_queue(self, chat_id: int, principal: dict, *, profile_name: str,
                                 page=0, message_id=0, sort="score", unspecified=False,
                                 token=None, advance_from=None, hint=False, reset=False) -> None:
        """One pending decision at a time; snapshot positions survive decisions in this session."""
        reply_menu = reply_menu_for_command(principal.get("role", ROLE_USER), "/review")
        self._set_selected_menu(principal["user_id"], reply_menu)
        items = await read_thread(self._review_visible_items, profile_name, unspecified=unspecified)
        view = DirectionView(self._profile(profile_name).home_dir)
        key = (int(principal["user_id"]), profile_name, self._review_scope(view), unspecified)
        sessions = self.__dict__.setdefault("_review_sessions", {})
        if reset or key not in sessions:
            sessions[key] = {"tokens": [i["token"] for i in items]}
        session = sessions[key]
        for item in items:
            if item["token"] not in session["tokens"]:
                session["tokens"].append(item["token"])
        tokens = session["tokens"]
        available = {item["token"]: item for item in items}
        if not items:
            unknown = (await read_thread(self._review_visible_items, profile_name, unspecified=True)
                       if not unspecified else [])
            if unknown:
                text = (f"📋 В очереди нет карточек с известным направлением. Ещё ждут решения или сверки: {len(unknown)}"
                        " · направление не указано.")
                markup = {"inline_keyboard": [[{
                    "text": f"Направление не указано: {len(unknown)}", "callback_data": f"rq:U:{profile_name}:0:s"}]]}
            else:
                text = "✅ Всё разобрано. Сейчас от вас ничего не требуется."
                markup = {"inline_keyboard": [[{"text": "🔎 Найти вакансии", "callback_data": "fs:open"}]]}
        else:
            if token not in available:
                start = tokens.index(advance_from) + 1 if advance_from in tokens else 0
                token = next((t for t in tokens[start:] + tokens[:start] if t in available), items[0]["token"])
            item = available[token]
            position = dict(position=tokens.index(token) + 1, total=len(tokens))
            if item.get("_inbox_kind"):
                markup = pending_inbox.card_markup(item, profile_name)
                text = pending_inbox.card_text(item, **position)
            else:
                markup = manual_apply_queue.build_review_card_markup(item, profile_name)
                text = manual_apply_queue.build_review_card_text(item, **position)
        if hint:
            text += "\n\n👍 — хорошая, но откликаться не буду; 👎 — мимо."
        if message_id:
            await self._edit_text(chat_id, message_id, text, reply_markup=markup)
        else:
            await self._send_text(chat_id, text, reply_markup=markup)

    @staticmethod
    def _review_scope(view):
        import profile_access
        return profile_access.current_scope()[1] or "all"

    async def _ask_inbox_reply(self, chat_id, principal, profile, item):
        key = f"{principal['user_id']}:{chat_id}"
        pending = {"profile": profile, "token": item['token'], "chat_id": item['chat_id'],
                   "revision": item['draft']['revision'], "created_at": time.time(),
                   "prompt_id": 0, "request_id": uuid.uuid4().hex}
        self._update_state(lambda state: state.setdefault('inbox_reply_pending', {}).update({key: pending.copy()}))
        try:
            sent = await self._send_text(chat_id, "Пришлите новый текст ответа работодателю. "
                "Ответьте на это сообщение. Отмена: /cancel_reply\n\nСейчас:\n" + item['draft']['answer'],
                reply_markup={"force_reply": True, "selective": True})
            def bind(state):
                entry = state.get('inbox_reply_pending', {}).get(key)
                if entry == pending:
                    entry['prompt_id'] = int((sent or {}).get('message_id') or 0)
            self._update_state(bind)
        except BaseException:
            self._clear_inbox_reply(key, pending)
            raise

    def _clear_inbox_reply(self, key, expected):
        removed = False
        def clear(state):
            nonlocal removed
            if state.get('inbox_reply_pending', {}).get(key) == expected:
                state['inbox_reply_pending'].pop(key)
                removed = True
        self._update_state(clear)
        return removed

    async def _accept_inbox_reply(self, chat_id, principal, message):
        key = f"{principal['user_id']}:{chat_id}"
        pending = self._load_state().get('inbox_reply_pending', {}).get(key)
        if not pending:
            return False
        text = (message.get('text') or '').strip()
        if text == '/cancel_reply':
            self._clear_inbox_reply(key, pending)
            await self._send_text(chat_id, 'Редактирование отменено. Черновик сохранён.')
            return True
        if time.time() - pending.get('created_at', 0) > 86400:
            self._clear_inbox_reply(key, pending)
            return False
        if (message.get('chat') or {}).get('type', 'private') != 'private':
            return False
        if not pending.get('prompt_id') or int((message.get('reply_to_message') or {}).get('message_id') or 0) != pending['prompt_id']:
            return False
        if principal.get('role') != ROLE_ADMIN and pending['profile'] != self._selected_profile(principal):
            return True
        if text.startswith('/'):
            return False
        if not text or len(text) > 3500:
            await self._send_text(chat_id, 'Пришлите текст ответа до 3500 символов.')
            return True
        profile = pending['profile']
        if profile not in self._profile_names():
            self._clear_inbox_reply(key, pending)
            return True
        if self._has_active_command(profile):
            await self._send_busy_status(chat_id, principal, profile_name=profile)
            return True
        home = self._profile(profile).home_dir
        try:
            items = await read_thread(self._review_visible_items, profile)
            if pending['token'] not in {item['token'] for item in items}:
                raise ValueError('Черновик устарел или недоступен. Откройте очередь заново.')
            accepted = False
            def accept(state):
                nonlocal accepted
                if state.get('inbox_reply_pending', {}).get(key) != pending:
                    return
                pending_inbox.edit_chat_draft(home, pending['chat_id'], pending['revision'], text)
                state['inbox_reply_pending'].pop(key)
                accepted = True
            self._update_state(accept)
            if not accepted:
                return False
        except ValueError as exc:
            self._clear_inbox_reply(key, pending)
            await self._send_text(chat_id, str(exc))
            return True
        await self._send_text(chat_id, 'Ответ сохранён. Проверьте текст и нажмите «Отправить».')
        items = await read_thread(self._review_visible_items, profile)
        token = next((item['token'] for item in items if item.get('chat_id') == pending['chat_id']
                      and item.get('_inbox_kind') == 'draft'), None)
        await self._show_review_queue(chat_id, principal, profile_name=profile, token=token)
        return True

    @staticmethod
    def _review_score(item: dict) -> int | None:
        if item.get("_inbox_kind") or item.get("status") == "uncertain":
            return None  # unresolved attempts must never enter bulk dismissal
        try:
            return int((item.get("evaluation") or {}).get("score"))
        except (TypeError, ValueError):
            return None

    async def _show_review_list(self, chat_id: int, principal: dict, *, profile_name: str,
                                 page: int = 0, message_id: int = 0, sort: str = "score",
                                 unspecified: bool = False) -> None:
        """One message per page; numbers open the same mixed pending-decision cards."""
        reply_menu = reply_menu_for_command(principal.get("role", ROLE_USER), "/review")
        self._set_selected_menu(principal["user_id"], reply_menu)
        view = DirectionView(self._profile(profile_name).home_dir)
        unknown = await read_thread(self._review_visible_items, profile_name, unspecified=True)
        all_items = await read_thread(self._review_visible_items, profile_name, unspecified=unspecified)
        scope = " · направление не указано" if unspecified else ""
        group_button = ([{"text": f"Направление не указано: {len(unknown)}",
                          "callback_data": f"rq:U:{profile_name}:0:s"}] if unknown and not unspecified else
                        [{"text": "К выбранному направлению", "callback_data": f"rq:L:{profile_name}:0:s"}]
                        if unspecified else [])
        if not all_items:
            text = "✅ Всё разобрано. Сейчас от вас ничего не требуется." + scope
            markup = ({"inline_keyboard": [group_button]} if group_button else
                      self._menu_reply_markup(principal, menu=reply_menu))
            if message_id:
                await self._edit_text(chat_id, message_id, text, reply_markup=markup)
                return
            await self._send_text(
                chat_id, text, reply_markup=markup,
            )
            return
        # Every entry point shares oldest-first order, including legacy sort callbacks.
        all_items = sorted(all_items, key=pending_inbox.oldest_key)
        size = self.REVIEW_PAGE_SIZE
        pages = (len(all_items) + size - 1) // size
        page = max(0, min(int(page), pages - 1))
        chunk = all_items[page * size:(page + 1) * size]
        lines = [f"📋 Ждут решения: {len(all_items)}" + scope + (f" · стр. {page + 1}/{pages}" if pages > 1 else ""),
                 "Сначала старые. Нажмите номер — откроется карточка с кнопками.", ""]
        buttons = []
        for offset, item in enumerate(chunk):
            number = page * size + offset + 1
            vacancy = item.get("vacancy") or {}
            score = (item.get("evaluation") or {}).get("score")
            label = {"form": "📝 Анкеты", "question": "📝 Анкеты", "draft": "💬 Ответ работодателю"}.get(item.get("_inbox_kind"), "")
            title = " · ".join(part for part in (label, " ".join(str(vacancy.get("title") or
                ("" if label else "Вакансия")).split())) if part)
            company = " ".join(str(vacancy.get("company") or "").split())
            salary = " ".join(str(vacancy.get("salary") or "").split())
            details = " · ".join(part for part in (
                str(company[:40]), str(salary[:30]) if salary and "не указ" not in salary else "",
                f"📊 {score}" if score not in (None, "") else "") if part)
            if item.get("status") == "uncertain":
                details = "❔ Нужна сверка" + (f" · {details}" if details else "")
            lines.append(f"{number}. {title[:70]}" + (f"\n     {details}" if details else ""))
            buttons.append({"text": str(number), "callback_data": f"rq:o:{profile_name}:{item.get('token')}"})
        keyboard = [buttons[i:i + 6] for i in range(0, len(buttons), 6)]
        mode = "s"
        if pages > 1:
            keyboard.append([
                {"text": "◀", "callback_data": f"rq:{'U' if unspecified else 'L'}:{profile_name}:{(page - 1) % pages}:{mode}"},
                {"text": f"{page + 1}/{pages}", "callback_data": f"rq:{'U' if unspecified else 'L'}:{profile_name}:{page}:{mode}"},
                {"text": "▶", "callback_data": f"rq:{'U' if unspecified else 'L'}:{profile_name}:{(page + 1) % pages}:{mode}"},
            ])
        scored = [self._review_score(item) for item in all_items]
        direction_tag = f":{view.callback_scope}" if view.name else ""
        dismiss = [{"text": f"👎 < {limit}", "callback_data": f"rq:d:{profile_name}:{limit}{direction_tag}"}
                   for limit in self.REVIEW_DISMISS_THRESHOLDS
                   if not unspecified and any(score is not None and score < limit for score in scored)]
        if dismiss:
            keyboard.append(dismiss)
        if group_button:
            keyboard.append(group_button)
        text, markup = "\n".join(lines), {"inline_keyboard": keyboard}
        if message_id:
            await self._edit_text(chat_id, message_id, text, reply_markup=markup)
        else:
            await self._send_text(chat_id, text, reply_markup=markup)

    async def _review_dismiss_below(self, chat_id: int, principal: dict, profile_name: str, limit: int,
                                    *, confirmed: bool, expected_scope: str | None = None) -> None:
        """«👎 < N»: every pending card scored below N becomes «Мимо» (kept in the feedback log)."""
        view = DirectionView(self._profile(profile_name).home_dir)
        if expected_scope is not None and expected_scope != view.callback_scope:
            await self._send_text(chat_id, "Выбрано другое направление. Откройте «📋 Ждут решения» заново.")
            return
        targets = [item for item in await read_thread(self._review_visible_items, profile_name)
                   if (score := self._review_score(item)) is not None and score < limit]
        if not targets:
            await self._send_text(chat_id, f"Вакансий с баллом ниже {limit} нет.")
            return
        if not confirmed:
            await self._send_text(
                chat_id,
                f"Убрать {len(targets)} вакансий с баллом ниже {limit}? Каждая запишется как «👎 Мимо» — "
                "это пойдёт в обучение поиска.",
                reply_markup={"inline_keyboard": [[
                    {"text": f"✅ Убрать {len(targets)}", "callback_data": f"rq:D:{profile_name}:{limit}" +
                     (f":{view.callback_scope}" if view.name else "")},
                    {"text": "❌ Нет", "callback_data": f"rq:L:{profile_name}:0:s"}]]})
            return
        done = 0
        for item in targets:
            if manual_apply_queue.record_feedback(str(item.get("token") or ""), "bad",
                                                  user_id=int(principal.get("user_id") or 0),
                                                  profile_name=profile_name, consume_decision=True):
                done += 1
        await self._send_text(chat_id, f"👎 Убрал {done} вакансий с баллом ниже {limit}.")
        await self._show_review_queue(chat_id, principal, profile_name=profile_name)

    async def _open_review_card(self, chat_id: int, profile_name: str, token: str) -> None:
        principal = {"user_id": chat_id, "role": ROLE_USER, "profile": profile_name}
        visible = await read_thread(self._review_visible_items, profile_name)
        unspecified = token not in {item['token'] for item in visible}
        if unspecified:
            visible = await read_thread(self._review_visible_items, profile_name, unspecified=True)
        if token not in {item['token'] for item in visible}:
            await self._send_text(chat_id, "Карточка недоступна или уже разобрана. Обновите очередь.")
            return
        await self._show_review_queue(chat_id, principal, profile_name=profile_name, token=token,
                                      unspecified=unspecified)

    async def _route_review(self, chat_id, principal, command, arg, profile_name, reply_menu):
        await self._show_review_queue(chat_id, principal, profile_name=profile_name, reset=True)
        return

    def _review_first_good_hint(self, user_id: int) -> bool:
        first = False
        def remember(state):
            nonlocal first
            users = state.setdefault("review_good_hint_users", {})
            first = not users.get(str(user_id))
            users[str(user_id)] = True
        self._update_state(remember)
        return first

    async def _show_manual_apply_success(self, chat_id, principal, profile_name, token) -> bool:
        item = manual_apply_queue.get_candidate(token, profile_name=profile_name)
        if not item or item.get("status") != "applied":
            return False
        vacancy = item.get("vacancy") or {}
        text = ("📨 Отклик отправлен\n" + str(vacancy.get("title") or "Вакансия")
                + " · " + str(vacancy.get("company") or "Компания не указана"))
        cover = item.get("sent_cover")
        if cover:
            text += "\n\n" + str(cover)[:220] + ("…" if len(str(cover)) > 220 else "")
        markup = ({"inline_keyboard": [[{"text": "📨 Открыть отклик",
                   "callback_data": f"rq:e:{profile_name}:{token}"}]]} if cover is not None else None)
        await self._send_text(chat_id, text, reply_markup=markup)
        await self._show_review_queue(chat_id, principal, profile_name=profile_name, advance_from=token)
        return True

    @profile_command
    async def _start_manual_ai_apply(
        self,
        chat_id: int,
        principal: dict,
        *,
        profile_name: str,
        token: str,
        direction_override: bool = False,  # legacy «mad:» buttons; a card tap always uses its direction
    ) -> None:
        role = principal.get("role", ROLE_USER)
        reply_markup = self._menu_reply_markup(principal)
        if self._has_active_command(profile_name):
            await self._send_busy_status(chat_id, principal, profile_name=profile_name)
            return

        # A card keeps the direction (and resume) it was evaluated in. A tap on it is the
        # decision to apply with that resume, also from the command queue: switch the
        # direction only for this application and restore it afterwards.
        import directions as directions_mod
        home = self._profile(profile_name).home_dir
        switch_to = restore_to = None
        try:
            item = manual_apply_queue.get_candidate(token, profile_name=profile_name) or {}
            bound = (item.get("vacancy") or {}).get("_direction_selection") or {}
            current = directions_mod.active_direction(home)
        except Exception:
            bound, current = {}, None
        bound_name = str(bound.get("direction") or "")
        if bound_name and bound_name != (current or {}).get("name"):
            try:
                card_direction = directions_mod.load_direction(home, bound_name)
            except Exception:
                await self._send_text(chat_id, "Направление, в котором найдена эта вакансия, удалено — "
                                      "нужна новая оценка: запустите поиск.", reply_markup=reply_markup)
                return
            switch_to, restore_to = bound_name, (current or {}).get("name")
            await self._send_text(chat_id, str(
                f"ℹ️ Карточка из направления «{card_direction['title']}» — откликаюсь его резюме; "
                f"выбранное направление «{(current or {}).get('title') or 'без направления'}» вернётся после отклика."))

        label = "manual AI apply"
        active_command = self._mark_active_command(principal=principal, label=label, profile_name=profile_name)
        progress_message = await self._send_text(
            chat_id,
            build_progress_text(label, profile_name=profile_name),
            reply_markup=self._menu_reply_markup(principal),
        )
        progress_task: asyncio.Task | None = None
        progress_message_id = int((progress_message or {}).get("message_id") or 0)
        if progress_message_id > 0:
            progress_task = asyncio.create_task(
                self._run_progress_indicator(
                    chat_id=chat_id,
                    message_id=progress_message_id,
                    label=label,
                    profile_name=profile_name,
                    reply_markup=self._menu_reply_markup(principal),
                    interval_sec=5,
                )
            )

        async def runner() -> None:
            nonlocal progress_task
            try:
                if switch_to:
                    directions_mod.set_active_direction(home, switch_to)
                try:
                    result = await self._run_active_command_capture(
                        active_command,
                        runtime_control.agent_command_argv(profile_name, "--manual-apply-token", token),
                        timeout=1800,
                    )
                finally:
                    if switch_to:
                        with contextlib.suppress(Exception):
                            # Restore only if nobody changed the direction meanwhile.
                            if (directions_mod.active_direction(home) or {}).get("name") == switch_to:
                                directions_mod.set_active_direction(home, restore_to)
                message = format_command_result(label, result, role=role)
                if progress_task:
                    progress_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await progress_task
                    progress_task = None
                if progress_message_id > 0:
                    await self._delete_message(chat_id, progress_message_id)
                if not await self._show_manual_apply_success(chat_id, principal, profile_name, token):
                    await self._send_text(chat_id, message, reply_markup=reply_markup)
            except asyncio.CancelledError:
                if progress_task:
                    progress_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await progress_task
                    progress_task = None
                if progress_message_id > 0:
                    await self._delete_message(chat_id, progress_message_id)
                await self._send_text(
                    chat_id,
                    format_command_result(label, {"cancelled": True}, role=role),
                    reply_markup=reply_markup,
                )
            except Exception as exc:
                log.error("Manual AI apply failed: %s", exc, exc_info=True)
                if progress_task:
                    progress_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await progress_task
                    progress_task = None
                if progress_message_id > 0:
                    await self._delete_message(chat_id, progress_message_id)
                await self._send_text(
                    chat_id,
                    f"❌ Ручной ИИ-отклик завершился с ошибкой:\n{exc}",
                    reply_markup=reply_markup,
                )
            finally:
                if progress_task:
                    progress_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await progress_task
                self._clear_active_command(profile_name)

        active_command.task = self._create_active_task(active_command, runner)
