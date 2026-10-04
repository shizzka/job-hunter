"""Persistence helpers for HH chat responder state."""

from __future__ import annotations

import contextlib
import hashlib
import logging
import math
import os
import re
import time
import uuid
from pathlib import Path
from typing import Any

from state_store.protected import ProtectedJsonStore


STATE_FILENAME = "chat_responder_state.json"
MAX_GOOGLE_FORM_PREVIEWS = 30


def _valid_state(state: dict) -> bool:
    for chat_id, chat in state.items():
        if not isinstance(chat_id, str) or not chat_id or not isinstance(chat, dict):
            return False
        count = chat.get("replies_count", 0)
        if type(count) is not int or count < 0:
            return False
        for name in ("last_replied_msg_id", "last_previewed_msg_id", "last_suspicious_msg_id"):
            if name in chat and not isinstance(chat[name], str):
                return False
        for name in ("last_reply_at", "last_previewed_at", "last_suspicious_at"):
            value = chat.get(name, 0)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                return False
        previews = chat.get("google_form_previews", {})
        if not isinstance(previews, dict) or any(not isinstance(item, dict) for item in previews.values()):
            return False
        for item in previews.values():
            value = item.get("created_at", 0)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                return False
        draft = chat.get("draft")
        if draft is not None and (not isinstance(draft, dict)
                or not all(isinstance(draft.get(key), str) and draft[key] for key in
                           ('message_id', 'answer', 'answer_hash', 'revision', 'candidate_revision', 'source_revision'))
                or not re.fullmatch(r'[0-9a-f]{12}', draft['revision'])
                or any(not re.fullmatch(r'[0-9a-f]{64}', draft[key]) for key in
                       ('answer_hash', 'candidate_revision', 'source_revision'))
                or hashlib.sha256(draft['answer'].encode()).hexdigest() != draft['answer_hash']):
            return False
        attempts = chat.get("attempts", {})
        if not isinstance(attempts, dict):
            return False
        owners = set()
        for attempt_key, item in attempts.items():
            if not isinstance(item, dict) or item.get("kind") not in {"reply", "preview", "notice", "form"}:
                return False
            if item.get("status") not in {"preparing", "acting", "completed", "failed", "uncertain"}:
                return False
            if not all(isinstance(item.get(field), str) and item[field] for field in ("owner", "key")):
                return False
            if not re.fullmatch(r"[0-9a-f]{32}", item["owner"]) or item["owner"] in owners:
                return False
            owners.add(item["owner"])
            if attempt_key != hashlib.sha256(f"{item['kind']}|{item['key']}".encode()).hexdigest():
                return False
            value = item.get("started_at")
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                return False
    return True


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
        self._store = ProtectedJsonStore(
            self.path,
            default_factory=dict,
            validator=_valid_state,
            logger=logger,
            read_error_message="state read failed",
        )

    def load(self) -> dict[str, Any]:
        return self._store.load()

    def save(self, state: dict[str, Any]) -> None:
        # Compatibility/import API only. Async workflows must use scoped claims.
        if not _valid_state(state):
            raise ValueError("Invalid chat responder state")
        self._store.update(lambda current: state)

    def update_chat(self, chat_id: str, mutator) -> dict:
        if not isinstance(chat_id, str) or not chat_id:
            raise ValueError("Chat ID is required")

        def update(state):
            mutator(state.setdefault(chat_id, {}))
            if not _valid_state(state):
                raise ValueError("Invalid chat responder mutation")
            return state

        return self._store.update(update)[chat_id]

    def claim(self, chat_id: str, kind: str, key: str, *, max_replies=None, repeat=False):
        if kind not in {"reply", "preview", "notice", "form"} or not isinstance(key, str) or not key:
            raise ValueError("Invalid chat attempt")
        if max_replies is not None and (type(max_replies) is not int or max_replies <= 0):
            raise ValueError("Invalid chat reply limit")
        owner = uuid.uuid4().hex
        claimed = False
        attempt_key = hashlib.sha256(f"{kind}|{key}".encode()).hexdigest()

        def update(chat):
            nonlocal claimed
            attempts = chat.get("attempts", {})
            # One browser/model owner per chat. No expiry: a crash needs review.
            if any(item["status"] in {"preparing", "acting"} or
                   (item["kind"] == "reply" and item["status"] == "uncertain")
                   for item in attempts.values()):
                return
            previous = attempts.get(attempt_key)
            if previous and (previous["status"] == "uncertain" or
                             (previous["status"] == "completed" and not (repeat and kind == "preview"))):
                return
            if kind in {"reply", "preview"}:
                if chat.get("last_replied_msg_id") == key:
                    return
                if max_replies is not None and chat.get("replies_count", 0) >= max_replies:
                    return
                if kind == "preview" and not repeat and chat.get("last_previewed_msg_id") == key:
                    return
            if kind == "notice" and chat.get("last_suspicious_msg_id") == key:
                return
            if kind == "form" and key in get_google_form_previews(chat):
                return
            chat.setdefault("attempts", {})[attempt_key] = {
                "owner": owner, "kind": kind, "key": key,
                "status": "preparing", "started_at": time.time(),
            }
            claimed = True

        self.update_chat(chat_id, update)
        return owner if claimed else None

    def set_draft(self, chat_id, owner, draft):
        def update(chat):
            if not any(item['owner'] == owner and item['status'] == 'preparing'
                       and item['kind'] == 'preview' for item in chat.get('attempts', {}).values()):
                raise RuntimeError('Chat draft ownership lost')
            chat['draft'] = draft
        self.update_chat(chat_id, update)

    def mark_acting(self, chat_id: str, owner: str) -> None:
        marked = False

        def update(chat):
            nonlocal marked
            for item in chat.get("attempts", {}).values():
                if item["owner"] == owner and item["status"] == "preparing":
                    item["status"] = "acting"
                    marked = True

        self.update_chat(chat_id, update)
        if not marked:
            raise RuntimeError("Chat attempt ownership lost")

    def confirm_no_action(self, chat_id: str, owner: str) -> None:
        """Owned positive zero-dispatch receipt; ambiguous outcomes stay acting."""
        def update(chat):
            for item in chat.get("attempts", {}).values():
                if item["owner"] == owner and item["status"] == "acting":
                    item["status"] = "preparing"
                    return
            raise RuntimeError("Chat no-action receipt ownership lost")
        self.update_chat(chat_id, update)

    def finish(self, chat_id: str, owner: str, status: str, mutator=None) -> bool:
        if status not in {"completed", "failed", "uncertain"}:
            raise ValueError("Invalid chat attempt outcome")
        finished = False

        def update(chat):
            nonlocal finished
            for item in chat.get("attempts", {}).values():
                if item["owner"] != owner or item["status"] not in {"preparing", "acting"}:
                    continue
                outcome = "uncertain" if status == "failed" and item["status"] == "acting" else status
                item["status"] = outcome
                if mutator is not None:
                    mutator(chat)
                if outcome == "completed" and item["kind"] == "reply":
                    chat["last_replied_msg_id"] = item["key"]
                    chat["replies_count"] = chat.get("replies_count", 0) + 1
                    chat["last_reply_at"] = time.time()
                finished = True

        self.update_chat(chat_id, update)
        return finished

    @contextlib.contextmanager
    def attempt(self, chat_id: str, kind: str, key: str, **kwargs):
        owner = self.claim(chat_id, kind, key, **kwargs)
        try:
            yield owner
        finally:
            if owner:
                # This is a short transaction, not a lock spanning the yield.
                def abandon(chat):
                    for item in chat.get("attempts", {}).values():
                        if item["owner"] == owner and item["status"] in {"preparing", "acting"}:
                            item["status"] = "uncertain" if item["status"] == "acting" else "failed"
                self.update_chat(chat_id, abandon)
