"""Runtime settings captured after the active profile is selected."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class RuntimePaths:
    """Immutable path snapshot for one runtime operation."""

    home_dir: str
    hh_state_dir: str
    resume_file: str

    @classmethod
    def from_config(cls, settings: Any) -> RuntimePaths:
        return cls(
            home_dir=os.fspath(settings.JOB_HUNTER_HOME),
            hh_state_dir=os.fspath(settings.HH_STATE_DIR),
            resume_file=os.fspath(settings.RESUME_FILE),
        )


@dataclass(frozen=True, slots=True)
class ChatResponderLimits:
    """Safety limits captured once for a chat responder run."""

    max_replies_per_chat: int
    reply_cooldown_s: int

    @classmethod
    def from_env(cls, env: Mapping[str, str]) -> ChatResponderLimits:
        return cls(
            max_replies_per_chat=int(env.get("HH_CHAT_MAX_REPLIES_PER_CHAT", "5")),
            reply_cooldown_s=int(env.get("HH_CHAT_REPLY_COOLDOWN_S", "30")),
        )


@dataclass(frozen=True, slots=True)
class TelegramRuntimePaths:
    """Process-level files captured when the Telegram bot starts."""

    bot_pid_file: str
    bot_state_file: str
    bot_runtime_file: str
    bot_log_file: str
    bot_debug_log_file: str

    @classmethod
    def from_config(cls, settings: Any) -> TelegramRuntimePaths:
        return cls(
            bot_pid_file=os.fspath(settings.TELEGRAM_BOT_PID_FILE),
            bot_state_file=os.fspath(settings.TELEGRAM_BOT_STATE_FILE),
            bot_runtime_file=os.fspath(settings.TELEGRAM_BOT_RUNTIME_FILE),
            bot_log_file=os.fspath(settings.TELEGRAM_BOT_LOG_FILE or ""),
            bot_debug_log_file=os.fspath(settings.TELEGRAM_BOT_DEBUG_LOG_FILE or ""),
        )
