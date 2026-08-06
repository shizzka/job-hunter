"""Persistence helpers for HH chat responder state."""

from __future__ import annotations

import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Any

from state_store.json_store import JsonStore


STATE_FILENAME = "chat_responder_state.json"
MAX_GOOGLE_FORM_PREVIEWS = 30


def google_form_seen_key(form_url: str, message_id: str = "") -> str:
    seed = f"{message_id}|{form_url}"
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


def get_google_form_previews(chat_state: dict[str, Any]) -> dict[str, Any]:
    previews = chat_state.get("google_form_previews")
    return previews if isinstance(previews, dict) else {}


def remember_google_form_preview(
    chat_state: dict[str, Any],
    key: str,
    detail: dict[str, Any],
    *,
    now: float | None = None,
    max_items: int = MAX_GOOGLE_FORM_PREVIEWS,
) -> None:
    previews = chat_state.setdefault("google_form_previews", {})
    if not isinstance(previews, dict):
        previews = {}
        chat_state["google_form_previews"] = previews

    previews[key] = {
        "created_at": int(time.time() if now is None else now),
        "ok": bool(detail.get("ok")),
        "status": detail.get("status") or detail.get("message") or "preview",
        "token": detail.get("token") or "",
        "form_url": detail.get("form_url") or detail.get("original_form_url") or "",
        "message_id": str(detail.get("message_id") or ""),
    }

    limit = max(0, int(max_items))
    if len(previews) <= limit:
        return
    ordered = sorted(
        previews.items(),
        key=lambda item: int((item[1] or {}).get("created_at") or 0),
    )
    for old_key, _ in ordered[: len(previews) - limit]:
        previews.pop(old_key, None)


class ChatResponderStateRepository:
    def __init__(
        self,
        home_dir: str | os.PathLike[str],
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self.path = Path(home_dir) / STATE_FILENAME
        self._store = JsonStore(
            self.path,
            default_factory=dict,
            logger=logger,
            read_error_message="state read failed",
        )

    def load(self) -> dict[str, Any]:
        return self._store.load()

    def save(self, state: dict[str, Any]) -> None:
        self._store.save(state)
