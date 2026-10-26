"""Telegram navigation handlers extracted without changing outcomes."""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from telegram_app.auth_bridge import looks_like_standalone_hh_auth_code as _looks_like_standalone_hh_auth_code
from telegram_app.command_queue import current_entry
from telegram_app.command_queue import profile_command
from telegram_app.guards import read_command
from telegram_app.routing import COMMAND_ROUTES
from telegram_app.routing import route_for_command
from telegram_bot_ui import (
    ADMIN_BUTTON_MAP,
    ADMIN_ONLY_COMMANDS,
    CLIENT_ONBOARDING_STEPS,
    LEGACY_BUTTON_MAP,
    MENU_MAIN,
    MENU_RESPONSES,
    MENU_PARENTS,
    ROLE_ADMIN,
    ROLE_USER,
    USER_BUTTON_MAP,
    _command_conflicts_with_active,
    _normalize_menu,
    _pretty_profile_name,
    _resolve_guest_command,
    _resolve_message_command,
    build_busy_status_text,
    build_client_review_inline_markup,
    build_client_status_text,
    build_guest_reply_markup,
    build_guest_welcome_text,
    build_help_text,
    build_hh_resumes_text,
    build_menu_section_text,
    build_menu_inline_markup,
    build_responses_summary_text,
    reply_menu_for_command,
)
import client_hh_auth
import runtime_control
import telegram_access
import telegram_clients

log = logging.getLogger("telegram_bot")


class TelegramNavigation:
    async def _send_busy_status(self, chat_id: int, principal: dict, *, profile_name: str | None = None) -> None:
        target_profile = profile_name or self._selected_profile(principal)
        command = self._active_command(target_profile)
        if not command:
            await self._send_text(
                chat_id,
                f"✅ Для профиля {_pretty_profile_name(target_profile)} сейчас нет активной команды.\n"
                f"В очереди: {self._queue_length(target_profile)}"
                + (" · ⏸ на паузе" if target_profile in self._queue_paused else ""),
                reply_markup=self._menu_reply_markup(principal),
            )
            return
        can_cancel = self._can_cancel_active(principal, command)
        from telegram_bot_ui import build_active_stop_markup
        await self._send_text(
            chat_id,
            build_busy_status_text(
                active_command=command.label,
                queue_length=self._queue_length(target_profile),
                profile_name=command.profile_name,
                elapsed_sec=self._active_elapsed_sec(command),
                pid=command.subprocess_pid,
                can_cancel=can_cancel,
                role=principal.get("role", ROLE_USER),
            ),
            reply_markup=(build_active_stop_markup(target_profile, command) if can_cancel
                          else self._menu_reply_markup(principal)),
        )

    async def _send_getting_started(self, chat_id: int, principal: dict, *, only_if_incomplete: bool = False, cached: bool = False) -> None:
        from telegram_app import onboarding
        import directions as directions_mod
        profile_name = self._selected_profile(principal)
        profile = self._profile(profile_name)
        from hh.session_probe import probe, save_catalog, OK
        checked = ({"status": OK if Path(profile.hh.cookies_file).is_file() else "login",
                    "message": "Проверить вход можно кнопкой ниже.",
                    "resumes": client_hh_auth.load_hh_resume_catalog(profile_name)}
                   if cached else await asyncio.to_thread(probe, profile.hh.cookies_file))
        resumes = checked["resumes"] if checked["status"] == OK else []
        if checked["status"] == OK and not cached:
            try:
                save_catalog(client_hh_auth.hh_resume_catalog_path(profile_name), resumes)
            except Exception as exc:
                log.warning("resume catalog cache refresh failed: %s", type(exc).__name__)
        try:
            items = directions_mod.list_directions(profile.home_dir)
            active = directions_mod.active_direction(profile.home_dir)
        except Exception:
            items, active = [], None
        text, rows, complete = onboarding.build(
            hh_ok=checked["status"] == OK, hh_message=checked["message"],
            resumes=[r for r in resumes if isinstance(r, dict)],
            directions=items, active=active,
            mode=str((active or {}).get("application_mode") or profile.hh.application_mode or ""),
            has_search=runtime_control.has_completed_search(profile.run_history_file),
        )
        if only_if_incomplete and complete:
            return
        markup = {"inline_keyboard": [[{"text": label, "callback_data": onboarding.CALLBACK_PREFIX + action}
                                       for label, action in row] for row in rows]}
        await self._send_text(chat_id, text, reply_markup=markup)

    async def _send_section_actions(self, chat_id: int, principal: dict, menu: str | None = None) -> None:
        from telegram_app import history
        selected_menu = menu or self._selected_menu(principal)
        role = principal.get("role", ROLE_USER)
        profile = self._profile(self._selected_profile(principal))
        markup = build_menu_inline_markup(role, selected_menu, token=history.token(profile.home_dir),
            application_mode=self._effective_application_mode(principal))
        if markup["inline_keyboard"]:
            await self._send_text(chat_id, "Выберите действие:", reply_markup=markup)

    async def _send_menu(self, chat_id: int, principal: dict, *, profile_name: str, menu: str = MENU_MAIN, help_section: bool = False) -> None:
        from telegram_app import history
        from telegram_app.guards import read_thread
        role = principal.get("role", ROLE_USER)
        selected_menu = _normalize_menu(role, menu)
        self._set_selected_menu(principal["user_id"], selected_menu)
        if selected_menu == MENU_RESPONSES:
            snapshot = await read_thread(history.responses_snapshot, self._profile(profile_name))
            text = build_responses_summary_text(snapshot)
        else:
            text = (build_help_text(role, profile_name=profile_name, help_section=help_section)
                    if selected_menu == MENU_MAIN else
                    build_menu_section_text(selected_menu, role=role, profile_name=profile_name))
        await self._send_text(chat_id, text, reply_markup=self._menu_reply_markup(principal, menu=selected_menu))
        if selected_menu != MENU_MAIN:
            await self._send_section_actions(chat_id, principal, selected_menu)

    async def _send_start_summary(self, chat_id: int, principal: dict, profile_name: str) -> bool:
        """Restore context from local state; opening /start never probes hh.ru."""
        import directions
        import application_history
        import json
        import time
        import company_blacklist
        from direction_views import DirectionView
        from telegram_app import history
        from telegram_app.guards import read_thread
        profile = self._profile(profile_name)
        try:
            active = directions.active_direction(profile.home_dir) or {}
        except (OSError, ValueError, PermissionError):
            return False
        mode = str(active.get("application_mode") or profile.hh.application_mode or "")
        resumes = client_hh_auth.load_hh_resume_catalog(profile_name)
        if not (active and resumes and Path(profile.hh.cookies_file).is_file()
                and mode in {"preview", "confirm", "auto"}):
            return False
        # Scope the count exactly like the decision inbox, including tester restrictions.
        view = DirectionView(profile.home_dir)
        try:
            queue = json.loads((Path(profile.home_dir) / "manual_apply_queue.json").read_text(encoding="utf-8"))
        except FileNotFoundError:
            queue = {"items": {}}
        candidates = [item for item in queue.get("items", {}).values()
                      if isinstance(item, dict) and item.get("status") in {"pending", "uncertain"}
                      and item.get("profile_name", profile_name) == profile_name
                      and (item.get("status") == "uncertain" or (
                          float(item.get("snoozed_until") or 0) <= time.time()
                          and (not item.get("created_ts") or time.time() - float(item["created_ts"]) <= 7 * 24 * 60 * 60)
                          and not company_blacklist.is_blocked((item.get("vacancy") or {}).get("company", ""), profile_name)))]
        pending = len(view.review_items(candidates))
        applications = await read_thread(application_history.summarize, profile.home_dir,
            events_file=profile.analytics_events_file, seen_file=profile.seen_file, days=7)
        daemon = self._daemon_state(profile_name)
        interval, _ = self._profile_schedule(profile_name)
        next_search = (f"в течение {interval} мин (по расписанию)" if daemon.get("running") and interval > 0
                       else "расписание выключено")
        labels = {"preview": "👀 Только показать", "confirm": "✋ Подтверждать отклик", "auto": "⚡ Автоматически"}
        prefix = "не меньше " if applications.get("incomplete") else ""
        text = (f"Job Hunter работает\n\n📋 Ждут решения: {pending}\n"
                f"📨 Отправлено за неделю: {prefix}{applications['count']}\n"
                f"🔎 Следующий поиск: {next_search}\n\nРежим откликов: {labels[mode]}")
        self._set_selected_menu(principal["user_id"], MENU_MAIN)
        await self._send_text(chat_id, text, reply_markup=self._menu_reply_markup(principal, menu=MENU_MAIN))
        if pending:
            await self._send_text(chat_id, "Подходящие вакансии ждут вашего решения.", reply_markup={"inline_keyboard": [[{
                "text": f"📋 Посмотреть {pending}", "callback_data": f"nav:{history.token(profile.home_dir)}:review"}]]})
        return True

    async def _send_guest_welcome(self, chat_id: int, user_id: int) -> None:
        await self._send_text(
            chat_id,
            build_guest_welcome_text(telegram_clients.get_client(user_id)),
            reply_markup=build_guest_reply_markup(),
        )

    async def _dispatch_guest(self, chat_id: int, sender: dict, command: str, arg: str, text: str) -> None:
        user_id = int(sender.get("id") or 0)
        username = str(sender.get("username") or "").strip()
        first_name = str(sender.get("first_name") or "").strip()
        last_name = str(sender.get("last_name") or "").strip()
        guest_state = self._guest_state(user_id)
        active_step = str(guest_state.get("step") or "").strip()

        if command in {"", "/start", "/help"} and not active_step:
            await self._send_guest_welcome(chat_id, user_id)
            return

        if command == "/client_status":
            client = telegram_clients.get_client(user_id)
            if not client:
                await self._send_text(
                    chat_id,
                "📄 Заявки пока нет. Нажмите «Стать клиентом», чтобы начать.",
                reply_markup=build_guest_reply_markup(),
            )
                return
            await self._send_text(chat_id, build_client_status_text(client), reply_markup=build_guest_reply_markup())
            return

        if command == "/hh_auth":
            client = telegram_clients.get_client(user_id)
            if not client or client.get("status") != telegram_clients.STATUS_APPROVED:
                await self._send_text(
                    chat_id,
                    "🔒 Вход HH станет доступен после одобрения заявки.",
                    reply_markup=build_guest_reply_markup(),
                )
                return
            await self._request_hh_auth(chat_id, user_id, client, reply_markup=build_guest_reply_markup())
            return

        if command == "/hh_resumes":
            client = telegram_clients.get_client(user_id)
            if not client or not client.get("profile_name"):
                await self._send_text(
                    chat_id,
                    "🧾 HH резюме пока недоступны: профиль ещё не создан.",
                    reply_markup=build_guest_reply_markup(),
                )
                return
            resumes = client_hh_auth.load_hh_resume_catalog(client["profile_name"])
            await self._send_text(chat_id, build_hh_resumes_text(client["profile_name"], resumes), reply_markup=build_guest_reply_markup())
            return

        if command == "/client_start":
            telegram_clients.start_onboarding(
                user_id,
                username=username,
                first_name=first_name,
                last_name=last_name,
            )
            self._set_guest_state(user_id, step=CLIENT_ONBOARDING_STEPS[0], answers={})
            await self._send_text(
                chat_id,
                "🆕 Начинаем заявку.\n\nКак к вам обращаться? Напишите имя и фамилию.",
                reply_markup=build_guest_reply_markup(),
            )
            return

        if not active_step:
            await self._send_guest_welcome(chat_id, user_id)
            return

        answers = dict(guest_state.get("answers") or {})
        value = (text or "").strip()
        if not value:
            await self._send_text(chat_id, "✍️ Нужен текстовый ответ. Попробуйте ещё раз.", reply_markup=build_guest_reply_markup())
            return

        answers[active_step] = value if not (active_step == "notes" and value == "-") else ""
        current_index = CLIENT_ONBOARDING_STEPS.index(active_step)
        if current_index + 1 < len(CLIENT_ONBOARDING_STEPS):
            next_step = CLIENT_ONBOARDING_STEPS[current_index + 1]
            self._set_guest_state(user_id, step=next_step, answers=answers)
            prompts = {
                "target_role": "На какую роль или направление нацелены?",
                "target_location": "Какую локацию/формат рассматриваете? Например: СПб, удалённо, РФ.",
                "notes": "Добавьте комментарий о себе или ожиданиях. Если нечего добавить, отправьте -.",
            }
            await self._send_text(chat_id, prompts[next_step], reply_markup=build_guest_reply_markup())
            return

        client = telegram_clients.submit_application(
            user_id,
            username=username,
            first_name=first_name,
            last_name=last_name,
            full_name=answers.get("full_name", ""),
            target_role=answers.get("target_role", ""),
            target_location=answers.get("target_location", ""),
            notes=answers.get("notes", ""),
        )
        self._clear_guest_state(user_id)
        await self._send_text(
            chat_id,
            (
                "✅ Заявка отправлена.\n\n"
                f"{build_client_status_text(client)}\n\n"
                "Следующий шаг после проверки: выдача профиля и запуск входа через браузер для сохранения сессии."
            ),
            reply_markup=build_guest_reply_markup(),
        )
        await self._notify_admins(
            "\n".join([
                "🆕 Новая клиентская заявка",
                "",
                f"• Пользователь Telegram: {user_id}",
                f"• Имя: {client.get('full_name') or '—'}",
                f"• Направление: {client.get('target_role') or '—'}",
                f"• Локация: {client.get('target_location') or '—'}",
                "",
                "Кнопки ниже или команды:",
                f"• /client_approve {user_id}",
                f"• /client_reject {user_id} причина",
            ]),
            reply_markup=build_client_review_inline_markup(user_id),
        )

    @telegram_access.scoped_request
    async def _handle_update(self, update: dict) -> None:
        callback = update.get("callback_query") or {}
        if callback:
            await self._handle_callback_query(callback)
            return
        message = update.get("message") or {}
        if (message.get("chat") or {}).get("type") != "private":
            return
        sender = message.get("from") or {}
        user_id = int(sender.get("id") or 0)
        chat_id = int((message.get("chat") or {}).get("id") or 0)
        principal = telegram_access.resolve_user(user_id)
        if not principal:
            if chat_id:
                command, arg = _resolve_guest_command(message.get("text") or "")
                await self._dispatch_guest(chat_id, sender, command, arg, message.get("text") or "")
            return

        text = (message.get("text") or "").strip()

        if await self._accept_candidate_document(chat_id, principal, message):
            return

        if await self._maybe_accept_hh_auth_response(chat_id, principal, text):
            return

        if await self._accept_form_answer(chat_id, principal, message):
            return

        if await self._accept_employer_question_text(chat_id, principal, text):
            return

        if await self._accept_direction_input(chat_id, principal, text):
            return

        if await self._accept_search_query_input(chat_id, principal, text):
            return

        if await self._accept_candidate_interview_input(chat_id, principal, text):
            return

        # Captcha привязана к профилю: её может решить владелец своей сессии
        # либо админ в выбранном профиле.
        if (
            text
            and len(text) <= 100
            and not text.startswith("/")
            and not text.startswith("➡")
        ):
            pending, captcha_profile_name = self._captcha_pending_for_principal(principal)
            if pending:
                # Исключаем кнопки меню: их нельзя считать текстом captcha.
                is_menu_button = (
                    text in ADMIN_BUTTON_MAP
                    or text in USER_BUTTON_MAP
                    or text in LEGACY_BUTTON_MAP
                )
                if not is_menu_button:
                    try:
                        import captcha_bridge as cb
                        cb.write_response(pending["id"], text, profile_name=captcha_profile_name)
                        await self._send_text(
                            chat_id,
                            "✅ Принял текст captcha. Вставляю его в форму hh.ru…",
                        )
                        return
                    except Exception as exc:
                        log.warning("captcha response write failed: %s", exc)

        command, arg = _resolve_message_command(text, principal.get("role", ROLE_USER))
        if _looks_like_standalone_hh_auth_code(text):
            await self._send_text(
                chat_id,
                "🔐 Похоже на HH SMS-код, но активного запроса входа сейчас нет. "
                "Сначала нажми восстановление HH-сессии, дождись сообщения «HH просит SMS-код», "
                "и только потом пришли код сюда.",
                reply_markup=self._menu_reply_markup(principal),
            )
            return
        if not command:
            return
        await self._dispatch(chat_id, principal, command, arg)

    @read_command
    @profile_command
    @telegram_access.scoped_request
    async def _dispatch(self, chat_id: int, principal: dict, command: str, arg: str) -> None:
        role = principal.get("role", ROLE_USER)
        profile_name = self._selected_profile(principal)
        active_command = self._active_command(profile_name)
        route = route_for_command(command)

        if route and route.phase == "impersonation" and not (role == ROLE_ADMIN or principal.get("impersonating")):
            route = None
        if route and route.phase in {"early", "impersonation"}:
            await getattr(self, route.handler)(chat_id, principal, command, arg, profile_name, None)
            return
        if (current_entry() is None and (active_command or self._queue_launching.get(profile_name)
                                         or (self._queue_length(profile_name) and command not in {"/hh_auth", "/client_hh_auth"}))
                and _command_conflicts_with_active(command)):
            await self._send_busy_status(chat_id, principal, profile_name=profile_name)
            return

        if route and route.phase == "public":
            await getattr(self, route.handler)(chat_id, principal, command, arg, profile_name, None)
            return

        if command in ADMIN_ONLY_COMMANDS and role != ROLE_ADMIN:
            await self._send_text(chat_id, "🔒 Нужны права администратора.", reply_markup=self._menu_reply_markup(principal))
            return

        if route and route.phase == "direction":
            await getattr(self, route.handler)(chat_id, principal, command, arg, profile_name, None)
            return

        if route and route.direction_command:
            import directions
            pending = self._direction_sessions().get(principal["user_id"])
            if directions.active_direction(self._profile(profile_name).home_dir) or (
                command == "/search_use_draft" and pending
            ):
                direction_menu = reply_menu_for_command(role, command)
                if direction_menu is not None:
                    self._set_selected_menu(principal["user_id"], direction_menu)
                await self._dispatch_direction(chat_id, principal, route.direction_command, arg)
                return

        reply_menu = reply_menu_for_command(role, command)
        if reply_menu is not None:
            self._set_selected_menu(principal["user_id"], reply_menu)

        handler = route.handler if route else "_route_unknown"
        await getattr(self, handler)(chat_id, principal, command, arg, profile_name, reply_menu)
        if route and route.actions:
            await self._send_section_actions(chat_id, principal)

    async def _route_busy(self, chat_id, principal, command, arg, profile_name, reply_menu):
        await self._send_busy_status(chat_id, principal, profile_name=profile_name)
        return

    async def _route_back(self, chat_id, principal, command, arg, profile_name, reply_menu):
        parent = MENU_PARENTS.get(self._selected_menu(principal), MENU_MAIN)
        await self._send_menu(chat_id, principal, profile_name=profile_name, menu=parent)
        return

    async def _route_main(self, chat_id, principal, command, arg, profile_name, reply_menu):
        if principal.get("impersonating"):
            await self._send_text(chat_id, f"👤 Режим «смотреть как клиент»: профиль {profile_name}. Вернуться: /act_as_self")
        if command == "/start" and await self._send_start_summary(chat_id, principal, profile_name):
            return
        await self._send_menu(chat_id, principal, profile_name=profile_name, menu=MENU_MAIN, help_section=command == "/help")
        if command == "/start":
            await self._send_getting_started(chat_id, principal, only_if_incomplete=True, cached=True)
        return

    async def _route_getting_started(self, chat_id, principal, command, arg, profile_name, reply_menu):
        role = principal.get("role", ROLE_USER)
        owner_menu = reply_menu_for_command(role, "/getting_started") or MENU_MAIN
        self._set_selected_menu(principal["user_id"], owner_menu)
        await self._send_text(chat_id, "🚀 С чего начать — пошаговая настройка.",
                              reply_markup=self._menu_reply_markup(principal, menu=owner_menu))
        await self._send_getting_started(chat_id, principal)
        return

    async def _route_menu(self, chat_id, principal, command, arg, profile_name, reply_menu):
        await self._send_menu(chat_id, principal, profile_name=profile_name, menu=COMMAND_ROUTES[command].args[0])
        return

    async def _route_unknown(self, chat_id, principal, command, arg, profile_name, reply_menu):
        await self._send_text(chat_id, f"❓ Неизвестная команда: {command}", reply_markup=self._menu_reply_markup(principal))
