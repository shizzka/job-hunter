"""Subprocess lifecycle helpers for the Telegram control bot."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass

import runtime_control


@dataclass
class ActiveCommandState:
    label: str
    profile_name: str
    owner_user_id: int
    started_at: float
    task: asyncio.Task | None = None
    subprocess_pid: int = 0
    cancel_requested: bool = False


class TelegramSubprocessManager:
    """Track active commands and bind captured subprocesses to their state."""

    def __init__(self, *, admin_role: str) -> None:
        self._active_admin_role = admin_role
        self._active_commands: dict[str, ActiveCommandState] = {}

    def _prune_active_commands(self) -> None:
        stale_profiles = [
            profile_name
            for profile_name, command in self._active_commands.items()
            if command.task is not None and command.task.done()
        ]
        for profile_name in stale_profiles:
            self._active_commands.pop(profile_name, None)

    def _all_active_commands(self) -> list[ActiveCommandState]:
        self._prune_active_commands()
        return sorted(
            self._active_commands.values(),
            key=lambda item: (item.started_at, item.profile_name),
        )

    def _active_command(
        self,
        profile_name: str,
    ) -> ActiveCommandState | None:
        self._prune_active_commands()
        return self._active_commands.get(profile_name)

    def _has_active_command(self, profile_name: str | None = None) -> bool:
        if profile_name is None:
            return bool(self._all_active_commands())
        return self._active_command(profile_name) is not None

    def _active_elapsed_sec(
        self,
        command: ActiveCommandState | None,
    ) -> int:
        if not command or command.started_at <= 0:
            return 0
        return max(0, int(time.monotonic() - command.started_at))

    def _can_cancel_active(
        self,
        principal: dict,
        command: ActiveCommandState | None,
    ) -> bool:
        if not command:
            return False
        if principal.get("role") == self._active_admin_role:
            return True
        return int(principal.get("user_id") or 0) == command.owner_user_id

    def _mark_active_command(
        self,
        *,
        principal: dict,
        label: str,
        profile_name: str,
    ) -> ActiveCommandState:
        command = ActiveCommandState(
            label=label,
            profile_name=profile_name,
            owner_user_id=int(principal.get("user_id") or 0),
            started_at=time.monotonic(),
        )
        self._active_commands[profile_name] = command
        self._sync_active_runtime()
        return command

    def _clear_active_command(self, profile_name: str) -> None:
        self._active_commands.pop(profile_name, None)
        self._sync_active_runtime()

    async def _run_active_command_capture(
        self,
        command: ActiveCommandState,
        argv: list[str],
        *,
        timeout: float | None,
    ) -> dict:
        result = await runtime_control.run_command_capture(
            argv,
            timeout=timeout,
            on_start=lambda pid: setattr(command, "subprocess_pid", pid),
        )
        if command.cancel_requested:
            result["cancelled"] = True
        return result

    def _request_active_command_cancel(
        self,
        command: ActiveCommandState,
        *,
        timeout: float = 15.0,
    ) -> dict | None:
        command.cancel_requested = True
        if command.subprocess_pid > 0:
            return runtime_control.stop_pid(
                command.subprocess_pid,
                timeout=timeout,
                process_group=True,
            )
        if command.task:
            command.task.cancel()
        return None
