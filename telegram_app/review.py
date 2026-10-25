"""Telegram review handlers extracted without changing outcomes."""
from __future__ import annotations

import asyncio
import contextlib
import logging

from direction_views import DirectionView
from telegram_app.command_queue import profile_command
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
        item = manual_apply_queue.get_candidate(token, profile_name=profile_name)
        if not item or item.get("status") != "uncertain":
            return False
        await self._open_review_card(chat_id, profile_name, token)
        return True

    @staticmethod
    def _review_item_text(item: dict, index: int) -> str:
        vacancy = item.get("vacancy") or {}
        evaluation = item.get("evaluation") or {}
        title = str(str(vacancy.get("title") or "Вакансия"))
        company = str(str(vacancy.get("company") or "Компания не указана"))
        source = str(str(vacancy.get("source_label") or vacancy.get("source") or "hh.ru"))
        salary = str(str(vacancy.get("salary") or "зарплата не указана"))
        score = evaluation.get("score")
        reason = " ".join(str(evaluation.get("reason") or "").split())
        if len(reason) > 380:
            reason = reason[:377].rstrip() + "..."
        lines = [
            f"{index}. {title}" if index != "•" else f"📌 {title}",
            f"{company} · {source}",
            f"💰 {salary}" + (f" · 📊 {score}/100" if score not in (None, "") else ""),
        ]
        if item.get("status") == "uncertain":
            lines.append("❔ " + (manual_apply_queue.ORPHAN_APPLY_MESSAGE if item.get("orphan_recovered")
                                  else "Нужна сверка: результат отклика не подтверждён."))
        if reason:
            lines.append(str(reason))
        return "\n".join(lines)

    @staticmethod
    def _review_score(item: dict) -> int | None:
        if item.get("status") == "uncertain":
            return None  # unresolved attempts must never enter bulk dismissal
        try:
            return int((item.get("evaluation") or {}).get("score"))
        except (TypeError, ValueError):
            return None

    async def _show_review_queue(self, chat_id: int, principal: dict, *, profile_name: str,
                                 page: int = 0, message_id: int = 0, sort: str = "score",
                                 unspecified: bool = False) -> None:
        """One message per page: a short line per vacancy, a button opens its full card."""
        reply_menu = reply_menu_for_command(principal.get("role", ROLE_USER), "/review")
        self._set_selected_menu(principal["user_id"], reply_menu)
        view = DirectionView(self._profile(profile_name).home_dir)
        items = manual_apply_queue.list_candidates(profile_name, limit=250)
        unknown = view.review_items(items, unspecified=True)
        all_items = view.review_items(items, unspecified=unspecified)
        scope = " · направление не указано" if unspecified else (f" · {view.title}" if view.title else "")
        group = "u" if unspecified else "p"
        group_button = ([{"text": f"Направление не указано: {len(unknown)}",
                          "callback_data": f"rq:u:{profile_name}:0:s"}] if unknown and not unspecified else
                        [{"text": "К выбранному направлению", "callback_data": f"rq:p:{profile_name}:0:s"}]
                        if unspecified else [])
        if not all_items:
            text = ("📋 Вакансий на проверку пока нет" + scope + ". Подходящие вакансии появятся здесь, "
                    "если выбран режим просмотра или подтверждения.")
            markup = ({"inline_keyboard": [group_button]} if group_button else
                      self._menu_reply_markup(principal, menu=reply_menu))
            if message_id:
                await self._edit_text(chat_id, message_id, text, reply_markup=markup)
                return
            await self._send_text(
                chat_id, text, reply_markup=markup,
            )
            return
        sort = "new" if sort == "new" else "score"
        if sort == "score":
            # Best first; unscored at the end; ties keep «newest first».
            all_items = sorted(all_items, key=lambda item: (self._review_score(item) is None,
                                                           -(self._review_score(item) or 0)))
        size = self.REVIEW_PAGE_SIZE
        pages = (len(all_items) + size - 1) // size
        page = max(0, min(int(page), pages - 1))
        chunk = all_items[page * size:(page + 1) * size]
        lines = [f"📋 Вакансии на проверку: {len(all_items)}" + scope + (f" · стр. {page + 1}/{pages}" if pages > 1 else ""),
                 ("Сверху лучшие по баллу." if sort == "score" else "Сверху новые.")
                 + " Нажмите номер — откроется карточка с кнопками.", ""]
        buttons = []
        for offset, item in enumerate(chunk):
            number = page * size + offset + 1
            vacancy = item.get("vacancy") or {}
            score = (item.get("evaluation") or {}).get("score")
            title = " ".join(str(vacancy.get("title") or "Вакансия").split())
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
        mode = "s" if sort == "score" else "n"
        if pages > 1:
            keyboard.append([
                {"text": "◀", "callback_data": f"rq:{group}:{profile_name}:{(page - 1) % pages}:{mode}"},
                {"text": f"{page + 1}/{pages}", "callback_data": f"rq:{group}:{profile_name}:{page}:{mode}"},
                {"text": "▶", "callback_data": f"rq:{group}:{profile_name}:{(page + 1) % pages}:{mode}"},
            ])
        keyboard.append([{"text": "🆕 Сначала новые" if sort == "score" else "📊 Сначала лучшие",
                          "callback_data": f"rq:{group}:{profile_name}:0:{'n' if sort == 'score' else 's'}"}])
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
            await self._send_text(chat_id, "Выбрано другое направление. Обновите список «Вакансии на проверку».")
            return
        targets = [item for item in view.review_items(manual_apply_queue.list_candidates(profile_name, limit=250))
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
                    {"text": "❌ Нет", "callback_data": f"rq:p:{profile_name}:0:s"}]]})
            return
        done = 0
        for item in targets:
            if manual_apply_queue.record_feedback(str(item.get("token") or ""), "bad",
                                                  user_id=int(principal.get("user_id") or 0),
                                                  profile_name=profile_name):
                done += 1
        await self._send_text(chat_id, f"👎 Убрал {done} вакансий с баллом ниже {limit}.")
        await self._show_review_queue(chat_id, principal, profile_name=profile_name)

    async def _open_review_card(self, chat_id: int, profile_name: str, token: str) -> None:
        item = manual_apply_queue.get_candidate(token, profile_name=profile_name)
        if not item or item.get("status") not in {"pending", "uncertain"}:
            await self._send_text(chat_id, "Эта вакансия уже разобрана. Обновите список: «На разбор».")
            return
        view = DirectionView(self._profile(profile_name).home_dir)
        owner = view.item_direction(item)
        if owner is not None and owner != view.name:
            await self._send_text(chat_id, "Выбрано другое направление. Обновите список «Вакансии на проверку».")
            return
        markup = manual_apply_queue.build_manual_apply_markup(
            item.get("vacancy") or {}, profile_name, token,
            include_feedback=item.get("status") != "uncertain",
            allow_ai_apply=item.get("status") == "pending" and bool(item.get("allow_ai_apply", True)))
        if item.get("status") == "uncertain" and item.get("orphan_recovered"):
            markup = manual_apply_queue.build_orphaned_apply_markup(item, profile_name)
        label = "Направление не указано.\n\n" if owner is None else (f"{view.title}\n\n" if view.title else "")
        await self._send_text(chat_id, label + self._review_item_text(item, "•"), reply_markup=markup)

    async def _route_review(self, chat_id, principal, command, arg, profile_name, reply_menu):
        await self._show_review_queue(chat_id, principal, profile_name=profile_name)
        return

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
