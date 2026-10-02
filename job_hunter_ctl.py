#!/usr/bin/env python3
"""CLI helpers to start/stop Job Hunter background processes."""
from __future__ import annotations

import argparse
import sys

import config
import profile as profile_mod
import runtime_control


def _runtime_pid() -> int:
    runtime = runtime_control.read_json_file(config.RUNTIME_STATUS_FILE) or {}
    pid = runtime.get("pid")
    return int(pid) if isinstance(pid, int) else 0


def _bot_runtime_pid() -> int:
    runtime = runtime_control.read_json_file(config.TELEGRAM_BOT_RUNTIME_FILE) or {}
    pid = runtime.get("pid")
    return int(pid) if isinstance(pid, int) else 0


def _print_process_status(label: str, state: dict) -> None:
    status = "running" if state.get("running") else "stopped"
    print(f"{label}: {status} (pid={state.get('pid', 0)})")


def main() -> int:
    parser = argparse.ArgumentParser(description="Job Hunter process control")
    parser.add_argument(
        "--profile",
        default="default",
        help="Имя профиля (default = из env vars; иначе из ~/.job-hunter/profiles/<name>/profile.env)",
    )
    parser.add_argument(
        "command",
        choices=("daemon-start", "daemon-stop", "daemon-status", "bot-start", "bot-stop", "bot-status", "status"),
    )
    args = parser.parse_args()

    profile_mod.activate_no_lock(args.profile)

    daemon_state = runtime_control.describe_process(
        config.DAEMON_PID_FILE,
        expected_tokens=runtime_control.AGENT_DAEMON_TOKENS,
        fallback_pid=_runtime_pid(),
    )
    bot_state = runtime_control.describe_process(
        config.TELEGRAM_BOT_PID_FILE,
        expected_tokens=runtime_control.BOT_TOKENS,
        fallback_pid=_bot_runtime_pid(),
    )

    if args.command == "daemon-start":
        result = runtime_control.start_background_process(
            runtime_control.agent_command_argv(args.profile, "--daemon"),
            pid_file=config.DAEMON_PID_FILE,
            log_file=config.LOG_FILE,
            expected_tokens=runtime_control.AGENT_DAEMON_TOKENS,
            fallback_pid=daemon_state.get("pid") or 0,
        )
        if result["already_running"]:
            print(f"Daemon already running: pid={result['pid']}")
            return 0
        if not result["ok"]:
            print(f"Failed to start daemon. Log: {result['log_file']}")
            return 1
        print(f"Daemon started: pid={result['pid']}")
        print(f"Log: {result['log_file']}")
        return 0

    if args.command == "daemon-stop":
        result = runtime_control.stop_process(
            config.DAEMON_PID_FILE,
            expected_tokens=runtime_control.AGENT_DAEMON_TOKENS,
            fallback_pid=daemon_state.get("pid") or 0,
        )
        if result.get("already_stopped"):
            print("Daemon already stopped")
            return 0
        print(f"Daemon stopped: pid={result['pid']} via {result.get('signal', 'SIGTERM')}")
        return 0 if result["ok"] else 1

    if args.command == "bot-start":
        result = runtime_control.start_background_process(
            runtime_control.bot_command_argv(args.profile),
            pid_file=config.TELEGRAM_BOT_PID_FILE,
            log_file=config.TELEGRAM_BOT_LOG_FILE,
            expected_tokens=runtime_control.BOT_TOKENS,
            fallback_pid=bot_state.get("pid") or 0,
        )
        if result["already_running"]:
            print(f"Telegram bot already running: pid={result['pid']}")
            return 0
        if not result["ok"]:
            print(f"Failed to start Telegram bot. Log: {result['log_file']}")
            return 1
        print(f"Telegram bot started: pid={result['pid']}")
        print(f"Log: {result['log_file']}")
        return 0

    if args.command == "bot-stop":
        result = runtime_control.stop_process(
            config.TELEGRAM_BOT_PID_FILE,
            expected_tokens=runtime_control.BOT_TOKENS,
            fallback_pid=bot_state.get("pid") or 0,
        )
        if result.get("already_stopped"):
            print("Telegram bot already stopped")
            return 0
        print(f"Telegram bot stopped: pid={result['pid']} via {result.get('signal', 'SIGTERM')}")
        return 0 if result["ok"] else 1

    if args.command == "daemon-status":
        _print_process_status("daemon", daemon_state)
        return 0

    if args.command == "bot-status":
        _print_process_status("telegram-bot", bot_state)
        return 0

    _print_process_status("daemon", daemon_state)
    _print_process_status("telegram-bot", bot_state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
