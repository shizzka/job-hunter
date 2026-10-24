"""Queue admission and replay contracts, without Telegram or HH network calls."""
import asyncio
import threading
from types import SimpleNamespace

import pytest

from telegram_app import command_queue as queue
from telegram_app.callbacks import TelegramCallbackRouter


class Bot(queue.TelegramCommandQueue):
    def __init__(self):
        self._init_command_queue()
        self._active_commands = {}
        self.messages = []
        self.edits = []
        self.answers = []
        self.started = []
        self.guard = ""

    def _active_command(self, profile):
        return self._active_commands.get(profile)

    def _queue_guard_reason(self, profile):
        return self.guard

    def _profile_recipient_ids(self, profile):
        return []

    def _profile_names(self):
        return ["qa", "other"]

    def _selected_profile(self, principal):
        return principal.get("profile", "qa")

    async def _send_text(self, chat_id, text, **kwargs):
        self.messages.append((chat_id, text, kwargs))
        entry = queue.current_entry()
        if entry:
            entry.last_message = text

    async def _edit_reply_markup(self, chat_id, message_id, reply_markup=None):
        self.edits.append((chat_id, message_id, reply_markup))

    async def _answer_callback_query(self, callback_id, text="", **kwargs):
        self.answers.append((callback_id, text, kwargs))

    def finish(self, profile="qa", outcome="завершена"):
        active = self._active_commands.pop(profile)
        if getattr(active, "entry", None):
            active.entry.outcome = outcome
        self._schedule_queue(profile)


def source(token="one"):
    return {"id": token, "data": f"manual_apply:qa:{token}",
            "from": {"id": 1}, "message": {"message_id": 7,
            "chat": {"id": 42, "type": "private"},
            "reply_markup": {"inline_keyboard": [[{"text": "Apply", "callback_data": f"manual_apply:qa:{token}"}]]}}}


def entry(bot, label="manual AI apply", *, profile="qa", key="", user_id=1,
          callback=None, refuse=False, started=None):
    async def invoke(revalidate):
        bot.started.append((profile, label, revalidate))
        if started is not None:
            started.append(label)
        if refuse:
            await bot._send_text(42, "Отказ: подтверждение отозвано")
            return
        bot._active_commands[profile] = SimpleNamespace(label=label, task=None, entry=item)
    item = queue.QueueEntry(profile, 42, user_id, label, invoke, key, source=callback)
    return item


async def settle(bot):
    for _ in range(30):
        tasks = list(bot._queue_drainers.values())
        if not tasks:
            return
        await asyncio.gather(*tasks)
    raise AssertionError("Queue drainer did not settle")


def test_head_removed_while_guard_worker_waits_does_not_consume_next_entry(monkeypatch):
    async def scenario():
        bot = Bot()
        first, second = entry(bot, label='first'), entry(bot, label='second')
        first.ready = second.ready = True
        bot._command_queues['qa'] = [first, second]
        entered, finish = threading.Event(), threading.Event()
        def guard(profile):
            entered.set()
            assert finish.wait(2)
            return ''
        monkeypatch.setattr(bot, '_queue_guard_reason', guard)
        bot._schedule_queue('qa')
        try:
            await spin_until(entered.is_set)
            bot._command_queues['qa'].remove(first)
        finally:
            finish.set()
        await settle(bot)
        assert bot.started == [('qa', 'second', True)]
        assert not bot._command_queues['qa']
    asyncio.run(scenario())


def test_fifo_and_profile_independence_preserve_manual_duplicates():
    async def scenario():
        bot = Bot()
        first = entry(bot)
        second = entry(bot)
        third = entry(bot)
        assert await bot._admit_command(first)
        assert await bot._admit_command(second)
        assert await bot._admit_command(third)
        assert bot._command_queues["qa"] == [second, third]
        assert await bot._admit_command(entry(bot, profile="other"))
        assert set(bot._active_commands) == {"qa", "other"}
        bot.finish()
        await settle(bot)
        assert bot._active_commands["qa"].entry is second
        bot.finish()
        await settle(bot)
        assert bot._active_commands["qa"].entry is third
        assert [call[2] for call in bot.started] == [False, False, True, True]
        bot.finish()
        bot.finish("other")
        await settle(bot)
        assert any("задач: 2" in text for _, text, _ in bot.messages)
    asyncio.run(scenario())


@pytest.mark.parametrize("state", ["running", "waiting", "launching"])
@pytest.mark.parametrize("label", ["search", "check invitations", "dry-run", "digest"])
def test_periodic_coalesces_running_waiting_and_launching(state, label):
    async def scenario():
        bot = Bot()
        periodic = entry(bot, label, key=label)
        if state == "running":
            await bot._admit_command(periodic)
        elif state == "waiting":
            await bot._admit_command(entry(bot))
            await bot._admit_command(periodic)
        else:
            bot._queue_launching["qa"] = periodic
        before = bot._queue_length("qa")
        assert not await bot._admit_command(entry(bot, label, key=label))
        assert bot._queue_length("qa") == before
        assert "уже идёт/в очереди" in bot.messages[-1][1]
        await bot._shutdown_command_queue()
    asyncio.run(scenario())


def test_ttl_expiry_restores_original_callback_and_does_not_run(monkeypatch):
    monkeypatch.setenv("BOT_COMMAND_QUEUE_TTL_MINUTES", "1")
    async def scenario():
        bot = Bot()
        await bot._admit_command(entry(bot))
        waiting = entry(bot, callback=source())
        await bot._admit_command(waiting)
        waiting.created_at -= 61
        bot.finish()
        await settle(bot)
        assert waiting.outcome == "снята по TTL"
        assert len(bot.started) == 1
        assert bot.edits[-1] == (42, 7, source()["message"]["reply_markup"])
        assert any("TTL" in text for _, text, _ in bot.messages)
    asyncio.run(scenario())


def test_full_queue_keeps_rejected_callback_button(monkeypatch):
    monkeypatch.setenv("BOT_COMMAND_QUEUE_MAX", "1")
    async def scenario():
        bot = Bot()
        await bot._admit_command(entry(bot))
        accepted = entry(bot, callback=source("accepted"))
        assert await bot._admit_command(accepted)
        before = list(bot.edits)
        rejected = entry(bot, callback=source("rejected"))
        assert not await bot._admit_command(rejected)
        assert bot.edits == before
        assert bot._command_queues["qa"] == [accepted]
        assert "полна" in bot.messages[-1][1]
        await bot._shutdown_command_queue()
    asyncio.run(scenario())


@pytest.mark.parametrize("actor,allowed", [(1, True), (2, False), (99, True)])
def test_remove_requires_owner_or_admin_and_restores_button(monkeypatch, actor, allowed):
    monkeypatch.setattr(queue.telegram_access, "resolve_user", lambda uid: {
        "user_id": uid, "role": "admin" if uid == 99 else "user", "profile": "qa"})
    async def scenario():
        bot = Bot()
        await bot._admit_command(entry(bot))
        waiting = entry(bot, callback=source())
        await bot._admit_command(waiting)
        await bot._queue_callback({"id": "cancel", "from": {"id": actor},
                                   "data": f"queue:remove:{waiting.id}"})
        assert (waiting not in bot._command_queues["qa"]) == allowed
        if allowed:
            assert waiting.outcome == "удалена пользователем"
            assert bot.edits[-1][2] == source()["message"]["reply_markup"]
        else:
            assert bot.answers[-1][2]["show_alert"]
        await bot._shutdown_command_queue()
    asyncio.run(scenario())


def test_guard_pause_notifies_once_and_explicit_resume_rechecks_guard(monkeypatch):
    monkeypatch.setattr(queue.telegram_access, "resolve_user", lambda uid: {
        "user_id": uid, "role": "user", "profile": "qa"})
    async def scenario():
        bot = Bot()
        await bot._admit_command(entry(bot))
        waiting = entry(bot)
        await bot._admit_command(waiting)
        bot.guard = "captcha"
        bot.finish()
        await settle(bot)
        assert "qa" in bot._queue_paused
        assert len(bot.started) == 1
        bot._schedule_queue("qa")
        await settle(bot)
        assert sum("на паузе" in text for _, text, _ in bot.messages) == 1
        assert not any("Очередь выполнена" in text for _, text, _ in bot.messages)
        callback = {"id": "resume", "from": {"id": 1}, "data": f"queue:resume:{waiting.id}"}
        await bot._queue_callback(callback)
        assert "qa" in bot._queue_paused
        assert bot.answers[-1][2]["show_alert"]
        bot.guard = ""
        await bot._queue_callback(callback)
        await settle(bot)
        assert "qa" not in bot._queue_paused
        assert bot._active_commands["qa"].entry is waiting
        bot.finish()
        await settle(bot)
    asyncio.run(scenario())


def test_shutdown_restores_waiters_notifies_reset_and_rejects_new_work():
    async def scenario():
        bot = Bot()
        await bot._admit_command(entry(bot))
        await bot._admit_command(entry(bot, callback=source()))
        await bot._shutdown_command_queue()
        assert not bot._command_queues
        assert bot.edits[-1][2] == source()["message"]["reply_markup"]
        assert any("сброшена при перезапуске" in text for _, text, _ in bot.messages)
        assert not await bot._admit_command(entry(bot))
        assert len(bot.started) == 1
        assert not any("Очередь выполнена" in text for _, text, _ in bot.messages)
    asyncio.run(scenario())


def test_summary_includes_success_refusal_and_ttl(monkeypatch):
    monkeypatch.setenv("BOT_COMMAND_QUEUE_TTL_MINUTES", "1")
    async def scenario():
        bot = Bot()
        await bot._admit_command(entry(bot, "blocker"))
        success = entry(bot, "send")
        refusal = entry(bot, "revoked", refuse=True, callback=source())
        expired = entry(bot, "old")
        for item in (success, refusal, expired):
            await bot._admit_command(item)
        expired.created_at -= 61
        bot.finish()
        await settle(bot)
        bot.finish(outcome="отправлена")
        await settle(bot)
        summaries = [text for _, text, _ in bot.messages if "Очередь выполнена" in text]
        assert len(summaries) == 1
        assert "send: отправлена" in summaries[0]
        assert "revoked: пропуск: Отказ: подтверждение отозвано" in summaries[0]
        assert "old: снята по TTL" in summaries[0]
        assert "задач: 3" in summaries[0]
        assert bot.edits[-1][2] == source()["message"]["reply_markup"]
    asyncio.run(scenario())


class CallbackBot(Bot):
    _handle_callback_query = TelegramCallbackRouter._handle_callback_query

    async def _show_uncertain_manual_apply(self, *args):
        return False  # this queue-only fake has no persisted application cards

    def __init__(self):
        super().__init__()
        self.state = {}
        self.valid_approval = True
        self.manual_calls = []

    def _update_state(self, mutate):
        mutate(self.state)
        return self.state

    async def _start_manual_ai_apply(self, chat_id, principal, *, profile_name, token):
        self.manual_calls.append((principal["user_id"], profile_name, token, self.valid_approval))
        if not self.valid_approval:
            await self._send_text(chat_id, "Подтверждение отозвано")
            return
        self._active_commands[profile_name] = SimpleNamespace(
            label="manual AI apply", task=None, entry=queue.current_entry())


def test_original_manual_callback_only_deletes_button_after_admission_and_replays_checks(monkeypatch):
    monkeypatch.setenv("BOT_COMMAND_QUEUE_MAX", "1")
    monkeypatch.setattr(queue.telegram_access, "resolve_user", lambda uid: {
        "user_id": uid, "role": "user", "profile": "qa"})
    async def scenario():
        bot = CallbackBot()
        await bot._admit_command(entry(bot))
        assert await bot._handle_callback_query(source("accepted"))
        assert not bot.manual_calls
        assert bot.edits[-1][2] is None
        before = list(bot.edits)
        assert not await bot._handle_callback_query(source("full"))
        assert bot.edits == before
        bot.valid_approval = False
        bot.finish()
        await settle(bot)
        assert bot.manual_calls == [(1, "qa", "accepted", False)]
        assert not bot._active_commands
        assert bot.edits[-1][2] == source("accepted")["message"]["reply_markup"]
    asyncio.run(scenario())


def test_queued_callback_revalidates_current_access_before_replay(monkeypatch):
    principals = {1: {"user_id": 1, "role": "user", "profile": "qa"}}
    monkeypatch.setattr(queue.telegram_access, "resolve_user", principals.get)
    async def scenario():
        bot = CallbackBot()
        await bot._admit_command(entry(bot))
        await bot._handle_callback_query(source())
        principals.clear()
        bot.finish()
        await settle(bot)
        assert not bot.manual_calls
        assert not bot._active_commands
        assert any("доступ изменились" in text for _, text, _ in bot.messages)
        assert bot.edits[-1][2] == source()["message"]["reply_markup"]
    asyncio.run(scenario())


def real_bot(monkeypatch, tmp_path):
    import telegram_bot
    import captcha_bridge
    monkeypatch.setattr(telegram_bot.manual_apply_queue, "_queue_path", lambda profile_name=None:
                        tmp_path / "manual_apply_queue.json")
    monkeypatch.setattr(captcha_bridge, "peek_pending", lambda **kwargs: None)
    bot = telegram_bot.TelegramBot("qa")
    messages, edits = [], []
    profile = SimpleNamespace(home_dir=str(tmp_path),
        run_history_file=str(tmp_path / "runs.jsonl"),
        analytics_events_file=str(tmp_path / "events.jsonl"),
        log_file=str(tmp_path / "hunter.log"), error_log_file=str(tmp_path / "error.log"))
    monkeypatch.setattr(bot, "_profile", lambda name: profile)
    monkeypatch.setattr(bot, "_profile_names", lambda: ["qa"])
    monkeypatch.setattr(bot, "_profile_recipient_ids", lambda name: [])
    monkeypatch.setattr(bot, "_selected_profile", lambda principal: principal.get("profile", "qa"))
    monkeypatch.setattr(bot, "_menu_reply_markup", lambda *args, **kwargs: {})
    monkeypatch.setattr(bot, "_sync_active_runtime", lambda: None)
    monkeypatch.setattr(bot, "_append_debug_log", lambda *args, **kwargs: None)
    monkeypatch.setattr(queue.telegram_access, "resolve_user", lambda uid: {
        "user_id": uid, "role": "admin", "profile": "qa"})

    async def api(method, payload, **kwargs):
        if method == "sendMessage":
            messages.append(payload)
        elif method == "editMessageReplyMarkup":
            edits.append(payload)
        return {}
    monkeypatch.setattr(bot, "_api_request", api)
    return bot, profile, messages, edits


async def spin_until(predicate):
    # Run completion now includes an auditor thread and durable journal writes;
    # count elapsed time, not event-loop turns that can outrun that thread.
    deadline = asyncio.get_running_loop().time() + 5
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("Expected asynchronous transition did not occur")


def test_real_bot_duplicate_manual_callbacks_run_sequentially_and_report_each_chat(monkeypatch, tmp_path):
    import runtime_control
    async def scenario():
        bot, _, messages, _ = real_bot(monkeypatch, tmp_path)
        first_release, second_release = asyncio.Event(), asyncio.Event()
        calls, running = [], 0

        async def capture(argv, *, timeout, on_start):
            nonlocal running
            running += 1
            assert running == 1, "Two jobs overlapped for the same profile"
            calls.append(list(argv))
            index = len(calls)
            on_start(100 + index)
            try:
                await (first_release if index == 1 else second_release).wait()
                return {"ok": index == 1, "returncode": 0 if index == 1 else 1,
                        "stdout": "Подтверждение отозвано" if index == 2 else "Отклик отправлен, подтверждено сервером hh.",
                        "stderr": "", "timeout": False}
            finally:
                running -= 1
        monkeypatch.setattr(runtime_control, "run_command_capture", capture)
        blocker = bot._mark_active_command(principal={"user_id": 1}, label="blocker", profile_name="qa")
        blocker.task = SimpleNamespace(done=lambda: False)
        first = source("same-token")
        second = source("same-token")
        second["message"]["chat"]["id"] = 84
        second["from"]["id"] = 2
        assert await bot._handle_callback_query(first)
        assert await bot._handle_callback_query(second)
        assert bot._queue_length("qa") == 2
        assert not calls
        bot._clear_active_command("qa")
        await spin_until(lambda: len(calls) == 1)
        assert bot._queue_length("qa") == 1
        assert any(p["chat_id"] == 42 and "manual AI apply" in p["text"] for p in messages)
        first_release.set()
        await spin_until(lambda: len(calls) == 2)
        assert any(p["chat_id"] == 84 and "manual AI apply" in p["text"] for p in messages)
        second_release.set()
        await spin_until(lambda: not bot._active_commands and not bot._queue_drainers)
        assert calls[0] == calls[1]
        assert "--manual-apply-token" in calls[0]
        assert calls[0][-1] == "same-token"
        summaries = [p for p in messages if "Очередь выполнена" in p["text"]]
        assert {p["chat_id"] for p in summaries} == {42, 84}
        assert "Подтверждение отозвано" in next(p["text"] for p in summaries if p["chat_id"] == 84)
        assert "отправлено, подтверждено сервером hh" in next(p["text"] for p in summaries if p["chat_id"] == 42)
        await bot._shutdown_command_queue()
    asyncio.run(scenario())


def test_real_capture_zero_exit_new_auth_log_pauses_following_job(monkeypatch, tmp_path):
    import runtime_control
    async def scenario():
        bot, profile, messages, _ = real_bot(monkeypatch, tmp_path)
        release = asyncio.Event()
        async def capture(argv, *, timeout, on_start):
            on_start(101)
            await release.wait()
            with open(profile.log_file, "a", encoding="utf-8") as stream:
                stream.write("ERROR Не залогинен!\n")
            return {"ok": True, "returncode": 0, "stdout": "", "stderr": ""}
        monkeypatch.setattr(runtime_control, "run_command_capture", capture)
        principal = {"user_id": 1, "role": "admin", "profile": "qa"}
        await bot._start_manual_ai_apply(42, principal, profile_name="qa", token="one")
        await asyncio.sleep(0)
        await bot._start_manual_ai_apply(42, principal, profile_name="qa", token="two")
        release.set()
        await spin_until(lambda: "qa" in bot._queue_paused)
        assert "не залогинен" in bot._queue_paused["qa"]
        assert bot._queue_length("qa") == 1
        assert not any("Очередь выполнена" in p["text"] for p in messages)
        await bot._shutdown_command_queue()
    asyncio.run(scenario())


def test_capture_evidence_ignores_stale_history_and_reads_new_hard_stop(monkeypatch, tmp_path):
    import json
    from telegram_app.subprocesses import ActiveCommandState
    bot, profile, _, _ = real_bot(monkeypatch, tmp_path)
    history = tmp_path / "runs.jsonl"
    history.write_text(json.dumps({"hh_recovery": {"stopped": True, "reason": "old auth failure"}}) + "\n")
    command = ActiveCommandState("search", "qa", 1, 0, result={"ok": True})
    snapshot = bot._queue_capture_evidence("qa")
    bot._queue_capture_finished(command, snapshot)
    assert command.pause_reason == ""
    with history.open("a") as stream:
        stream.write(json.dumps({"hh_recovery": {"stopped": True, "reason": "auth_session_lost"}}) + "\n")
    bot._queue_capture_finished(command, snapshot)
    assert command.pause_reason == "auth_session_lost"


@pytest.mark.parametrize("corrupt", [False, True])
def test_actual_guard_reads_only_requested_profile_and_fails_closed(monkeypatch, tmp_path, corrupt):
    import json
    from datetime import datetime, timedelta, timezone
    bot, _, _, _ = real_bot(monkeypatch, tmp_path)
    home = tmp_path / "hh_guard_state.json"
    home.write_text("broken-json" if corrupt else json.dumps({
        "blocked_until": (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat(),
        "last_reason": "profile captcha", "successful_apply_timestamps": []}))
    assert bot._queue_guard_reason("qa") == (
        "Состояние HH-guard недоступно" if corrupt else "profile captcha")


def test_authorization_await_reserves_profile_before_second_manual_arrival(monkeypatch, tmp_path):
    async def scenario():
        bot, _, _, _ = real_bot(monkeypatch, tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        async def authorize(*args, **kwargs):
            entered.set()
            await release.wait()
            return False
        monkeypatch.setattr(bot, "_authorize_ai_usage", authorize)
        principal = {"user_id": 1, "role": "admin", "profile": "qa"}
        first = asyncio.create_task(bot._start_chat_ai_reply(
            42, principal, profile_name="qa", hh_chat_id="123", hh_message_id="456"))
        await entered.wait()
        assert "qa" in bot._queue_launching
        assert not bot._active_commands
        assert await bot._start_manual_ai_apply(42, principal, profile_name="qa", token="manual")
        assert bot._queue_length("qa") == 1
        assert not bot._active_commands
        await bot._shutdown_command_queue()
        release.set()
        await first
    asyncio.run(scenario())


def test_auth_recovery_reservation_during_delivery_keeps_manual_waiting(monkeypatch, tmp_path):
    async def scenario():
        bot, _, _, _ = real_bot(monkeypatch, tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        # Recoveries reserve the profile before their first asynchronous notification.
        original_send = bot._send_text
        async def send(chat_id, text, **kwargs):
            if "Запускаю вход HH" in text:
                entered.set()
                await release.wait()
            return await original_send(chat_id, text, **kwargs)
        monkeypatch.setattr(bot, "_send_text", send)
        principal = {"user_id": 1, "role": "admin", "profile": "qa"}
        recovery = asyncio.create_task(bot._start_profile_hh_auth_capture(42, principal, profile_name="qa"))
        await entered.wait()
        assert bot._has_active_command("qa")
        assert await bot._start_manual_ai_apply(42, principal, profile_name="qa", token="manual")
        assert bot._queue_length("qa") == 1
        assert bot._active_commands["qa"].label == "hh auth capture"
        # Cancel before the browser runner is created; no subprocess can start.
        recovery.cancel()
        with pytest.raises(asyncio.CancelledError):
            await recovery
        await bot._shutdown_command_queue()
    asyncio.run(scenario())


@pytest.mark.parametrize("kind", ["manual", "suggest", "auth"])
def test_shutdown_during_progress_delivery_prevents_runner_start(monkeypatch, tmp_path, kind):
    import runtime_control
    import telegram_bot
    async def scenario():
        bot, _, _, _ = real_bot(monkeypatch, tmp_path)
        entered, release = asyncio.Event(), asyncio.Event()
        calls = []
        async def forbidden_capture(*args, **kwargs):
            calls.append("subprocess")
            raise AssertionError("Subprocess started after shutdown")
        async def forbidden_suggest(*args, **kwargs):
            calls.append("suggest")
            raise AssertionError("AI task started after shutdown")
        async def authorized(*args, **kwargs):
            return True
        original_api = bot._api_request
        async def blocked_api(method, payload, **kwargs):
            if method == "sendMessage" and not entered.is_set():
                entered.set()
                await release.wait()
            return await original_api(method, payload, **kwargs)
        monkeypatch.setattr(bot, "_api_request", blocked_api)
        monkeypatch.setattr(bot, "_authorize_ai_usage", authorized)
        monkeypatch.setattr(runtime_control, "run_command_capture", forbidden_capture)
        monkeypatch.setattr(telegram_bot.search_query_suggester, "suggest_queries", forbidden_suggest)
        principal = {"user_id": 1, "role": "admin", "profile": "qa"}
        if kind == "manual":
            invocation = bot._start_manual_ai_apply(42, principal, profile_name="qa", token="manual")
        elif kind == "auth":
            invocation = bot._start_profile_hh_auth_capture(42, principal, profile_name="qa")
        else:
            invocation = bot._start_search_query_suggestion(42, principal, profile_name="qa")
        launch = asyncio.create_task(invocation)
        await asyncio.wait_for(entered.wait(), timeout=2)
        await bot._shutdown_command_queue()
        release.set()
        await launch
        await asyncio.sleep(0)
        assert calls == []
        assert not bot._active_commands
        assert not bot._queue_launching
        assert not bot._queue_drainers
    asyncio.run(scenario())


def test_paused_resume_button_survives_removal_of_original_head(monkeypatch):
    monkeypatch.setattr(queue.telegram_access, "resolve_user", lambda uid: {
        "user_id": uid, "role": "user", "profile": "qa"})
    async def scenario():
        bot = Bot()
        await bot._admit_command(entry(bot))
        first, second = entry(bot), entry(bot)
        await bot._admit_command(first)
        await bot._admit_command(second)
        bot.guard = "captcha"
        bot.finish()
        await settle(bot)
        paused_notice = next(kwargs for _, text, kwargs in bot.messages if "на паузе" in text)
        resume = paused_notice["reply_markup"]["inline_keyboard"][0][0]["callback_data"]
        await bot._queue_callback({"id": "remove", "from": {"id": 1}, "data": f"queue:remove:{first.id}"})
        bot.guard = ""
        await bot._queue_callback({"id": "resume", "from": {"id": 1}, "data": resume})
        await settle(bot)
        assert bot._active_commands["qa"].entry is second
        bot.finish()
        await settle(bot)
    asyncio.run(scenario())


def test_pruning_finished_task_wakes_waiting_profile(monkeypatch, tmp_path):
    import runtime_control
    async def scenario():
        bot, _, _, _ = real_bot(monkeypatch, tmp_path)
        done = False
        blocker = bot._mark_active_command(principal={"user_id": 1}, label="blocker", profile_name="qa")
        blocker.task = SimpleNamespace(done=lambda: done)
        await bot._handle_callback_query(source())
        calls = []
        async def capture(argv, *, timeout, on_start):
            calls.append(argv)
            return {"ok": True, "stdout": "done", "stderr": ""}
        monkeypatch.setattr(runtime_control, "run_command_capture", capture)
        done = True
        assert bot._active_command("qa") is None
        await spin_until(lambda: bool(calls) and not bot._active_commands and not bot._queue_drainers)
        assert len(calls) == 1
    asyncio.run(scenario())


def test_second_queued_manual_token_is_not_admitted_again(monkeypatch, tmp_path):
    import runtime_control
    async def scenario():
        bot, _, messages, _ = real_bot(monkeypatch, tmp_path)
        blocker = bot._mark_active_command(principal={"user_id": 1}, label="blocker", profile_name="qa")
        blocker.task = SimpleNamespace(done=lambda: False)
        await bot._handle_callback_query(source("same-token"))
        await bot._handle_callback_query(source("same-token"))
        attempts = []
        async def capture(argv, *, timeout, on_start):
            attempts.append(argv[-1])
            return {"ok": True, "stdout": "Отклик отправлен, подтверждено сервером hh.", "stderr": ""}
        monkeypatch.setattr(runtime_control, "run_command_capture", capture)
        bot._clear_active_command("qa")
        await spin_until(lambda: len(attempts) == 1 and not bot._active_commands and not bot._queue_drainers)
        assert attempts == ["same-token"]
        summary = next(payload["text"] for payload in messages if "Очередь выполнена" in payload["text"])
        assert "отправлено, подтверждено сервером hh" in summary
        assert "задач: 1" in summary
    asyncio.run(scenario())


def test_queued_candidate_applied_before_drain_is_revalidated_before_hh(monkeypatch, tmp_path):
    import agent
    import config
    import runtime_control
    import manual_apply_queue
    from unittest.mock import Mock
    candidate = {"status": "pending", "allow_ai_apply": True, "vacancy": {"source": "hh"}}
    monkeypatch.setattr(config, "HH_APPLICATION_MODE", "auto")
    monkeypatch.setattr(manual_apply_queue, "get_candidate", lambda token, **kwargs: dict(candidate))
    monkeypatch.setattr(agent.company_blacklist, "is_blocked", lambda company: False)
    hh = Mock(side_effect=AssertionError("Applied candidate must be refused before opening HH"))
    monkeypatch.setattr(agent, "HHClient", hh)

    async def scenario():
        bot, _, messages, _ = real_bot(monkeypatch, tmp_path)
        blocker = bot._mark_active_command(principal={"user_id": 1}, label="blocker", profile_name="qa")
        blocker.task = SimpleNamespace(done=lambda: False)
        await bot._handle_callback_query(source("applied-before-drain"))
        assert bot._queue_length("qa") == 1
        candidate["status"] = "applied"
        attempts = []
        async def capture(argv, *, timeout, on_start):
            attempts.append(argv[-1])
            result = await agent.do_manual_apply_token(argv[-1])
            assert result == {"ok": False, "message": "Отклик для этой карточки недоступен"}
            return {"ok": False, "stdout": "", "stderr": "", "returncode": 1}
        monkeypatch.setattr(runtime_control, "run_command_capture", capture)
        bot._clear_active_command("qa")
        await spin_until(lambda: attempts and not bot._active_commands and not bot._queue_drainers)
        assert attempts == ["applied-before-drain"]
        hh.assert_not_called()
        summary = next(payload["text"] for payload in messages if "Очередь выполнена" in payload["text"])
        assert "статус карточки: applied" in summary
    asyncio.run(scenario())


def test_arrival_between_release_and_drain_cannot_overtake_waiter():
    async def scenario():
        bot = Bot()
        await bot._admit_command(entry(bot, "A"))
        older = entry(bot, "B")
        await bot._admit_command(older)
        bot.finish()
        newer = entry(bot, "C")
        await bot._admit_command(newer)
        await settle(bot)
        assert bot._active_commands["qa"].entry is older
        assert bot._command_queues["qa"] == [newer]
        bot.finish()
        await settle(bot)
        assert bot._active_commands["qa"].entry is newer
        bot.finish()
        await settle(bot)
    asyncio.run(scenario())


def ai_invocation(bot, principal, kind):
    if kind in {"analyze", "search_suggest"}:
        return bot._dispatch(42, principal, "/" + kind, "")
    if kind == "chat":
        return bot._start_chat_ai_reply(42, principal, profile_name="qa", hh_chat_id="123", hh_message_id="456")
    return bot._start_google_form_command(42, principal, profile_name="qa", label="google form preview",
                                          argv=["synthetic", "--google-form-preview"])


@pytest.mark.parametrize("kind", ["analyze", "search_suggest", "chat", "form"])
@pytest.mark.parametrize("finish", ["launch", "ttl", "remove", "shutdown", "exhausted", "access_changed",
                                    "shutdown_during_progress", "exhausted_during_progress"])
def test_queued_ai_charges_only_when_runner_starts(monkeypatch, tmp_path, kind, finish):
    import config
    import runtime_control
    import telegram_bot
    import telegram_resume_limits as limits

    if kind == "chat":
        from tests.chat_ownership_fakes import owned_telegram_chats
        owned_telegram_chats(tmp_path, ["123"])

    monkeypatch.setattr(config, "TELEGRAM_AI_LIMITS_FILE", str(tmp_path / "limits.json"))
    limits.reset_free_limit(1, free_total=1)
    principal = {"user_id": 1, "role": "user", "profile": "qa"}
    access = {1: principal}
    before = limits.get_user_snapshot(1)

    async def scenario():
        bot, profile, messages, _ = real_bot(monkeypatch, tmp_path)
        profile.resume_file = str(tmp_path / "resume.md")
        profile.hh = SimpleNamespace(search_queries=[])
        monkeypatch.setattr(queue.telegram_access, "resolve_user", access.get)
        monkeypatch.setattr(bot, "_daemon_state", lambda name: {"running": False})
        monkeypatch.setattr(bot, "_client_record", lambda uid: {})
        monkeypatch.setattr(bot, "_set_search_settings_state", lambda *a, **kw: None)
        calls = []

        async def work(*args, **kwargs):
            assert limits.get_user_snapshot(1)["free_used"] == 1
            calls.append(kind)
            return [] if kind == "search_suggest" else {"ok": False, "stdout": "synthetic result"}
        monkeypatch.setattr(runtime_control, "run_command_capture", work)
        monkeypatch.setattr(telegram_bot.search_query_suggester, "suggest_queries", work)
        blocker = bot._mark_active_command(principal=principal, label="busy", profile_name="qa")
        blocker.task = SimpleNamespace(done=lambda: False)
        assert await ai_invocation(bot, principal, kind)
        assert bot._queue_length("qa") == 1
        assert bot._active_commands["qa"] is blocker
        assert not calls
        assert limits.get_user_snapshot(1) == before
        queued = bot._command_queues["qa"][0]
        if finish == "ttl":
            queued.created_at -= 121 * 60
        elif finish == "remove":
            await bot._queue_callback({"id": "remove", "from": {"id": 1}, "data": f"queue:remove:{queued.id}"})
        elif finish == "shutdown":
            await bot._shutdown_command_queue()
        elif finish == "exhausted":
            limits.reset_free_limit(1, free_total=0)
        elif finish == "access_changed":
            access.clear()

        entered, release = asyncio.Event(), asyncio.Event()
        if finish.endswith("_during_progress"):
            original_api = bot._api_request
            async def blocked_api(method, payload, **kwargs):
                if method == "sendMessage" and not entered.is_set():
                    entered.set()
                    await release.wait()
                return await original_api(method, payload, **kwargs)
            monkeypatch.setattr(bot, "_api_request", blocked_api)
        bot._clear_active_command("qa")
        if finish.endswith("_during_progress"):
            await asyncio.wait_for(entered.wait(), timeout=1)
            assert limits.get_user_snapshot(1) == before
            if finish == "shutdown_during_progress":
                # Shutdown cancels the drainer while progress delivery is pending.
                await bot._shutdown_command_queue()
            else:
                limits.reset_free_limit(1, free_total=0)
            release.set()
        await spin_until(lambda: not bot._active_commands and not bot._queue_drainers)
        assert calls == ([kind] if finish == "launch" else [])
        assert limits.get_user_snapshot(1)["free_used"] == (1 if finish == "launch" else 0)
        usage = [event for event in limits.recent_events(100) if event["action"].endswith("ai_used")]
        assert len(usage) == (1 if finish == "launch" else 0)
        if finish.startswith("exhausted"):
            assert any("Лимит ИИ исчерпан" in payload["text"] for payload in messages)
        await bot._shutdown_command_queue()
    asyncio.run(scenario())


def test_healthy_hh_run_clears_a_stale_queue_stop(monkeypatch, tmp_path):
    """19.10: a 14:22 login glitch paused the queue at 15:37 although a 15:27 search had worked."""
    import json
    from telegram_app.subprocesses import ActiveCommandState
    bot, profile, _, _ = real_bot(monkeypatch, tmp_path)
    history = tmp_path / "runs.jsonl"
    history.write_text("")
    monkeypatch.setattr(bot, "_schedule_queue", lambda profile: None)
    bot._queue_stop_reasons["qa"] = "HH authentication was not proven"
    command = ActiveCommandState("search", "qa", 1, 0, result={"ok": True})
    snapshot = bot._queue_capture_evidence("qa")
    with history.open("a") as stream:
        stream.write(json.dumps({"kind": "search", "status": "finished", "finished_at": "2026-10-19T15:35:49",
                                 "source_stats": {"hh": {"fetched": 500, "stop_reason": None}}}) + "\n")
    bot._queue_capture_finished(command, snapshot)
    bot._queue_command_finished("qa", command)
    assert command.pause_reason == "" and "qa" not in bot._queue_stop_reasons


def test_failed_hh_run_keeps_the_queue_stopped(monkeypatch, tmp_path):
    import json
    from telegram_app.subprocesses import ActiveCommandState
    bot, profile, _, _ = real_bot(monkeypatch, tmp_path)
    history = tmp_path / "runs.jsonl"
    history.write_text("")
    monkeypatch.setattr(bot, "_schedule_queue", lambda profile: None)
    command = ActiveCommandState("search", "qa", 1, 0, result={"ok": True})
    snapshot = bot._queue_capture_evidence("qa")
    with history.open("a") as stream:
        stream.write(json.dumps({"kind": "search", "status": "finished", "finished_at": "2026-10-19T14:22:10",
                                 "source_stats": {"hh": {"stop_reason": "HH authentication was not proven"}}}) + "\n")
    bot._queue_capture_finished(command, snapshot)
    bot._queue_command_finished("qa", command)
    assert bot._queue_stop_reasons["qa"] == "HH authentication was not proven"


@pytest.mark.parametrize('cancel_stage', ['markup', 'notice', 'callback'])
def test_timed_out_admission_does_not_block_following_commands(monkeypatch, cancel_stage):
    from telegram_bot import TelegramBot

    async def scenario():
        bot = Bot()
        await bot._admit_command(entry(bot, 'active'))
        incomplete = entry(bot, 'incomplete', callback=source())
        survivor = entry(bot, 'survivor')
        entered = asyncio.Event()
        original = getattr(bot, {'markup': '_queue_markup', 'notice': '_queue_notice',
                                 'callback': '_answer_callback_query'}[cancel_stage])

        async def blocked(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()

        attribute = {'markup': '_queue_markup', 'notice': '_queue_notice',
                     'callback': '_answer_callback_query'}[cancel_stage]
        monkeypatch.setattr(bot, attribute, blocked)
        supervisor = TelegramBot('qa')
        supervisor.UPDATE_TIMEOUT_SECONDS = 0.04
        supervisor.SLOW_UPDATE_SECONDS = 0.01
        admission = asyncio.create_task(supervisor._run_foreground(bot._admit_command, incomplete))
        await asyncio.wait_for(entered.wait(), timeout=2)
        monkeypatch.setattr(bot, attribute, original)
        await bot._admit_command(survivor)
        bot.finish()
        # Completion cannot drain past incomplete.ready=False yet.
        await settle(bot)
        await admission
        await settle(bot)
        assert incomplete not in bot._command_queues['qa']
        assert incomplete not in bot._queue_history['qa']
        assert bot._active_commands['qa'].entry is survivor
        assert all(label != 'incomplete' for _, label, _ in bot.started)

    asyncio.run(scenario())


def test_paused_queue_admission_says_why_and_offers_resume(monkeypatch, tmp_path):
    """24.10 e2e: after an HHUnexpectedUI pause a new apply answered «В очереди №2 после «пауза»» and waited
    unnoticed. It must say the queue is paused, why in plain words, and offer «Продолжить очередь» right there."""
    async def scenario():
        bot, profile, messages, _ = real_bot(monkeypatch, tmp_path)
        bot._queue_paused["qa"] = "HHUnexpectedUI при ручном отклике"
        principal = {"user_id": 1, "role": "admin", "profile": "qa"}
        await bot._start_manual_ai_apply(42, principal, profile_name="qa", token="one")
        notice = next(p for p in messages if "Очередь на паузе" in p["text"])
        assert "непривычное окно" in notice["text"] and "HHUnexpectedUI" not in notice["text"]
        assert "после «пауза»" not in notice["text"]
        buttons = [b for row in notice["reply_markup"]["inline_keyboard"] for b in row]
        resume = next(b for b in buttons if b["text"] == "Продолжить очередь")
        token = resume["callback_data"].split(":")[-1]
        assert bot._queue_resume_profiles[token] == "qa"
        assert bot._queue_resume_token("qa") == token  # one token per paused profile
        await bot._shutdown_command_queue()
    asyncio.run(scenario())


def test_pause_reasons_are_plain_words():
    assert "HHUnexpectedUI" not in queue.human_pause_reason("HHUnexpectedUI")
    assert queue.human_pause_reason("Не залогинен") == "Не залогинен"
