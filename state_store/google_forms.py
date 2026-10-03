"""Persistence helpers for Google Form previews."""

from __future__ import annotations

import hashlib
import logging
import math
import os
import re
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from state_store.json_store import file_lock
from state_store.protected import ProtectedJsonStore


STATE_FILENAME = "google_form_previews.json"
MAX_PREVIEW_AGE_SECONDS = 7 * 24 * 60 * 60
TERMINAL_STATUSES = frozenset({"submitted", "submit_uncertain", "already_submitted"})
SUBMITTING_STATUS = "submit_in_progress"


def form_state_lock(home_dir: str | os.PathLike[str]):
    """Coordinate short preview/edit operations; never hold this across awaits."""
    # Lock order: workflow first, then one JSON store's sidecar at a time.
    return file_lock(Path(home_dir) / "google_form_workflow")


def valid_preview_state(state: dict[str, Any]) -> bool:
    if not isinstance(state, dict):
        return False
    items = state.get("items")
    if not isinstance(items, dict):
        return False
    for item in items.values():
        if not isinstance(item, dict):
            return False
        for key in ("questions", "answers"):
            if key in item and (
                not isinstance(item[key], list)
                or any(not isinstance(entry, dict) for entry in item[key])
            ):
                return False
        for key in ("fill_result", "vacancy"):
            if key in item and not isinstance(item[key], dict):
                return False
        if "status" in item and not isinstance(item["status"], str):
            return False
        if "superseded_by" in item and not isinstance(item["superseded_by"], str):
            return False
        claim = item.get("submission")
        if claim is not None:
            if (not isinstance(claim, dict) or not isinstance(claim.get("attempt_id"), str)
                    or not re.fullmatch(r'[a-f0-9]{32}', claim['attempt_id']) or not isinstance(claim.get('phase'), str)
                    or claim['phase'] not in {"preparing", "submitting", "completed"}
                    or not isinstance(claim.get("revision"), str) or not re.fullmatch(r'[a-f0-9]{64}', claim['revision'])):
                return False
            started = claim.get('started_at')
            if isinstance(started, bool) or not isinstance(started, (float, int)) or not math.isfinite(started):
                return False
            if claim['phase'] == 'completed':
                if item.get('status') not in TERMINAL_STATUSES | {'submit_failed'}:
                    return False
            elif item.get('status') != SUBMITTING_STATUS:
                return False
        if item.get("status") == SUBMITTING_STATUS and not claim:
            return False
        if item.get('status') == SUBMITTING_STATUS and claim['phase'] == 'completed':
            return False
    return True


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
        self._store = ProtectedJsonStore(
            self.path,
            default_factory=lambda: {"items": {}},
            validator=valid_preview_state,
            logger=logger,
            read_error_message="google form state read failed",
        )

    def load(self) -> dict[str, Any]:
        return self._store.load()

    def save(self, state: dict[str, Any]) -> None:
        """Explicit full snapshot; callers must not use stale read/modify/save."""
        if not valid_preview_state(state):
            raise ValueError("Invalid Google Form preview state")
        with form_state_lock(self.path.parent):
            self._store.save(state)

    def remember(
        self,
        token: str,
        detail: dict[str, Any],
        *,
        trim_expired: bool,
    ) -> dict[str, Any]:
        if not valid_preview_state({"items": {token: detail}}):
            raise ValueError("Invalid Google Form preview detail")

        def mutate(state):
            previous = state["items"].get(token)
            if previous and (previous.get("status") in TERMINAL_STATUSES | {SUBMITTING_STATUS}
                             or previous.get("superseded_by")) and previous != detail:
                raise ValueError("Cannot replace a terminal Google Form preview")
            state["items"][token] = detail
            if trim_expired:
                self.trim_expired(state)

        with form_state_lock(self.path.parent):
            return self._store.update(mutate)

    def trim_expired(self, state: dict[str, Any]) -> None:
        items = state.get("items")
        if not isinstance(items, dict):
            state["items"] = {}
            return

        cutoff = int(self._clock()) - self._max_preview_age_seconds
        for token, item in list(items.items()):
            if item.get("status") in {SUBMITTING_STATUS, "submit_uncertain"}:
                continue  # Never expire a possibly externally delivered attempt.
            try:
                created_at = int((item or {}).get("created_at") or 0)
            except (TypeError, ValueError):
                created_at = 0
            if created_at < cutoff:
                items.pop(token, None)
