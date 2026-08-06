"""Persistence helpers for Google Form previews."""

from __future__ import annotations

import hashlib
import logging
import os
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from state_store.json_store import JsonStore


STATE_FILENAME = "google_form_previews.json"
MAX_PREVIEW_AGE_SECONDS = 7 * 24 * 60 * 60


def new_preview_token(
    form_url: str,
    chat_id: str = "",
    message_id: str = "",
    *,
    now: float | None = None,
    pid: int | None = None,
) -> str:
    timestamp = time.time() if now is None else now
    process_id = os.getpid() if pid is None else pid
    seed = f"{form_url}|{chat_id}|{message_id}|{timestamp}|{process_id}"
    return hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]


class GoogleFormStateRepository:
    def __init__(
        self,
        home_dir: str | os.PathLike[str],
        *,
        logger: logging.Logger | None = None,
        clock: Callable[[], float] = time.time,
        max_preview_age_seconds: int = MAX_PREVIEW_AGE_SECONDS,
    ) -> None:
        self.path = Path(home_dir) / STATE_FILENAME
        self._clock = clock
        self._max_preview_age_seconds = max(0, int(max_preview_age_seconds))
        self._store = JsonStore(
            self.path,
            default_factory=lambda: {"items": {}},
            logger=logger,
            read_error_message="google form state read failed",
        )

    def load(self) -> dict[str, Any]:
        state = self._store.load()
        if not isinstance(state.get("items"), dict):
            state["items"] = {}
        return state

    def save(self, state: dict[str, Any]) -> None:
        if not isinstance(state.get("items"), dict):
            state = {**state, "items": {}}
        self._store.save(state)

    def remember(
        self,
        token: str,
        detail: dict[str, Any],
        *,
        trim_expired: bool,
    ) -> dict[str, Any]:
        state = self.load()
        state["items"][token] = detail
        if trim_expired:
            self.trim_expired(state)
        self.save(state)
        return state

    def trim_expired(self, state: dict[str, Any]) -> None:
        items = state.get("items")
        if not isinstance(items, dict):
            state["items"] = {}
            return

        cutoff = int(self._clock()) - self._max_preview_age_seconds
        for token, item in list(items.items()):
            try:
                created_at = int((item or {}).get("created_at") or 0)
            except (TypeError, ValueError):
                created_at = 0
            if created_at < cutoff:
                items.pop(token, None)
