"""Telegram admin handlers extracted without changing outcomes."""
from __future__ import annotations

import contextlib
import profile as profile_mod
import time

from telegram_bot_ui import (
    MENU_ADMIN,
    MENU_APPLICATION_MODE,
    MENU_HH,
    MENU_LLM,
    MENU_MAIN,
    MENU_RUN,
    ROLE_USER,
    _format_users_text,
    _pretty_command_label,
    _pretty_profile_name,
    build_ai_limits_text,
    build_busy_status_text,
    build_client_status_text,
    build_clients_inline_markup,
    build_clients_text,
    build_guest_reply_markup,
    build_profiles_text,
    build_reply_markup,
    format_ai_snapshot_text,
    reply_menu_for_command,
)
import admin_llm
import runtime_control
import telegram_access
import telegram_clients
import telegram_resume_limits


class TelegramAdmin:
    async def _cancel_active_command(self, chat_id: int, principal: dict, *, profile_name: str | None = None) -> None:
        target_profile = profile_name or self._selected_profile(principal)
        command = self._active_command(target_profile)
        if not command:
            # Свежезапущенной из бота команды нет — ищем внешние agent.py-процессы
            # этого профиля (запущенные через cron/shell/watchdog) и стопим их.
            external = runtime_control.find_agent_pids_for_profile(target_profile)
            if not external:
                await self._send_text(
                    chat_id,
                    f"✅ Для профиля {_pretty_profile_name(target_profile)} сейчас нет активной команды.",
                    reply_markup=self._menu_reply_markup(principal),
                )
                return
            killed: list[str] = []
            for entry in external:
                result = runtime_control.stop_pid(
                    entry["pid"],
                    timeout=10.0,
                    process_group=True,
                    expected_profile=target_profile,
                    expected_argv=entry["argv"],
                )
                if result.get("already_stopped") or not result.get("ok"):
                    continue
                flag_label = runtime_control.AGENT_FLAG_LABELS.get(entry["flag"], entry["flag"])
                killed.append(
                    f"{flag_label} (pid={entry['pid']}, {result.get('signal', 'SIGTERM')})"
                )
            if not killed:
                await self._send_text(
                    chat_id,
                    f"⚪️ Внешние процессы профиля {_pretty_profile_name(target_profile)} уже завершались.",
                    reply_markup=self._menu_reply_markup(principal),
                )
                return
            await self._send_text(
                chat_id,
                "⏹ Остановлено: " + "; ".join(killed),
                reply_markup=self._menu_reply_markup(principal),
            )
            return
        menu = self._selected_menu(principal)
        mode = self._effective_application_mode(principal) if menu == MENU_APPLICATION_MODE else ""
        if not self._can_cancel_active(principal, command):
            await self._send_text(
                chat_id,
                build_busy_status_text(
                    active_command=command.label,
                    queue_length=self._queue_length(target_profile),
                    profile_name=command.profile_name,
                    elapsed_sec=self._active_elapsed_sec(command),
                    pid=command.subprocess_pid,
                    can_cancel=False,
                ),
                reply_markup=build_reply_markup(
                    principal.get("role", ROLE_USER),
                    menu=menu,
                    active=True,
                    can_stop=False,
                    application_mode=mode,
                ),
            )
            return

        result = self._request_active_command_cancel(command)
        if result is not None:
            signal_name = result.get("signal", "SIGTERM")
            await self._send_text(
                chat_id,
                (
                    f"⏹ Останавливаю {_pretty_command_label(command.label)} "
                    f"для {_pretty_profile_name(command.profile_name)} "
                    f"(pid={command.subprocess_pid}, {signal_name})."
                ),
                reply_markup=build_reply_markup(
                    principal.get("role", ROLE_USER),
                    menu=menu,
                    active=True,
                    can_stop=False,
                    application_mode=mode,
                ),
            )
            return

        await self._send_text(
            chat_id,
            (
                f"⏹ Останавливаю {_pretty_command_label(command.label)} "
                f"для {_pretty_profile_name(command.profile_name)}."
            ),
            reply_markup=build_reply_markup(
                principal.get("role", ROLE_USER),
                menu=menu,
                active=True,
                can_stop=False,
                application_mode=mode,
            ),
        )

    async def _approve_client(self, chat_id: int, principal: dict, target_user_id: int, *, profile_name: str = "") -> dict | None:
        reply_markup = self._menu_reply_markup(principal)
        client = telegram_clients.get_client(target_user_id)
        if not client:
            await self._send_text(chat_id, f"❌ Клиент не найден: {target_user_id}", reply_markup=reply_markup)
            return None
        resolved_profile = profile_name.strip() or client.get("profile_name") or self._default_client_profile_name(target_user_id)
        if resolved_profile not in self._profile_names():
            queries = [client.get("target_role") or "поиск работы"]
            profile_mod.create_profile(resolved_profile, search_queries=queries)
        telegram_access.upsert_user(
            target_user_id,
            profile=resolved_profile,
            role=ROLE_USER,
            label=client.get("full_name") or client.get("username") or "",
        )
        client = telegram_clients.set_status(
            target_user_id,
            status=telegram_clients.STATUS_APPROVED,
            auth_status=telegram_clients.AUTH_NOT_STARTED,
            profile_name=resolved_profile,
            admin_note="",
        )
        self._set_selected_menu(target_user_id, MENU_RUN)
        self._append_debug_log(
            "client_approved",
            admin_user_id=principal.get("user_id"),
            target_user_id=target_user_id,
            profile_name=resolved_profile,
        )
        await self._send_text(
            chat_id,
            f"✅ Клиент одобрен: {target_user_id} -> профиль {resolved_profile}",
            reply_markup=reply_markup,
        )
        with contextlib.suppress(Exception):
            await self._send_text(
                target_user_id,
                (
                    "✅ Ваша заявка одобрена.\n\n"
                    f"{build_client_status_text(client)}\n\n"
                    "Следующий этап: вход в HH.\n"
                    "Кнопка «🔐 Вход HH» уже показана ниже на клавиатуре.\n"
                    "Пароль в Telegram мы не просим."
                ),
                reply_markup=build_reply_markup(ROLE_USER, menu=MENU_HH),
            )
        return client

    async def _reject_client(self, chat_id: int, principal: dict, target_user_id: int, *, admin_note: str = "") -> dict | None:
        reply_markup = self._menu_reply_markup(principal)
        client = telegram_clients.get_client(target_user_id)
        if not client:
            await self._send_text(chat_id, f"❌ Клиент не найден: {target_user_id}", reply_markup=reply_markup)
            return None
        telegram_access.remove_user(target_user_id)
        client = telegram_clients.set_status(
            target_user_id,
            status=telegram_clients.STATUS_REJECTED,
            admin_note=admin_note,
        )
        self._append_debug_log(
            "client_rejected",
            admin_user_id=principal.get("user_id"),
            target_user_id=target_user_id,
            admin_note=admin_note,
        )
        await self._send_text(
            chat_id,
            f"🛑 Заявка отклонена: {target_user_id}",
            reply_markup=reply_markup,
        )
        with contextlib.suppress(Exception):
            await self._send_text(
                target_user_id,
                build_client_status_text(client),
                reply_markup=build_guest_reply_markup(),
            )
        return client

    async def _route_impersonation(self, chat_id, principal, command, arg, profile_name, reply_menu):
        target = "" if command == "/act_as_self" else arg.strip()
        try:
            if target and target not in self._profile_names():
                raise RuntimeError(f"Профиль {target} не найден.")
            telegram_access.set_impersonation(principal["user_id"], target or None)
        except RuntimeError as exc:
            await self._send_text(chat_id, f"❌ {exc}")
            return
        fresh = telegram_access.resolve_user(principal["user_id"]) or principal
        note = (f"👤 Теперь вы видите бота как клиент профиля {target}. Вернуться: /act_as_self"
                if target else "👤 Вы снова администратор.")
        await self._send_text(chat_id, note, reply_markup=self._menu_reply_markup(fresh, menu=MENU_MAIN))
        return

    async def _route_cancel_active(self, chat_id, principal, command, arg, profile_name, reply_menu):
        await self._cancel_active_command(chat_id, principal, profile_name=profile_name)
        return

    async def _route_llm(self, chat_id, principal, command, arg, profile_name, reply_menu):
        if command == "/menu_llm":
            self._set_selected_menu(principal["user_id"], MENU_LLM)
        markup = self._menu_reply_markup(principal)
        if command == "/menu_llm":
            message = admin_llm.overview()
        elif command == "/llm_balance":
            message = await admin_llm.diagnostic()
        elif self._llm_test_lock.locked() or time.monotonic() < self._llm_test_next_at:
            message = "⏳ Тест доступен не чаще раза в минуту. Дождитесь завершения предыдущего."
        else:
            async with self._llm_test_lock:
                self._llm_test_next_at = time.monotonic() + 60
                await self._send_text(chat_id, "🧪 Проверяю DeepSeek…", reply_markup=markup)
                message = await admin_llm.diagnostic(test=True)
        await self._send_text(chat_id, message, reply_markup=markup)
        return

    async def _route_profiles(self, chat_id, principal, command, arg, profile_name, reply_menu):
        from telegram_app import history
        profiles = self._profile_names()
        await self._send_text(chat_id, "Выберите профиль:", reply_markup=self._menu_reply_markup(principal))
        await self._send_text(
            chat_id,
            build_profiles_text(profiles, selected_profile=profile_name),
            reply_markup={"inline_keyboard": [[{"text": name,
                "callback_data": f"nav:{history.profile_picker_token(self._profile(profile_name).home_dir, profiles)}:profile:{index}"}]
                for index, name in enumerate(profiles)]},
        )
        return

    async def _route_profile(self, chat_id, principal, command, arg, profile_name, reply_menu):
        target = arg.strip()
        if not target:
            await self._send_text(chat_id, "ℹ️ Использование: /profile <имя_профиля>", reply_markup=self._menu_reply_markup(principal))
            return
        if target not in self._profile_names():
            await self._send_text(chat_id, f"❌ Неизвестный профиль: {target}", reply_markup=self._menu_reply_markup(principal))
            return
        self._direction_sessions().pop(principal["user_id"], None)
        self._clear_search_settings_state(principal["user_id"], "search_input", "search_query_draft")
        self._set_selected_profile(principal["user_id"], target)
        self._set_selected_menu(principal["user_id"], MENU_MAIN)
        await self._send_text(chat_id, f"✅ Выбран профиль: {target}", reply_markup=self._menu_reply_markup(principal, menu=MENU_MAIN))
        return

    async def _route_users(self, chat_id, principal, command, arg, profile_name, reply_menu):
        await self._send_text(chat_id, _format_users_text(telegram_access.list_users()), reply_markup=self._menu_reply_markup(principal))
        return

    async def _route_client_status(self, chat_id, principal, command, arg, profile_name, reply_menu):
        client = self._client_record(principal["user_id"])
        if not client:
            await self._send_text(
                chat_id,
                "ℹ️ Клиентская заявка для этого Telegram-пользователя пока не заведена.",
                reply_markup=self._menu_reply_markup(principal),
            )
            return
        await self._send_text(chat_id, build_client_status_text(client), reply_markup=self._menu_reply_markup(principal))
        return

    async def _route_clients(self, chat_id, principal, command, arg, profile_name, reply_menu):
        clients = telegram_clients.list_clients()
        actions_markup = build_clients_inline_markup(clients)
        if actions_markup:
            await self._send_text(chat_id, "🧑‍💼 Клиенты", reply_markup=self._menu_reply_markup(principal))
        await self._send_text(
            chat_id,
            build_clients_text(clients),
            reply_markup=actions_markup or self._menu_reply_markup(principal),
        )
        return

    async def _route_client_hh_auth(self, chat_id, principal, command, arg, profile_name, reply_menu):
        parts = [part for part in arg.split() if part]
        if not parts:
            await self._send_text(
                chat_id,
                "ℹ️ Использование: /client_hh_auth <id_пользователя>",
                reply_markup=self._menu_reply_markup(principal),
            )
            return
        try:
            target_user_id = int(parts[0])
        except ValueError:
            await self._send_text(chat_id, "❌ Некорректный ID пользователя.", reply_markup=self._menu_reply_markup(principal))
            return
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

    async def _route_client_approve(self, chat_id, principal, command, arg, profile_name, reply_menu):
        parts = [part for part in arg.split() if part]
        if not parts:
            await self._send_text(
                chat_id,
                "ℹ️ Использование: /client_approve <id_пользователя> [имя_профиля]",
                reply_markup=self._menu_reply_markup(principal),
            )
            return
        try:
            target_user_id = int(parts[0])
        except ValueError:
            await self._send_text(chat_id, "❌ Некорректный ID пользователя.", reply_markup=self._menu_reply_markup(principal))
            return
        profile_name = parts[1].strip() if len(parts) > 1 else ""
        await self._approve_client(chat_id, principal, target_user_id, profile_name=profile_name)
        return

    async def _route_client_reject(self, chat_id, principal, command, arg, profile_name, reply_menu):
        parts = [part for part in arg.split() if part]
        if not parts:
            await self._send_text(
                chat_id,
                "ℹ️ Использование: /client_reject <id_пользователя> [комментарий]",
                reply_markup=self._menu_reply_markup(principal),
            )
            return
        try:
            target_user_id = int(parts[0])
        except ValueError:
            await self._send_text(chat_id, "❌ Некорректный ID пользователя.", reply_markup=self._menu_reply_markup(principal))
            return
        admin_note = " ".join(parts[1:]).strip()
        await self._reject_client(chat_id, principal, target_user_id, admin_note=admin_note)
        return

    async def _route_ai_limits(self, chat_id, principal, command, arg, profile_name, reply_menu):
        users = telegram_access.list_users()
        user_ids = [int(item["user_id"]) for item in users]
        snapshots = telegram_resume_limits.list_user_snapshots(user_ids)
        events = telegram_resume_limits.recent_events(limit=8)
        await self._send_text(
            chat_id,
            build_ai_limits_text(users=users, snapshots=snapshots, events=events),
            reply_markup=self._menu_reply_markup(principal),
        )
        return

    async def _route_ai_grant(self, chat_id, principal, command, arg, profile_name, reply_menu):
        parts = [part for part in arg.split() if part]
        if not parts:
            await self._send_text(
                chat_id,
                "ℹ️ Использование: /ai_grant <id_пользователя> [количество]",
                reply_markup=self._menu_reply_markup(principal),
            )
            return
        try:
            target_user_id = int(parts[0])
        except ValueError:
            await self._send_text(chat_id, "❌ Некорректный ID пользователя.", reply_markup=self._menu_reply_markup(principal))
            return
        try:
            amount = int(parts[1]) if len(parts) > 1 else 1
        except ValueError:
            await self._send_text(chat_id, "❌ Некорректное количество.", reply_markup=self._menu_reply_markup(principal))
            return
        if amount <= 0:
            await self._send_text(chat_id, "❌ Количество должно быть больше нуля.", reply_markup=self._menu_reply_markup(principal))
            return
        snapshot = telegram_resume_limits.grant_bonus(
            target_user_id,
            amount=amount,
            actor_user_id=principal["user_id"],
        )
        await self._send_text(
            chat_id,
            f"✅ Бонус ИИ добавлен пользователю {target_user_id}: +{amount}\n\n{format_ai_snapshot_text(snapshot)}",
            reply_markup=self._menu_reply_markup(principal),
        )
        return

    async def _route_ai_reset(self, chat_id, principal, command, arg, profile_name, reply_menu):
        parts = [part for part in arg.split() if part]
        if not parts:
            await self._send_text(
                chat_id,
                "ℹ️ Использование: /ai_reset <id_пользователя> [новый_бесплатный_лимит]",
                reply_markup=self._menu_reply_markup(principal),
            )
            return
        try:
            target_user_id = int(parts[0])
        except ValueError:
            await self._send_text(chat_id, "❌ Некорректный ID пользователя.", reply_markup=self._menu_reply_markup(principal))
            return
        try:
            free_total = int(parts[1]) if len(parts) > 1 else None
        except ValueError:
            await self._send_text(chat_id, "❌ Некорректный новый бесплатный лимит.", reply_markup=self._menu_reply_markup(principal))
            return
        snapshot = telegram_resume_limits.reset_free_limit(
            target_user_id,
            actor_user_id=principal["user_id"],
            free_total=free_total,
        )
        await self._send_text(
            chat_id,
            f"♻️ Бесплатный лимит сброшен для пользователя {target_user_id}.\n\n{format_ai_snapshot_text(snapshot)}",
            reply_markup=self._menu_reply_markup(principal),
        )
        return

    async def _route_grant(self, chat_id, principal, command, arg, profile_name, reply_menu):
        parts = [part for part in arg.split() if part]
        if len(parts) < 2:
            await self._send_text(
                chat_id,
                "ℹ️ Использование: /grant <id_пользователя> <профиль> [admin|user]",
                reply_markup=self._menu_reply_markup(principal),
            )
            return
        try:
            target_user_id = int(parts[0])
        except ValueError:
            await self._send_text(chat_id, "❌ Некорректный ID пользователя.", reply_markup=self._menu_reply_markup(principal))
            return
        target_profile = parts[1]
        target_role = parts[2].casefold() if len(parts) > 2 else ROLE_USER
        if target_profile not in self._profile_names():
            await self._send_text(chat_id, f"❌ Неизвестный профиль: {target_profile}", reply_markup=self._menu_reply_markup(principal))
            return
        record = telegram_access.upsert_user(target_user_id, profile=target_profile, role=target_role)
        await self._send_text(
            chat_id,
            f"✅ Доступ выдан: {record['user_id']} -> профиль {record['profile']} ({record['role']})",
            reply_markup=self._menu_reply_markup(principal),
        )
        return

    async def _route_revoke(self, chat_id, principal, command, arg, profile_name, reply_menu):
        try:
            target_user_id = int(arg.strip())
        except ValueError:
            await self._send_text(chat_id, "ℹ️ Использование: /revoke <id_пользователя>", reply_markup=self._menu_reply_markup(principal))
            return
        removed = telegram_access.remove_user(target_user_id)
        await self._send_text(
            chat_id,
            f"{'✅ Пользователь удалён' if removed else '⚪️ Пользователь не найден'}: {target_user_id}",
            reply_markup=self._menu_reply_markup(principal),
        )
        return

    async def _route_seen_reset(self, chat_id, principal, command, arg, profile_name, reply_menu):
        role = principal.get("role", ROLE_USER)
        import seen_reset
        from telegram_app import history
        parts = arg.split()
        home = self._profile(profile_name).home_dir
        if parts and parts[0] == "do" and len(parts) != 4:
            await self._send_text(chat_id, "Эта кнопка устарела. Откройте сброс заново.")
            return
        direction_scope = history.scope_arg(parts[2] if len(parts) > 2 else "")
        if len(parts) > 3 and parts[3] != history.token(home):
            await self._send_text(chat_id, "Направление изменилось. Откройте сброс заново.")
            return
        if parts and parts[0] == "view":
            direction_scope = history.scope_arg(parts[1])
            parts = []
        direction_label = history.HistoryScope(home, direction_scope).title
        choice = {"*": "all", "?": "unspecified"}.get(direction_scope, "active")
        binding = history.token(home)
        if self._has_active_command(profile_name):
            await self._send_busy_status(chat_id, principal, profile_name=profile_name)
            return
        if not parts:
            owner_menu = reply_menu_for_command(role, "/seen_reset") or MENU_ADMIN
            self._set_selected_menu(principal["user_id"], owner_menu)
            await self._send_text(chat_id, f"🧹 Сброс просмотренных · {direction_label}.\n"
                                  "Бот забудет пропущенные вакансии и переоценит их в следующем поиске. "
                                  "Отклики, ручные карточки и неопределённые исходы не трогаются.",
                                  reply_markup=self._menu_reply_markup(principal, menu=owner_menu))
            await self._send_text(chat_id, "За какой период?", reply_markup={"inline_keyboard": [[
                {"text": label.capitalize(), "callback_data": f"sr:ask:{scope}:{choice}:{binding}"}
                for scope, label in seen_reset.SCOPES.items()], [
                {"text": label, "callback_data": f"sr:view:{scope}:view:{binding}"}
                for label, scope in (("Это направление", "active"), ("Все направления", "all"), ("Направление не указано", "unspecified"))]]})
            return
        action, scope = (parts + [""])[:2]
        if scope not in seen_reset.SCOPES:
            await self._send_text(chat_id, "Неизвестный период.")
            return
        try:
            if action == "ask":
                count = len(seen_reset.plan(home, scope, direction_scope=direction_scope))
                await self._send_text(chat_id, f"Снять {count} отметок «пропущено» ({seen_reset.SCOPES[scope]}) "
                                      f"для «{direction_label}»?", reply_markup={"inline_keyboard": [[
                                          {"text": "✅ Да, сбросить", "callback_data": f"sr:do:{scope}:{choice}:{binding}"},
                                          {"text": "❌ Нет", "callback_data": "sr:no:"}]]})
            elif action == "do":
                count = seen_reset.apply(home, scope, direction_scope=direction_scope)
                await self._send_text(chat_id, f"✅ Снято {count} отметок ({seen_reset.SCOPES[scope]}). "
                                      "Следующий поиск переоценит эти вакансии. Копия списка сохранена.",
                                      reply_markup=self._menu_reply_markup(principal))
        except (OSError, ValueError) as exc:
            await self._send_text(chat_id, f"❌ {str(str(exc))}")
        return
