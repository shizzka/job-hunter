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
