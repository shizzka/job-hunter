from dataclasses import FrozenInstanceError
from types import SimpleNamespace

import pytest

from runtime_context import ChatResponderLimits, RuntimePaths, TelegramRuntimePaths


def _settings(home: str) -> SimpleNamespace:
    return SimpleNamespace(
        JOB_HUNTER_HOME=home,
        HH_STATE_DIR=f"{home}/state",
        RESUME_FILE=f"{home}/resume.md",
    )


def test_runtime_paths_capture_active_settings() -> None:
    paths = RuntimePaths.from_config(_settings("/tmp/profile-a"))

    assert paths.home_dir == "/tmp/profile-a"
    assert paths.hh_state_dir == "/tmp/profile-a/state"
    assert paths.resume_file == "/tmp/profile-a/resume.md"


def test_runtime_paths_are_a_late_immutable_snapshot() -> None:
    settings = _settings("/tmp/profile-a")
    first = RuntimePaths.from_config(settings)
    settings.JOB_HUNTER_HOME = "/tmp/profile-b"

    second = RuntimePaths.from_config(settings)

    assert first.home_dir == "/tmp/profile-a"
    assert second.home_dir == "/tmp/profile-b"
    with pytest.raises(FrozenInstanceError):
        first.home_dir = "/tmp/changed"


def test_chat_responder_limits_capture_environment() -> None:
    limits = ChatResponderLimits.from_env(
        {
            "HH_CHAT_MAX_REPLIES_PER_CHAT": "7",
            "HH_CHAT_REPLY_COOLDOWN_S": "12",
        }
    )

    assert limits.max_replies_per_chat == 7
    assert limits.reply_cooldown_s == 12


def test_chat_responder_limits_preserve_defaults_and_validation() -> None:
    assert ChatResponderLimits.from_env({}) == ChatResponderLimits(
        max_replies_per_chat=5,
        reply_cooldown_s=30,
    )
    with pytest.raises(ValueError):
        ChatResponderLimits.from_env({"HH_CHAT_MAX_REPLIES_PER_CHAT": "invalid"})


def test_telegram_runtime_paths_capture_process_files() -> None:
    settings = SimpleNamespace(
        TELEGRAM_BOT_PID_FILE="/tmp/profile-a/bot.pid",
        TELEGRAM_BOT_STATE_FILE="/tmp/profile-a/bot-state.json",
        TELEGRAM_BOT_RUNTIME_FILE="/tmp/profile-a/bot-runtime.json",
        TELEGRAM_BOT_LOG_FILE="/tmp/profile-a/bot.log",
        TELEGRAM_BOT_DEBUG_LOG_FILE="/tmp/profile-a/bot-debug.jsonl",
    )

    paths = TelegramRuntimePaths.from_config(settings)
    settings.TELEGRAM_BOT_STATE_FILE = "/tmp/profile-b/bot-state.json"

    assert paths == TelegramRuntimePaths(
        bot_pid_file="/tmp/profile-a/bot.pid",
        bot_state_file="/tmp/profile-a/bot-state.json",
        bot_runtime_file="/tmp/profile-a/bot-runtime.json",
        bot_log_file="/tmp/profile-a/bot.log",
        bot_debug_log_file="/tmp/profile-a/bot-debug.jsonl",
    )
