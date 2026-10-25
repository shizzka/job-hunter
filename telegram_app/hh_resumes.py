"""Telegram hh resumes handlers extracted without changing outcomes."""
from __future__ import annotations

import asyncio
import contextlib
import logging
import traceback

from telegram_app.command_queue import profile_command
from telegram_bot_ui import (
    MENU_HH,
    ROLE_ADMIN,
    ROLE_USER,
    _client_display_name,
    build_client_hh_auth_inline_markup,
    build_client_status_text,
    build_hh_auth_admin_text,
    build_hh_auth_result_text,
    build_hh_resumes_text,
    build_progress_text,
    build_reply_markup,
    parse_hh_auth_command_result,
)
import client_hh_auth
import runtime_control
import telegram_clients

log = logging.getLogger("telegram_bot")


class TelegramHHResumes:
    def _captcha_pending_for_principal(self, principal: dict) -> tuple[dict | None, str]:
        """Return only the pending captcha that this Telegram user may answer."""
        profile_name = self._selected_profile(principal)
        try:
            import captcha_bridge

            pending = captcha_bridge.peek_pending(profile_name)
        except Exception as exc:
            log.debug("captcha bridge peek failed for %s: %s", profile_name, exc)
            return None, profile_name
        if not pending:
            return None, profile_name
        request_profile = str(pending.get("profile_name") or profile_name).strip()
        if principal.get("role") != ROLE_ADMIN and request_profile != profile_name:
            return None, profile_name
        return pending, request_profile

    async def _request_hh_auth(self, chat_id: int, user_id: int, client: dict, *, reply_markup: dict) -> dict:
        updated = telegram_clients.set_status(
            user_id,
            status=telegram_clients.STATUS_APPROVED,
            auth_status=telegram_clients.AUTH_PENDING_WEB,
        )
        self._append_debug_log(
            "hh_auth_requested",
            target_user_id=user_id,
            profile_name=updated.get("profile_name") or self._default_client_profile_name(user_id),
        )
        await self._send_text(
            chat_id,
            (
                "🔐 Запрос на вход HH отправлен.\n\n"
                "Нужен один тап администратора, чтобы подтвердить запуск.\n"
                "После подтверждения мы сохраним сессию и захватим текущие HH резюме."
            ),
            reply_markup=reply_markup,
        )
        await self._notify_admins(
            "\n".join([
                "🔐 Клиент запросил вход HH",
                "",
                f"• Пользователь Telegram: {user_id}",
                f"• Имя: {_client_display_name(updated)}",
                f"• Профиль: {updated.get('profile_name') or self._default_client_profile_name(user_id)}",
                "",
                "Подтвердите запуск кнопкой ниже или командой:",
                f"• /client_hh_auth {user_id}",
            ]),
            reply_markup=build_client_hh_auth_inline_markup(user_id),
        )
        return updated

    async def _route_search_resume(self, chat_id, principal, command, arg, profile_name, reply_menu):
        self._set_selected_menu(principal["user_id"], reply_menu)
        catalog = client_hh_auth.load_hh_resume_catalog(profile_name)
        if not catalog:
            await self._send_text(
                chat_id,
                "🧾 Пока нет списка резюме HH. Сначала нажмите «Вход HH», затем повторите попытку.",
                reply_markup=self._menu_reply_markup(principal, menu=reply_menu),
            )
            return
        lines = ["🧾 Выберите резюме для поиска. Отправьте его номер:", ""]
        for index, item in enumerate(catalog, start=1):
            title = str(item.get("title") or "Резюме без названия")
            lines.append(f"{index}. {title}")
        lines.append("\nДля отмены отправьте «Отмена».")
        self._set_search_settings_state(principal["user_id"], search_input="resume", search_input_profile=profile_name)
        await self._send_text(
            chat_id,
            "\n".join(lines),
            reply_markup=self._menu_reply_markup(principal, menu=reply_menu),
        )
        return

    async def _route_hh_auth(self, chat_id, principal, command, arg, profile_name, reply_menu):
        role = principal.get("role", ROLE_USER)
        client = self._client_record(principal["user_id"])
        if not client and role == ROLE_ADMIN:
            await self._start_profile_hh_auth_capture(chat_id, principal, profile_name=profile_name)
            return
        if not client:
            await self._send_text(
                chat_id,
                "ℹ️ Вход HH доступен только после заполнения клиентской заявки.",
                reply_markup=self._menu_reply_markup(principal),
            )
            return
        if client.get("status") != telegram_clients.STATUS_APPROVED:
            await self._send_text(
                chat_id,
                "🔒 Вход HH станет доступен после одобрения заявки.",
                reply_markup=self._menu_reply_markup(principal),
            )
            return
        await self._request_hh_auth(
            chat_id,
            principal["user_id"],
            client,
            reply_markup=self._menu_reply_markup(principal),
        )
        return

    async def _route_hh_resumes(self, chat_id, principal, command, arg, profile_name, reply_menu):
        client = self._client_record(principal["user_id"])
        target_profile = profile_name
        if client and client.get("profile_name"):
            target_profile = client["profile_name"]
        from hh.session_probe import probe, save_catalog, OK
        checked = await asyncio.to_thread(probe, self._profile(target_profile).hh.cookies_file)
        note = ""
        if checked["status"] == OK:
            try:
                save_catalog(client_hh_auth.hh_resume_catalog_path(target_profile), checked["resumes"])
            except Exception as exc:
                log.warning("resume catalog cache refresh failed: %s", type(exc).__name__)
        else:
            note = "⚠️ " + checked["message"] + "\nНиже — последний сохранённый список.\n\n"
        resumes = client_hh_auth.load_hh_resume_catalog(target_profile)
        await self._send_text(
            chat_id,
            note + build_hh_resumes_text(target_profile, resumes),
            reply_markup=self._menu_reply_markup(principal),
        )
        return

    @profile_command
    async def _start_profile_hh_auth_capture(
        self,
        chat_id: int,
        principal: dict,
        *,
        profile_name: str,
    ) -> None:
        role = principal.get("role", ROLE_USER)
        reply_markup = self._menu_reply_markup(principal)
        if profile_name not in self._profile_names():
            await self._send_text(chat_id, f"❌ Неизвестный профиль: {profile_name}", reply_markup=reply_markup)
            return
        if self._has_active_command(profile_name):
            await self._send_busy_status(chat_id, principal, profile_name=profile_name)
            return

        label = "hh auth capture"
        active_command = self._mark_active_command(principal=principal, label=label, profile_name=profile_name)
        self._append_debug_log(
            "profile_hh_auth_capture_started",
            admin_user_id=principal.get("user_id"),
            profile_name=profile_name,
            argv=runtime_control.client_hh_auth_command_argv(profile_name, timeout_sec=900),
        )
        progress_message = await self._send_text(
            chat_id,
            "🔐 Запускаю вход HH для выбранного профиля. Бот откроет браузер; если hh.ru попросит телефон или SMS-код, пришли ответ сюда в Telegram.",
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
                command_result = await self._run_active_command_capture(
                    active_command,
                    runtime_control.client_hh_auth_command_argv(profile_name, timeout_sec=900),
                    timeout=1800,
                )
                self._append_debug_log(
                    "profile_hh_auth_capture_command_result",
                    admin_user_id=principal.get("user_id"),
                    profile_name=profile_name,
                    command_result=command_result,
                )
                result = parse_hh_auth_command_result(command_result, profile_name=profile_name)
                self._append_debug_log(
                    "profile_hh_auth_capture_result",
                    admin_user_id=principal.get("user_id"),
                    profile_name=profile_name,
                    result=result,
                )
                if progress_task:
                    progress_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await progress_task
                    progress_task = None
                if progress_message_id > 0:
                    await self._delete_message(chat_id, progress_message_id)
                await self._send_text(
                    chat_id,
                    build_hh_auth_result_text(result),
                    reply_markup=reply_markup,
                )
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
                    build_hh_auth_result_text({"cancelled": True, "profile_name": profile_name}),
                    reply_markup=reply_markup,
                )
            except Exception as exc:
                log.error("Profile HH auth capture failed: %s", exc, exc_info=True)
                self._append_debug_log(
                    "profile_hh_auth_capture_exception",
                    admin_user_id=principal.get("user_id"),
                    profile_name=profile_name,
                    error=str(exc),
                    traceback=traceback.format_exc(),
                )
                if progress_task:
                    progress_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await progress_task
                    progress_task = None
                if progress_message_id > 0:
                    await self._delete_message(chat_id, progress_message_id)
                await self._send_text(
                    chat_id,
                    f"❌ Вход HH завершился с ошибкой:\n{exc}",
                    reply_markup=reply_markup,
                )
            finally:
                self._clear_active_command(profile_name)

        active_command.task = self._create_active_task(active_command, runner)

    @profile_command
    async def _start_hh_auth_capture(self, chat_id: int, principal: dict, client: dict) -> None:
        role = principal.get("role", ROLE_USER)
        reply_markup = self._menu_reply_markup(principal)

        target_user_id = int(client["user_id"])
        profile_name = client.get("profile_name") or self._default_client_profile_name(target_user_id)
        if self._has_active_command(profile_name):
            await self._send_busy_status(chat_id, principal, profile_name=profile_name)
            return
        telegram_clients.set_status(
            target_user_id,
            status=telegram_clients.STATUS_APPROVED,
            auth_status=telegram_clients.AUTH_PENDING_WEB,
            profile_name=profile_name,
        )
        with contextlib.suppress(Exception):
            await self._send_text(
                target_user_id,
                "🔐 Администратор подтвердил вход HH. Запускаем захват сессии и текущих HH резюме.",
                reply_markup=build_reply_markup(ROLE_USER, menu=MENU_HH),
            )
        active_command = self._mark_active_command(principal=principal, label="hh auth capture", profile_name=profile_name)
        self._append_debug_log(
            "hh_auth_capture_started",
            admin_user_id=principal.get("user_id"),
            target_user_id=target_user_id,
            profile_name=profile_name,
            argv=runtime_control.client_hh_auth_command_argv(profile_name, timeout_sec=900),
        )
        progress_message = await self._send_text(
            chat_id,
            build_progress_text("hh auth capture", profile_name=profile_name),
            reply_markup=self._menu_reply_markup(principal),
        )
        progress_task: asyncio.Task | None = None
        progress_message_id = int((progress_message or {}).get("message_id") or 0)
        if progress_message_id > 0:
            progress_task = asyncio.create_task(
                self._run_progress_indicator(
                    chat_id=chat_id,
                    message_id=progress_message_id,
                    label="hh auth capture",
                    profile_name=profile_name,
                    reply_markup=self._menu_reply_markup(principal),
                    interval_sec=5,
                )
            )

        async def runner() -> None:
            nonlocal progress_task
            try:
                command_result = await self._run_active_command_capture(
                    active_command,
                    runtime_control.client_hh_auth_command_argv(profile_name, timeout_sec=900),
                    timeout=1800,
                )
                self._append_debug_log(
                    "hh_auth_capture_command_result",
                    admin_user_id=principal.get("user_id"),
                    target_user_id=target_user_id,
                    profile_name=profile_name,
                    command_result=command_result,
                )
                result = parse_hh_auth_command_result(command_result, profile_name=profile_name)
                self._append_debug_log(
                    "hh_auth_capture_result",
                    admin_user_id=principal.get("user_id"),
                    target_user_id=target_user_id,
                    profile_name=profile_name,
                    result=result,
                )
                telegram_clients.set_status(
                    target_user_id,
                    status=telegram_clients.STATUS_APPROVED,
                    auth_status=(
                        telegram_clients.AUTH_READY
                        if (result.get("ok") or result.get("authenticated")) and not result.get("cancelled")
                        else telegram_clients.AUTH_PENDING_WEB
                    ),
                    profile_name=profile_name,
                )
                if progress_task:
                    progress_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await progress_task
                    progress_task = None
                if progress_message_id > 0:
                    await self._delete_message(chat_id, progress_message_id)
                admin_text = build_hh_auth_admin_text(
                    result,
                    client=telegram_clients.get_client(target_user_id) or client,
                    debug_log_path=self.runtime_paths.bot_debug_log_file,
                )
                await self._send_text(chat_id, admin_text, reply_markup=reply_markup)
                with contextlib.suppress(Exception):
                    await self._send_text(
                        target_user_id,
                        (
                            f"{build_hh_auth_result_text(result)}\n\n"
                            f"{build_client_status_text(telegram_clients.get_client(target_user_id) or client)}"
                        ),
                        reply_markup=build_reply_markup(ROLE_USER, menu=MENU_HH),
                    )
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
                    build_hh_auth_admin_text(
                        {"cancelled": True, "profile_name": profile_name},
                        client=telegram_clients.get_client(target_user_id) or client,
                        debug_log_path=self.runtime_paths.bot_debug_log_file,
                    ),
                    reply_markup=reply_markup,
                )
                with contextlib.suppress(Exception):
                    await self._send_text(
                        target_user_id,
                        build_hh_auth_result_text({"cancelled": True}),
                        reply_markup=build_reply_markup(ROLE_USER, menu=MENU_HH),
                    )
            except Exception as exc:
                log.error("HH auth capture failed: %s", exc, exc_info=True)
                self._append_debug_log(
                    "hh_auth_capture_exception",
                    admin_user_id=principal.get("user_id"),
                    target_user_id=target_user_id,
                    profile_name=profile_name,
                    error=str(exc),
                    traceback=traceback.format_exc(),
                )
                if progress_task:
                    progress_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await progress_task
                    progress_task = None
                if progress_message_id > 0:
                    await self._delete_message(chat_id, progress_message_id)
                await self._send_text(
                    chat_id,
                    (
                        "❌ Вход HH завершился с ошибкой.\n\n"
                        f"• Ошибка: {exc}\n"
                        f"• Журнал отладки: {self.runtime_paths.bot_debug_log_file}"
                    ),
                    reply_markup=reply_markup,
                )
                with contextlib.suppress(Exception):
                    await self._send_text(
                        target_user_id,
                        (
                            "❌ Вход HH завершился с ошибкой.\n\n"
                            "Администратор уже получил журнал отладки и сможет повторить запуск."
                        ),
                        reply_markup=build_reply_markup(ROLE_USER, menu=MENU_HH),
                    )
            finally:
                self._clear_active_command(profile_name)

        active_command.task = self._create_active_task(active_command, runner)
