import asyncio

import telegram_bot
from telegram_app import subprocesses as telegram_subprocesses
from telegram_app.subprocesses import (
    ActiveCommandState,
    TelegramSubprocessManager,
)


class _Task:
    def __init__(self, *, done: bool = False):
        self._done = done
        self.cancelled = False

    def done(self):
        return self._done

    def cancel(self):
        self.cancelled = True


class _Manager(TelegramSubprocessManager):
    def __init__(self):
        self.runtime_syncs = 0
        super().__init__(admin_role="admin")

    def _sync_active_runtime(self):
        self.runtime_syncs += 1


def test_telegram_bot_keeps_active_command_contract():
    bot = telegram_bot.TelegramBot("qa")

    assert isinstance(bot, TelegramSubprocessManager)
    assert telegram_bot.ActiveCommandState is ActiveCommandState
    assert bot._active_commands == {}


def test_manager_tracks_profiles_and_prunes_finished_tasks(monkeypatch):
    timestamps = iter([20.0, 10.0])
    monkeypatch.setattr(
        telegram_subprocesses.time,
        "monotonic",
        lambda: next(timestamps),
    )
    manager = _Manager()
    qa_command = manager._mark_active_command(
        principal={"user_id": 1},
        label="search",
        profile_name="qa",
    )
    client_command = manager._mark_active_command(
        principal={"user_id": 42},
        label="search",
        profile_name="client_42",
    )
    qa_command.task = _Task()
    client_command.task = _Task()

    assert [
        command.profile_name for command in manager._all_active_commands()
    ] == ["client_42", "qa"]

    client_command.task._done = True

    assert manager._active_command("client_42") is None
    assert manager._active_command("qa") is qa_command
    assert manager.runtime_syncs == 2

    manager._clear_active_command("qa")

    assert manager._all_active_commands() == []
    assert manager.runtime_syncs == 3


def test_manager_checks_admin_and_owner_permissions():
    manager = _Manager()
    command = ActiveCommandState(
        label="search",
        profile_name="qa",
        owner_user_id=42,
        started_at=1.0,
    )

    assert manager._can_cancel_active(
        {"user_id": 7, "role": "admin"},
        command,
    )
    assert manager._can_cancel_active(
        {"user_id": 42, "role": "user"},
        command,
    )
    assert not manager._can_cancel_active(
        {"user_id": 7, "role": "user"},
        command,
    )
    assert not manager._can_cancel_active({"role": "admin"}, None)


def test_run_active_command_capture_binds_pid_and_marks_cancellation(
    monkeypatch,
):
    manager = _Manager()
    command = ActiveCommandState(
        label="search",
        profile_name="qa",
        owner_user_id=1,
        started_at=1.0,
    )
    calls = []

    async def fake_run_command_capture(
        argv,
        *,
        cwd=None,
        timeout=None,
        on_start=None,
    ):
        calls.append((argv, cwd, timeout))
        on_start(321)
        command.cancel_requested = True
        return {
            "ok": True,
            "stdout": "done",
            "stderr": "",
            "timeout": False,
        }

    monkeypatch.setattr(
        telegram_subprocesses.runtime_control,
        "run_command_capture",
        fake_run_command_capture,
    )

    result = asyncio.run(
        manager._run_active_command_capture(
            command,
            ["agent.py", "--search"],
            timeout=120,
        )
    )

    assert calls == [(["agent.py", "--search"], None, 120)]
    assert command.subprocess_pid == 321
    assert result == {
        "ok": True,
        "stdout": "done",
        "stderr": "",
        "timeout": False,
        "cancelled": True,
    }


def test_run_active_command_capture_leaves_completed_result_unchanged(
    monkeypatch,
):
    manager = _Manager()
    command = ActiveCommandState(
        label="search",
        profile_name="qa",
        owner_user_id=1,
        started_at=1.0,
    )
    expected = {"ok": True, "stdout": "done", "stderr": ""}

    async def fake_run_command_capture(
        argv,
        *,
        cwd=None,
        timeout=None,
        on_start=None,
    ):
        on_start(654)
        return expected.copy()

    monkeypatch.setattr(
        telegram_subprocesses.runtime_control,
        "run_command_capture",
        fake_run_command_capture,
    )

    result = asyncio.run(
        manager._run_active_command_capture(
            command,
            ["agent.py", "--check"],
            timeout=60,
        )
    )

    assert result == expected
    assert "cancelled" not in result
    assert command.subprocess_pid == 654


def test_request_cancel_stops_process_group(monkeypatch):
    manager = _Manager()
    task = _Task()
    command = ActiveCommandState(
        label="search",
        profile_name="qa",
        owner_user_id=1,
        started_at=1.0,
        task=task,
        subprocess_pid=777,
    )
    calls = []

    def fake_stop_pid(pid, *, timeout, process_group):
        calls.append((pid, timeout, process_group))
        return {"ok": True, "signal": "SIGTERM", "pid": pid}

    monkeypatch.setattr(
        telegram_subprocesses.runtime_control,
        "stop_pid",
        fake_stop_pid,
    )

    result = manager._request_active_command_cancel(command)

    assert result == {"ok": True, "signal": "SIGTERM", "pid": 777}
    assert calls == [(777, 15.0, True)]
    assert command.cancel_requested is True
    assert task.cancelled is False


def test_request_cancel_cancels_task_before_process_starts():
    manager = _Manager()
    task = _Task()
    command = ActiveCommandState(
        label="search",
        profile_name="qa",
        owner_user_id=1,
        started_at=1.0,
        task=task,
    )

    result = manager._request_active_command_cancel(command)

    assert result is None
    assert command.cancel_requested is True
    assert task.cancelled is True
