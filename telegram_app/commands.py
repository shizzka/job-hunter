"""Telegram commands handlers extracted without changing outcomes."""
from __future__ import annotations

import asyncio
import contextlib
import logging

from telegram_app.command_queue import profile_command
from telegram_bot_ui import (
    ROLE_USER,
    build_active_stop_markup,
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
        *,
        search_directions: list[str] | None = None,
        search_snapshot: str = "",
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

        previous_run_id = None
        if flag in {"--search", "--dry-run"}:
            from telegram_app.guards import read_thread
            previous_run = await read_thread(self._latest_run, profile_name)
            previous_run_id = (previous_run or {}).get("run_id")
        active_command = self._mark_active_command(principal=principal, label=label, profile_name=profile_name)
        progress_markup = (build_active_stop_markup(profile_name, active_command)
                           if flag in {"--search", "--dry-run"} else self._menu_reply_markup(principal))
        progress_message = await self._send_text(
            chat_id,
            build_progress_text(label, profile_name=profile_name),
            reply_markup=progress_markup,
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
                if search_directions is None:
                    result = await self._run_active_command_capture(
                        active_command,
                        runtime_control.agent_command_argv(profile_name, flag),
                        timeout=timeout,
                    )
                else:
                    result = await self._run_selected_searches(
                        active_command, profile_name, flag, timeout, search_directions, search_snapshot)
                message, send_as_document, document_name, document_content = self._cli_command_result(
                    profile_name, principal, flag, label, result, role)
                completion_markup = reply_markup
                if flag in {"--search", "--dry-run"} and result.get("ok"):
                    from telegram_app.guards import read_thread
                    completion_markup = (await read_thread(
                        self._empty_search_completion_markup, profile_name, previous_run_id)) or reply_markup
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
                    await self._send_text(chat_id, message, reply_markup=completion_markup)
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

    def _empty_search_completion_markup(self, profile_name, previous_run_id):
        from telegram_app import history
        profile = self._profile(profile_name)
        summary = history.search_summary(profile, profile_name)
        displayed_run = self._latest_run(profile_name) or {}
        if (not summary or summary.get("run_id") == previous_run_id
                or summary.get("run_id") != displayed_run.get("run_id")):
            return None
        return history.empty_search_markup(profile.home_dir, summary)

    async def _run_selected_searches(self, active_command, profile_name, flag, timeout, selected, snapshot):
        import directions
        if flag not in {"--search", "--dry-run"} or snapshot != self._find_snapshot(profile_name):
            return {"ok": False, "stdout": "Настройки поиска изменились. Откройте «Найти вакансии» заново."}
        home = self._profile(profile_name).home_dir
        original = directions.active_direction(home)
        original_name = (original or {}).get("name")
        results = []
        for name in selected or [None]:
            if snapshot != self._find_snapshot(profile_name):
                results.append({"ok": False, "stdout": "Настройки поиска изменились. Оставшиеся поиски отменены."})
                break
            switched = name is not None and name != original_name
            if switched:
                directions.set_active_direction(home, name)
            try:
                result = await self._run_active_command_capture(
                    active_command, runtime_control.agent_command_argv(profile_name, flag), timeout=timeout)
                results.append(result)
            finally:
                if switched and (directions.active_direction(home) or {}).get("name") == name:
                    directions.set_active_direction(home, original_name)
            if not result.get("ok") or active_command.cancel_requested:
                break
            if directions.active_direction(home) != original:
                break
        return {**results[-1], "ok": all(item.get("ok") for item in results),
                "stdout": "\n".join(item.get("stdout") or "" for item in results)}

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
                message = f"{message}\n\n🕓 Последний поиск:\n{format_run_summary(last_run, role=role)}"
        return message, send_as_document, document_name, document_content
