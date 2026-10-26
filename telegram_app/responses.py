"""Telegram responses handlers extracted without changing outcomes."""
from __future__ import annotations

from datetime import datetime
import asyncio
import contextlib
import json
import logging
import re

from chat_reply_result import parse_reply_result
from direction_views import DirectionView
from state_store.private_journal import append_json
from telegram_app.command_queue import profile_command
from telegram_bot_ui import (
    CALLBACK_CHAT_AI_MANUAL_REPLY,
    CALLBACK_CHAT_AI_REPLY,
    ROLE_ADMIN,
    ROLE_USER,
    _is_menu_button_text,
    build_hh_response_count_text,
    build_progress_text,
    format_command_result,
    reply_menu_for_command,
)
import runtime_control

log = logging.getLogger("telegram_bot")


def _build_chat_ai_candidates_markup(profile_name: str, summary: dict, *, home_dir=None) -> dict | None:
    rows = []
    for idx, item in enumerate(list(summary.get("candidates") or [])[:8], 1):
        chat_id = str(item.get("chat_id") or "")
        message_id = str(item.get("message_id") or "")
        if not chat_id or not message_id:
            continue
        callback_data = _chat_ai_candidate_callback_data(profile_name, item)
        row = []
        if len(callback_data.encode("utf-8")) <= 64:
            row.append({"text": f"🤖 {idx}", "callback_data": callback_data})
        if item.get("google_form_urls"):
            try:
                import google_form_filler as gforms
                form_callback = gforms.google_form_preview_callback_data(profile_name, chat_id, message_id)
            except Exception:
                form_callback = ""
            if form_callback and len(form_callback.encode("utf-8")) <= 64:
                row.append({"text": f"📝 Анкета {idx}", "callback_data": form_callback})
        row.append({"text": f"Открыть {idx}", "url": f"https://chatik.hh.ru/chat/{chat_id}"})
        rows.append(row)
    if home_dir is not None:
        from telegram_app.chat_ownership import ownership_review_row
        for item in list(summary.get("ownership_reviews") or [])[:8]:
            rows.append(ownership_review_row(home_dir, profile_name, item['chat_id'],
                                             item.get('message_id', ''), item.get('title', '')))
    return {"inline_keyboard": rows} if rows else None



def _build_chat_ai_candidates_text(summary: dict) -> str:
    if summary.get("ok") is False:
        url = str(summary.get("url") or "")
        if "account/login" in url:
            return "hh.ru просит заново войти в аккаунт, поэтому список чатов сейчас недоступен. Откройте «⚙️ Настройки» → «🔐 hh.ru и резюме» → «🔐 Вход в hh.ru», обновите сессию и повторите /chat_ai."
        message = str(summary.get("message") or "не удалось открыть chatik")
        return f"Не удалось получить список HH-чатов: {message}"
    candidates = list(summary.get("candidates") or [])
    scanned = int(summary.get("chats_scanned") or 0)
    read = int(summary.get("chats_read") or 0)
    looked = f"просмотрено {read} из {scanned} последних чатов"
    skipped = []
    if summary.get("skipped_closing"):
        skipped.append(f"отказы и «свяжемся» — {summary['skipped_closing']}")
    if summary.get("skipped_other_profile"):
        skipped.append(f"отклики другого профиля — {summary['skipped_other_profile']}")
    if summary.get("skipped_other_direction"):
        skipped.append(f"отклики другого направления — {summary['skipped_other_direction']}")
    skipped_line = ("Пропустил: " + ", ".join(skipped) + ".") if skipped else ""
    heading = f"🤖 Ответ ИИ в чат · {summary['direction_title']}\n\n" if summary.get("direction_title") else ""
    review_line = ("Чат требует вашей проверки: выберите профиль и резюме отклика."
                   if summary.get("ownership_reviews") else "")
    if not candidates:
        return heading + "\n".join(line for line in (
            f"Не нашёл свежих сообщений HR для ответа ({looked}).", skipped_line, review_line) if line)
    lines = [f"Нашёл чатов для ответа: {len(candidates)} ({looked}). Выбери, на какой ответить."]
    if skipped_line:
        lines.append(skipped_line)
    if review_line:
        lines.append(review_line)
    for idx, item in enumerate(candidates, 1):
        title = str(item.get("title") or "—")[:120]
        company = str(item.get("company") or "—")[:80]
        kind = str(item.get("kind_label") or item.get("kind") or "HR")[:40]
        author = str(item.get("author") or "HR")[:80]
        question = str(item.get("question") or "").replace("\n", " ")[:220]
        marker = " 📝 анкета" if item.get("google_form_urls") else ""
        lines.append(f"\n{idx}. [{kind}]{marker} {title} @ {company}\n{author}: {question}")
    return (heading + "\n".join(lines))[:3900]



def _parse_chat_candidates_stdout(stdout: str) -> dict:
    for line in reversed((stdout or "").splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        summary = payload.get("chat_candidates")
        if isinstance(summary, dict):
            return summary
    return {}



def _chat_ai_candidate_callback_data(profile_name: str, candidate: dict) -> str:
    action = CALLBACK_CHAT_AI_MANUAL_REPLY if candidate.get("allow_any") else CALLBACK_CHAT_AI_REPLY
    chat_id = str(candidate.get("chat_id") or "")
    message_id = str(candidate.get("message_id") or "")
    return f"{action}:{_safe_chat_callback_profile(profile_name)}:{chat_id}:{message_id}"



def _safe_chat_callback_profile(profile_name: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", profile_name or "default")



def _parse_manual_chat_ai_arg(raw: str) -> tuple[str, str]:
    text = (raw or "").strip()
    if not text:
        return "", ""

    chat_id = ""
    message_id = ""

    chat_match = re.search(r"(?:chatik\.hh\.ru|hh\.ru)?/chat/(\d{6,})", text, re.I)
    if chat_match:
        chat_id = chat_match.group(1)

    if not chat_id:
        chat_param = re.search(r"(?:chat_id|chatId|chat)=([0-9]{6,})", text, re.I)
        if chat_param:
            chat_id = chat_param.group(1)

    msg_param = re.search(r"(?:message_id|messageId|msg|mid)=([0-9]{6,})", text, re.I)
    if msg_param:
        message_id = msg_param.group(1)

    numbers = re.findall(r"\d{6,}", text)
    if not chat_id and numbers:
        chat_id = numbers[0]
    if not message_id:
        for number in numbers:
            if number != chat_id:
                message_id = number
                break

    return chat_id, message_id



class TelegramResponses:
    def _append_chat_ai_audit_event(self, event: str, **payload: object) -> None:
        path = self.runtime_paths.bot_debug_log_file
        if not path:
            return
        record = {
            "event": event,
            "profile": self.profile_name,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            **payload,
        }
        try:
            append_json(path, record)
        except Exception as exc:
            log.warning("chat AI audit write failed: %s", type(exc).__name__)

    async def _answer_employer_question(self, chat_id: int, principal: dict, profile_name: str, qid: str,
                                        choice: str, text: str | None = None) -> None:
        """A button/message answer to «❓ Работодатель спрашивает»: remember it and retry the application."""
        import question_memory
        if profile_name not in self._profile_names() or (
                principal.get("role") != ROLE_ADMIN and self._selected_profile(principal) != profile_name):
            await self._send_text(chat_id, "Вопрос недоступен.")
            return
        home = self._profile(profile_name).home_dir
        pending = question_memory.peek_pending(home, qid)
        if not pending:
            await self._send_text(chat_id, "Этот вопрос уже отвечен или устарел.")
            return
        if choice == "text" and text is None:
            self._set_search_settings_state(principal["user_id"], question_answer={"profile": profile_name, "qid": qid})
            await self._send_text(chat_id, "✏️ Пришлите ответ одним сообщением — я запомню его для этого вопроса.\n"
                                           f"Вопрос: «{str(pending['question'])}»")
            return
        labels: list[str] = []
        if text is not None:
            answer = " ".join(str(text).split())
        else:
            try:
                answer = pending["options"][int(choice)]
                labels = [answer]
            except (ValueError, IndexError, KeyError):
                await self._send_text(chat_id, "Не понял выбор — нажмите кнопку ещё раз.")
                return
        if not answer:
            await self._send_text(chat_id, "Пустой ответ — пришлите текст.")
            return
        question_memory.remember(home, pending["question"], answer, control=pending.get("control", ""), labels=labels,
                                 direction=pending.get("direction", ""), resume_id=pending.get("resume_id", ""))
        question_memory.pop_pending(home, qid)
        self._clear_search_settings_state(principal["user_id"], "question_answer")
        await self._send_text(chat_id, str(
            f"✅ Запомнил: «{pending['question']}» → «{answer}». Откликаюсь на «{pending.get('vacancy_title') or 'вакансию'}» снова…"))
        await self._start_manual_ai_apply(chat_id, principal, profile_name=profile_name, token=pending["token"])

    async def _accept_employer_question_text(self, chat_id: int, principal: dict, text: str) -> bool:
        waiting = self._search_settings_state(principal["user_id"]).get("question_answer") or {}
        if not waiting or not text or text.startswith("/") or _is_menu_button_text(text):
            return False
        if text.strip().casefold() in {"отмена", "cancel"}:
            self._clear_search_settings_state(principal["user_id"], "question_answer")
            await self._send_text(chat_id, "Ответ отменён. Вопрос остался в чате — можно ответить позже.")
            return True
        await self._answer_employer_question(chat_id, principal, waiting.get("profile", ""), waiting.get("qid", ""),
                                             "text", text=text)
        return True

    async def _route_forms(self, chat_id, principal, command, arg, profile_name, reply_menu):
        await self._send_text(chat_id, "📝 Анкеты", reply_markup=self._menu_reply_markup(principal))
        await self._list_forms(chat_id, principal, profile_name)
        return

    async def _route_hh_responses(self, chat_id, principal, command, arg, profile_name, reply_menu):
        role = principal.get("role", ROLE_USER)
        if reply_menu is None:
            self._set_selected_menu(principal["user_id"], reply_menu_for_command(role, "/hh_responses"))
        target_profile = self._profile(profile_name)
        await self._send_chat_action(chat_id)
        from telegram_app.hh_counter_cache import snapshot as counter_snapshot
        snapshot, counter_age, counter_error = await counter_snapshot(
            self, principal["user_id"], profile_name, target_profile,
            force=arg.strip().lower() in {"refresh", "обновить"})
        if snapshot is None:
            text = counter_error
            if DirectionView(target_profile.home_dir).directions:
                text = (build_hh_response_count_text({}, home_dir=target_profile.home_dir) +
                        "\n\n" + counter_error)
            await self._send_text(chat_id, text, reply_markup=self._menu_reply_markup(principal))
            return

        self._append_debug_log(
            "hh_response_counter_updated",
            profile_name=profile_name,
            active=snapshot.get("active", 0),
            archived=snapshot.get("archived", 0),
            deleted=snapshot.get("deleted", 0),
            total=snapshot.get("total", 0),
        )
        await self._send_text(
            chat_id,
            build_hh_response_count_text(snapshot, home_dir=target_profile.home_dir)
            + f"\n\nобновлено {counter_age} мин назад"
            + (f"\n{counter_error}" if counter_error else "")
            + "\nОбновить: /hh_responses refresh",
            reply_markup=self._menu_reply_markup(principal),
        )
        return

    async def _route_chat_ai(self, chat_id, principal, command, arg, profile_name, reply_menu):
        role = principal.get("role", ROLE_USER)
        if reply_menu is None:
            self._set_selected_menu(principal["user_id"], reply_menu_for_command(role, "/chat_ai"))
        hh_chat_id, hh_message_id = _parse_manual_chat_ai_arg(arg)
        if not hh_chat_id:
            await self._start_chat_ai_candidate_list(
                chat_id,
                principal,
                profile_name=profile_name,
            )
            return
        self._append_chat_ai_audit_event(
            "manual_command",
            action="preview",
            telegram_chat_id=chat_id,
            user_id=int(principal.get("user_id") or 0),
            profile_name=profile_name,
            hh_chat_id=hh_chat_id,
            hh_message_id=hh_message_id,
            allow_any=True,
        )
        await self._start_chat_ai_reply(
            chat_id,
            principal,
            profile_name=profile_name,
            hh_chat_id=hh_chat_id,
            hh_message_id=hh_message_id,
            allow_any=True,
        )
        return

    async def _start_google_form_preview(
        self,
        chat_id: int,
        principal: dict,
        *,
        profile_name: str,
        hh_chat_id: str,
        hh_message_id: str,
    ) -> None:
        await self._start_google_form_command(
            chat_id,
            principal,
            profile_name=profile_name,
            label="google form preview",
            argv=runtime_control.agent_command_argv(
                profile_name,
                "--google-form-preview",
                hh_chat_id,
                "--chat-message-id",
                hh_message_id,
            ),
        )

    async def _start_google_form_submit(
        self,
        chat_id: int,
        principal: dict,
        *,
        profile_name: str,
        token: str,
    ) -> None:
        await self._start_google_form_command(
            chat_id,
            principal,
            profile_name=profile_name,
            label="google form submit",
            argv=runtime_control.agent_command_argv(profile_name, "--google-form-submit", token),
        )

    @profile_command
    async def _start_google_form_command(
        self,
        chat_id: int,
        principal: dict,
        *,
        profile_name: str,
        label: str,
        argv: list[str],
    ) -> None:
        role = principal.get("role", ROLE_USER)
        reply_markup = self._menu_reply_markup(principal)
        if self._has_active_command(profile_name):
            await self._send_busy_status(chat_id, principal, profile_name=profile_name)
            return

        if not await self._authorize_ai_usage(chat_id, principal, profile_name=profile_name, action="form_ai_used",
                                              consume="--google-form-submit" not in argv, check_only=True):
            return

        active_command = self._mark_active_command(principal=principal, label=label, profile_name=profile_name)
        progress_message = await self._send_text(
            chat_id,
            build_progress_text(label, profile_name=profile_name),
            reply_markup=self._menu_reply_markup(principal),
        )
        progress_message_id = int((progress_message or {}).get("message_id") or 0)

        async def runner() -> None:
            try:
                if not await self._authorize_ai_usage(chat_id, principal, profile_name=profile_name,
                                                      action="form_ai_used", consume="--google-form-submit" not in argv):
                    return
                result = await self._run_active_command_capture(
                    active_command,
                    argv,
                    timeout=1800,
                )
                if progress_message_id > 0:
                    await self._delete_message(chat_id, progress_message_id)
                await self._send_text(
                    chat_id,
                    format_command_result(label, result, role=role),
                    reply_markup=reply_markup,
                )
            except asyncio.CancelledError:
                if progress_message_id > 0:
                    await self._delete_message(chat_id, progress_message_id)
                await self._send_text(
                    chat_id,
                    format_command_result(label, {"cancelled": True}, role=role),
                    reply_markup=reply_markup,
                )
            except Exception as exc:
                log.error("Google Form command failed: %s", exc, exc_info=True)
                if progress_message_id > 0:
                    await self._delete_message(chat_id, progress_message_id)
                await self._send_text(
                    chat_id,
                    f"❌ Google Form команда завершилась с ошибкой:\n{exc}",
                    reply_markup=reply_markup,
                )
            finally:
                self._clear_active_command(profile_name)

        active_command.task = self._create_active_task(active_command, runner)

    @profile_command
    async def _start_chat_ai_candidate_list(
        self,
        chat_id: int,
        principal: dict,
        *,
        profile_name: str,
    ) -> None:
        role = principal.get("role", ROLE_USER)
        reply_markup = self._menu_reply_markup(principal)
        if self._has_active_command(profile_name):
            await self._send_busy_status(chat_id, principal, profile_name=profile_name)
            return

        label = "chat AI list"
        active_command = self._mark_active_command(principal=principal, label=label, profile_name=profile_name)
        progress_message = await self._send_text_safely(
            chat_id,
            "Смотрю последние HH-чаты и ищу входящие сообщения для ИИ-предпросмотра…",
            reply_markup=self._menu_reply_markup(principal),
        )
        progress_message_id = int((progress_message or {}).get("message_id") or 0)

        async def runner() -> None:
            try:
                argv = runtime_control.agent_command_argv(
                    profile_name,
                    "--chat-list-candidates",
                    "--chat-list-limit",
                    "8",
                    "--chat-list-max-scan",
                    "25",
                )
                self._append_chat_ai_audit_event(
                    "candidate_list_start",
                    profile_name=profile_name,
                    telegram_chat_id=chat_id,
                    user_id=int(principal.get("user_id") or 0),
                )
                result = await self._run_active_command_capture(
                    active_command,
                    argv,
                    timeout=900,
                )
                if progress_message_id > 0:
                    await self._delete_message(chat_id, progress_message_id)
                if not result.get("ok") or result.get("cancelled"):
                    await self._send_text_safely(
                        chat_id,
                        format_command_result(label, result, role=role),
                        reply_markup=reply_markup,
                    )
                    return
                summary = _parse_chat_candidates_stdout(result.get("stdout") or "")
                self._append_chat_ai_audit_event(
                    "candidate_list_result",
                    profile_name=profile_name,
                    telegram_chat_id=chat_id,
                    ok=bool(summary),
                    candidates=len(summary.get("candidates") or []) if summary else 0,
                    chats_scanned=summary.get("chats_scanned") if summary else None,
                    chats_read=summary.get("chats_read") if summary else None,
                    read_failures=summary.get("read_failures") if summary else None,
                    stderr=(result.get("stderr") or "")[-1200:],
                )
                if not summary:
                    await self._send_text_safely(
                        chat_id,
                        "Не смог разобрать список HH-чатов. Посмотри /chat_log или /log.",
                        reply_markup=reply_markup,
                    )
                    return
                # The user may have switched direction while the browser was reading.
                home = self._profile(profile_name).home_dir
                view = DirectionView(home)
                candidates, reviews = [], []
                for item in list(summary.get("candidates") or []) + list(summary.get("ownership_reviews") or []):
                    status = view.chat_status(item.get("chat_id"))
                    if status == "selected":
                        candidates.append(item)
                    elif status == "review":
                        reviews.append(item)
                summary.update(candidates=candidates, ownership_reviews=reviews, direction_title=view.title)
                await self._send_text_safely(
                    chat_id,
                    _build_chat_ai_candidates_text(summary),
                    reply_markup=_build_chat_ai_candidates_markup(profile_name, summary, home_dir=home) or reply_markup,
                )
            except asyncio.CancelledError:
                if progress_message_id > 0:
                    await self._delete_message(chat_id, progress_message_id)
                await self._send_text_safely(
                    chat_id,
                    format_command_result(label, {"cancelled": True}, role=role),
                    reply_markup=reply_markup,
                )
            except Exception as exc:
                self._append_chat_ai_audit_event(
                    "candidate_list_exception",
                    profile_name=profile_name,
                    telegram_chat_id=chat_id,
                    error=str(exc),
                )
                log.error("Chat AI candidate list failed: %s", exc, exc_info=True)
                if progress_message_id > 0:
                    await self._delete_message(chat_id, progress_message_id)
                await self._send_text_safely(
                    chat_id,
                    f"❌ Список HH-чатов для ИИ-ответа завершился с ошибкой:\n{exc}",
                    reply_markup=reply_markup,
                )
            finally:
                self._clear_active_command(profile_name)

        active_command.task = self._create_active_task(active_command, runner)

    @profile_command
    async def _start_chat_ai_reply(
        self,
        chat_id: int,
        principal: dict,
        *,
        profile_name: str,
        hh_chat_id: str,
        hh_message_id: str,
        force_send: bool = False,
        allow_any: bool = False,
        alternative: bool = False,
    ) -> None:
        role = principal.get("role", ROLE_USER)
        reply_markup = self._menu_reply_markup(principal)
        if self._has_active_command(profile_name):
            await self._send_busy_status(chat_id, principal, profile_name=profile_name)
            return

        label = "chat AI send" if force_send else "chat AI alternative" if alternative else "chat AI reply"
        if not await self._authorize_ai_usage(chat_id, principal, profile_name=profile_name, action="chat_ai_used",
                                              consume=not (force_send and re.fullmatch(r"\d+~[0-9a-f]{12}", hh_message_id)),
                                              check_only=True):
            return

        active_command = self._mark_active_command(principal=principal, label=label, profile_name=profile_name)
        progress_message = await self._send_text_safely(
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
            switch_to = restore_to = None
            import directions as directions_mod
            home = self._profile(profile_name).home_dir
            try:
                # B13: answer with the facts of the resume this application was sent with.
                import chat_context
                owner = await _chat_reply_owner(
                    self, chat_id, principal, profile_name, hh_chat_id, hh_message_id, reply_markup, home)
                if owner is None:
                    return
                current = directions_mod.active_direction(home)
                if owner.status == chat_context.OWN and owner.direction and owner.direction != (current or {}).get("name"):
                    switch_to, restore_to = owner.direction, (current or {}).get("name")
                    directions_mod.set_active_direction(home, switch_to)
                if not await self._authorize_ai_usage(
                    chat_id, principal, profile_name=profile_name, action="chat_ai_used",
                    consume=not (force_send and re.fullmatch(r"\d+~[0-9a-f]{12}", hh_message_id)),
                ):
                    return
                argv = runtime_control.agent_command_argv(
                    profile_name,
                    "--chat-respond-one",
                    hh_chat_id,
                    "--chat-message-id",
                    hh_message_id,
                    "--chat-allow-suspicious",
                )
                if allow_any:
                    argv.append("--chat-allow-any")
                if force_send:
                    argv.append("--chat-force-send")
                if alternative:
                    argv.append("--chat-alternative")
                self._append_chat_ai_audit_event(
                    "command_start",
                    action="send" if force_send else "preview",
                    profile_name=profile_name,
                    hh_chat_id=hh_chat_id,
                    hh_message_id=hh_message_id,
                    force_send=force_send,
                    allow_any=allow_any,
                    alternative=alternative,
                )
                result = await self._run_active_command_capture(
                    active_command,
                    argv,
                    timeout=1200,
                )
                self._append_chat_ai_audit_event(
                    "command_result",
                    action="send" if force_send else "preview",
                    profile_name=profile_name,
                    hh_chat_id=hh_chat_id,
                    hh_message_id=hh_message_id,
                    force_send=force_send,
                    allow_any=allow_any,
                    alternative=alternative,
                    ok=bool(result.get("ok")),
                    returncode=result.get("returncode"),
                    timeout=bool(result.get("timeout")),
                    cancelled=bool(result.get("cancelled")),
                    stdout=(result.get("stdout") or "")[-2000:],
                    stderr=(result.get("stderr") or "")[-1200:],
                )
                message = format_command_result(label, result, role=role)
                outcome = parse_reply_result(result.get('stdout'))
                result_markup = reply_markup
                if (not force_send and outcome and not outcome.get('ok')
                        and outcome.get('retryable')):
                    action = 'chat_ai_any' if allow_any else 'chat_ai'
                    callback = f'{action}:{profile_name}:{hh_chat_id}:{hh_message_id.partition("~")[0]}'
                    if len(callback.encode('utf-8')) <= 64:
                        result_markup = {'inline_keyboard': [[{'text': 'Повторить', 'callback_data': callback}]]}
                if progress_task:
                    progress_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await progress_task
                    progress_task = None
                if progress_message_id > 0:
                    await self._delete_message(chat_id, progress_message_id)
                try:
                    sent_message = await self._send_text(chat_id, message, reply_markup=result_markup)
                    self._append_chat_ai_audit_event(
                        "delivery_ok",
                        action="send" if force_send else "preview",
                        profile_name=profile_name,
                        hh_chat_id=hh_chat_id,
                        hh_message_id=hh_message_id,
                        force_send=force_send,
                        allow_any=allow_any,
                        alternative=alternative,
                        telegram_message_id=int((sent_message or {}).get("message_id") or 0),
                    )
                except Exception as exc:
                    self._append_chat_ai_audit_event(
                        "delivery_failed",
                        action="send" if force_send else "preview",
                        profile_name=profile_name,
                        hh_chat_id=hh_chat_id,
                        hh_message_id=hh_message_id,
                        force_send=force_send,
                        allow_any=allow_any,
                        alternative=alternative,
                        cli_ok=bool(result.get("ok")),
                        error=str(exc),
                    )
                    log.warning("Telegram delivery failed for Chat AI reply: %s", exc)
            except asyncio.CancelledError:
                self._append_chat_ai_audit_event(
                    "command_cancelled",
                    action="send" if force_send else "preview",
                    profile_name=profile_name,
                    hh_chat_id=hh_chat_id,
                    hh_message_id=hh_message_id,
                    force_send=force_send,
                    allow_any=allow_any,
                    alternative=alternative,
                )
                if progress_task:
                    progress_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await progress_task
                    progress_task = None
                if progress_message_id > 0:
                    await self._delete_message(chat_id, progress_message_id)
                await self._send_text_safely(
                    chat_id,
                    format_command_result(label, {"cancelled": True}, role=role),
                    reply_markup=reply_markup,
                )
            except Exception as exc:
                self._append_chat_ai_audit_event(
                    "command_exception",
                    action="send" if force_send else "preview",
                    profile_name=profile_name,
                    hh_chat_id=hh_chat_id,
                    hh_message_id=hh_message_id,
                    force_send=force_send,
                    allow_any=allow_any,
                    alternative=alternative,
                    error=str(exc),
                )
                log.error("Chat AI reply failed: %s", exc, exc_info=True)
                if progress_task:
                    progress_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await progress_task
                    progress_task = None
                if progress_message_id > 0:
                    await self._delete_message(chat_id, progress_message_id)
                await self._send_text_safely(
                    chat_id,
                    f"❌ Ответ ИИ в чат завершился с ошибкой:\n{exc}",
                    reply_markup=reply_markup,
                )
            finally:
                if switch_to:
                    with contextlib.suppress(Exception):
                        # Restore only if nobody changed the direction meanwhile.
                        if (directions_mod.active_direction(home) or {}).get("name") == switch_to:
                            directions_mod.set_active_direction(home, restore_to)
                if progress_task:
                    progress_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await progress_task
                self._clear_active_command(profile_name)

        active_command.task = self._create_active_task(active_command, runner)


async def _chat_reply_owner(self, chat_id, principal, profile_name, hh_chat_id, hh_message_id, reply_markup, home):
    import chat_context
    owner = chat_context.safe_resolve(home, hh_chat_id)
    if owner.status == chat_context.OTHER:
        markup = reply_markup
        target = owner.owner_profile
        if (target and target in self._profile_names()
                and (principal.get("role") == ROLE_ADMIN or self._selected_profile(principal) == target)):
            callback = f"cop:{target}:{hh_chat_id}:{hh_message_id.partition('~')[0]}"
            if len(callback.encode("utf-8")) <= 64:
                # One tap: switch to the owning profile and answer this very chat there.
                markup = {"inline_keyboard": [[{"text": f"▶ Ответить из профиля {target}",
                                                "callback_data": callback}]]}
        await self._send_text(chat_id, chat_context.refusal_text(owner, profile_name),
                              reply_markup=markup)
        return
    if owner.status != chat_context.OWN or not owner.resume_id:
        import chat_owner_review
        token = chat_owner_review.create_review(
            home, hh_chat_id, hh_message_id.partition('~')[0], profile_name)
        callback = f"cbo:{profile_name}:{token}"
        markup = reply_markup
        if len(callback.encode("utf-8")) <= 64:
            markup = {"inline_keyboard": [[{"text": "Выбрать владельца отклика",
                                           "callback_data": callback}]]}
        reason = (chat_context.CONFLICT_TEXT if owner.status == chat_context.CONFLICT
                  else chat_context.UNKNOWN_TEXT)
        await self._send_text(chat_id, reason, reply_markup=markup)
        return
    return owner
