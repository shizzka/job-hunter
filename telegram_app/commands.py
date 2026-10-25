"""Telegram commands handlers extracted without changing outcomes."""
from __future__ import annotations

import asyncio
import contextlib
import logging

from telegram_app.command_queue import profile_command
from telegram_bot_ui import (
    ROLE_USER,
    build_progress_text,
    format_command_result,
    format_run_summary,
)
import runtime_control
import telegram_resume_limits

log = logging.getLogger("telegram_bot")


class TelegramCommands:
    @profile_command
    async def _start_cli_command(
        self,
        chat_id: int,
        principal: dict,
        profile_name: str,
        flag: str,
        label: str,
        timeout: int,
    ) -> None:
        role = principal.get("role", ROLE_USER)
        reply_markup = self._menu_reply_markup(principal)
        if self._has_active_command(profile_name):
            await self._send_busy_status(chat_id, principal, profile_name=profile_name)
            return

        daemon_state = self._daemon_state(profile_name)
        if daemon_state["running"]:
            await self._send_text(
                chat_id,
                f"🟢 Поиск уже работает для профиля {profile_name}. Сначала остановите его через /daemon_off.",
                reply_markup=reply_markup,
            )
            return

        if flag == "--analyze-resume" and not await self._authorize_ai_usage(
            chat_id, principal, profile_name=profile_name, action="analyze_ai_used", check_only=True,
        ):
            return

        active_command = self._mark_active_command(principal=principal, label=label, profile_name=profile_name)
        progress_message = await self._send_text(
            chat_id,
            build_progress_text(label, profile_name=profile_name),
            reply_markup=self._menu_reply_markup(principal),
        )
        progress_task: asyncio.Task | None = None
        progress_message_id = int((progress_message or {}).get("message_id") or 0)
        if progress_message and label == "analyze resume":
            if progress_message_id > 0:
                progress_task = asyncio.create_task(
                    self._run_progress_indicator(
                        chat_id=chat_id,
                        message_id=progress_message_id,
                        label=label,
                        profile_name=profile_name,
                        reply_markup=self._menu_reply_markup(principal),
                    )
                )

        async def runner() -> None:
            nonlocal progress_task
            try:
                if flag == "--analyze-resume" and not await self._authorize_ai_usage(
                    chat_id, principal, profile_name=profile_name, action="analyze_ai_used",
                ):
                    return
                result = await self._run_active_command_capture(
                    active_command,
                    runtime_control.agent_command_argv(profile_name, flag),
                    timeout=timeout,
                )
                message, send_as_document, document_name, document_content = self._cli_command_result(
                    profile_name, principal, flag, label, result, role)
                if progress_task:
                    progress_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await progress_task
                    progress_task = None
                if progress_message_id > 0:
                    await self._delete_message(chat_id, progress_message_id)
                if send_as_document:
                    await self._send_document(
                        chat_id,
                        filename=document_name,
                        content=document_content,
                        caption=message,
                        reply_markup=reply_markup,
                    )
                else:
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
                log.error("Command %s failed: %s", label, exc, exc_info=True)
                if progress_task:
                    progress_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await progress_task
                    progress_task = None
                if progress_message_id > 0:
                    await self._delete_message(chat_id, progress_message_id)
                await self._send_text(
                    chat_id,
                    f"❌ Команда завершилась с ошибкой: {label}\n{exc}",
                    reply_markup=reply_markup,
                )
            finally:
                if progress_task:
                    progress_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await progress_task
                self._clear_active_command(profile_name)

        active_command.task = self._create_active_task(active_command, runner)

    def _cli_command_result(self, profile_name, principal, flag, label, result, role):
        message = format_command_result(label, result, role=role)
        send_as_document = False
        document_name = ""
        document_content = b""
        if flag == "--analyze-resume" and result.get("ok"):
            telegram_resume_limits.record_resume_analysis(
                principal["user_id"],
                profile_name=profile_name,
                consume_credit=False,
            )
            document_name, document_content = self._analysis_document_payload(
                profile_name,
                (result.get("stdout") or "").strip(),
            )
            send_as_document = bool(document_content)
            message = (
                "✅ ИИ-анализ резюме готов. Отправляю файл анализа."
                if send_as_document
                else "✅ ИИ-анализ резюме готов."
            )
        if flag in {"--search", "--dry-run"}:
            last_run = self._latest_run(profile_name)
            if last_run:
                message = f"{message}\n\n🕓 Последний поиск:\n{format_run_summary(last_run)}"
        return message, send_as_document, document_name, document_content
