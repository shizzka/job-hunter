"""Search launch callbacks bind the reviewed choices to one user's profile."""
from __future__ import annotations


def stop_token(profile_name, active):
    import hashlib
    return hashlib.sha256(f"{profile_name}:{active.started_at}".encode()).hexdigest()[:12]


async def handle_query_draft(self, context):
    import profile_access
    import telegram_access
    from telegram_app import history
    parts = str(context.raw_data).split(":")

    def current(principal):
        if not principal or len(parts) != 3 or parts[2] not in {"save", "edit"}:
            return False
        state = self._search_settings_state(context.user_id)
        profile = self._selected_profile(principal)
        return ((context.message.get("chat") or {}).get("type") == "private"
                and state.get("search_query_draft") and parts[1] == state.get("search_query_draft_token")
                and profile == state.get("search_query_draft_profile")
                and state.get("search_query_draft_binding") == history.token(self._profile(profile).home_dir))

    error = "Черновик изменился или недоступен. Запросите предложение заново."
    if not current(context.principal):
        await self._answer_callback_query(context.callback_id, error)
        return
    await self._answer_callback_query(context.callback_id)
    # The acknowledgement yields: a new draft, binding or revocation must win.
    fresh = telegram_access.resolve_user(context.user_id)
    with profile_access.principal_scope(fresh):
        if not current(fresh):
            await self._send_text(context.chat_id, error)
            return
        self._clear_search_settings_state(context.user_id, "search_query_draft_token")
        if parts[2] == "edit":
            self._clear_search_settings_state(context.user_id, "search_query_draft")
        await self._dispatch(context.chat_id, fresh,
                             "/search_use_draft" if parts[2] == "save" else "/search_edit", "")


async def handle_search(self, context):
    parts = str(context.raw_data).split(":")
    if parts == ["fs", "open"]:
        if (context.message.get("chat") or {}).get("type") != "private":
            await self._answer_callback_query(context.callback_id, "Откройте личный чат с ботом.")
            return
        await self._answer_callback_query(context.callback_id)
        await self._show_find_launch(context.chat_id, context.principal, self._selected_profile(context.principal))
        return
    if len(parts) == 3 and parts[1] == "stop":
        profile = self._selected_profile(context.principal)
        active = self._active_command(profile) if context.can_access_profile(profile) else None
        if (not active or stop_token(profile, active) != parts[2]
                or not self._can_cancel_active(context.principal, active)
                or (context.message.get("chat") or {}).get("type") != "private"):
            await self._answer_callback_query(context.callback_id, "Этот поиск уже завершён или недоступен.")
            return
        await self._answer_callback_query(context.callback_id, "Останавливаю поиск")
        await self._cancel_active_command(context.chat_id, context.principal, profile_name=profile)
        return
    state = self._search_settings_state(context.user_id).get("find_launch") or {}
    valid = (len(parts) in {3, 4} and parts[1] in {"pick", "go", "dry"}
             and parts[2] == state.get("token")
             and state.get("profile") == self._selected_profile(context.principal)
             and context.can_access_profile(state.get("profile", ""))
             and (context.message.get("chat") or {}).get("type") == "private")
    if not valid or not self._find_launch_current(state):
        await self._answer_callback_query(context.callback_id, "Эта кнопка устарела. Откройте «Найти вакансии» заново.")
        return
    if parts[1] == "pick":
        if len(parts) != 4 or not parts[3].isdigit():
            await self._answer_callback_query(context.callback_id, "Поиск недоступен.")
            return
        index = int(parts[3])
        choices = state.get("choices") or []
        if index > len(choices):
            await self._answer_callback_query(context.callback_id, "Поиск недоступен.")
            return
        await self._answer_callback_query(context.callback_id)
        selected = choices if index == len(choices) else [choices[index]]
        await self._show_find_launch(context.chat_id, context.principal, state["profile"], selected=selected)
        return
    if len(parts) != 3 or "selected" not in state or parts[1] == "dry" and not state.get("auto"):
        await self._answer_callback_query(context.callback_id, "Запуск недоступен.")
        return
    # Consume before awaiting: even two different copies of this keyboard cannot
    # start the same approved launch twice. The durable callback guard also claims it.
    self._clear_search_settings_state(context.user_id, "find_launch")
    await self._answer_callback_query(context.callback_id, "Начинаю поиск")
    dry = parts[1] == "dry"
    await self._start_cli_command(context.chat_id, context.principal, state["profile"],
                                 "--dry-run" if dry else "--search", "dry-run" if dry else "search", 3600,
                                 search_directions=state["selected"], search_snapshot=state["snapshot"])
