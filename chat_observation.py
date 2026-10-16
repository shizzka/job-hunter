"""Private, local observations and fair scheduling; never opens a chat."""
from __future__ import annotations

import hashlib
import json
import os
import uuid
from contextvars import ContextVar
from datetime import datetime, timezone
from pathlib import Path

from state_store.json_store import file_lock, atomic_write_bytes, atomic_write_text
from state_store.protected import ProtectedJsonStore


active_chat_journal = ContextVar("active_chat_journal", default=None)


def private_directory(path):
    path = Path(path)
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.chmod(0o700)
    return path


class ChatJournal:
    def __init__(self, home, profile):
        self.home = private_directory(home)
        self.path = self.home / "chat_journal.jsonl"
        self.profile = profile
        self.run_id = uuid.uuid4().hex
        self.latest_threads = {}

    def write(self, event, **fields):
        snapshot_id = uuid.uuid4().hex
        record = dict(schema_version=1, event=event, snapshot_id=snapshot_id,
                      run_id=self.run_id, observed_at=datetime.now(timezone.utc).isoformat(),
                      profile=self.profile, **fields)
        payload = (json.dumps(record, ensure_ascii=False) + "\n").encode()
        # Serialize whole JSONL records across processes; never expose an initial 0644 file.
        with file_lock(self.path):
            fd = os.open(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "ab") as stream:
                    fd = -1
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
            finally:
                if fd >= 0:
                    os.close(fd)
        return snapshot_id

    def thread(self, chat_id, data):
        # Whitelist fields: never serialize a page, cookies, storage or transport details.
        messages = []
        for index, msg in enumerate(data.get("messages") or []):
            messages.append({
                "id": str(msg.get("id") or ""), "dom_index": msg.get("dom_index", index),
                "text": msg.get("text") or "", "raw_text": msg.get("raw_text", msg.get("text") or ""),
                "author": msg.get("raw_author", msg.get("author") or ""),
                "avatar_alt": msg.get("avatar_alt") or "",
                "direction": "outgoing" if msg.get("is_me") else "incoming",
                "is_me": bool(msg.get("is_me")), "is_ai": bool(msg.get("is_ai")),
                "is_ai_suspect": bool(msg.get("is_ai_suspect")),
                "is_me_evidence": msg.get("is_me_evidence") or [],
                "classification_evidence": msg.get("classification_evidence") or [],
                "links": msg.get("links") or [], "controls": msg.get("controls") or [],
                "time": msg.get("time") or "",
            })
        ref = self.write("chat_thread_snapshot", chat_id=chat_id,
                         vacancy=data.get("vacancy") or {}, history_complete=False, messages=messages,
                         controls=data.get("thread_controls") or [])
        self.latest_threads[chat_id] = ref
        return ref


def select_chat_batch(home, chats, limit):
    """Persist cursor and pending changes atomically, without touching reply claims.

    Unread edges/changed previews get priority once per observed version. Reserve
    one ordinary cursor slot when priority traffic would otherwise fill a batch.
    """
    store = ProtectedJsonStore(Path(home) / "chat_scan_state.json")
    selected = []

    def select(state):
        ids = [str(row["chat_id"]) for row in chats]
        previous = state.get("observations", {})
        observations = {}
        priority = []
        for row, chat_id in zip(chats, ids):
            fingerprint = hashlib.sha256((row.get("raw_text", row.get("preview")) or "").encode()).hexdigest()
            old = previous.get(chat_id, {})
            unread = bool(row.get("unread"))
            pending = bool(old.get("pending")) or (unread and not old.get("unread")) or (
                bool(old) and old.get("fingerprint") != fingerprint)
            observations[chat_id] = dict(fingerprint=fingerprint, unread=unread, pending=pending)
            if pending:
                priority.append(chat_id)
        start = ids.index(state["next_chat_id"]) if state.get("next_chat_id") in ids else 0
        rotated = ids[start:] + ids[:start]
        ordinary = [chat_id for chat_id in rotated if chat_id not in priority]
        slots = max(0, limit)
        priority_slots = max(0, slots - 1) if len(priority) >= slots and len(ids) > slots and slots > 1 else slots
        chosen = priority[:priority_slots]
        # With a one-chat budget alternate priority and cursor to prevent starvation.
        if slots == 1 and priority and ordinary and state.get("priority_only_last"):
            chosen = []
        cursor_chosen = [chat_id for chat_id in rotated if chat_id not in chosen][:slots - len(chosen)]
        chosen.extend(cursor_chosen)
        state["priority_only_last"] = bool(chosen and not cursor_chosen)
        if cursor_chosen:
            state["next_chat_id"] = ids[(ids.index(cursor_chosen[-1]) + 1) % len(ids)]
        elif not ids:
            state["next_chat_id"] = ""
        for chat_id in chosen:
            observations[chat_id]["pending"] = False
        state["observations"] = observations
        by_id = {str(row["chat_id"]): row for row in chats}
        selected.extend(by_id[chat_id] for chat_id in chosen)

    store.update(select)
    return selected


async def diagnostic_snapshot(journal, page, chat_id, message_id, decision, snapshot_ref, budget):
    """One-shot budget per category, durable reservations also survive process restarts."""
    if budget <= 0:
        return
    root = private_directory(journal.home / "chat_diagnostics")
    store = ProtectedJsonStore(root / "state.json")
    key = hashlib.sha256(f"{decision}|{chat_id}|{message_id}".encode()).hexdigest()
    reserved = False

    def reserve(state):
        nonlocal reserved
        if key in state or sum(v["decision"] == decision for v in state.values()) >= budget:
            return
        state[key] = dict(decision=decision, status="reserved")
        reserved = True

    store.update(reserve)
    if not reserved:
        return
    # Only message DOM, not page HTML with bootstrap/session scripts.
    dom = await page.evaluate("""() => {
        const bubbles = [...document.querySelectorAll('[data-qa^="chatik-chat-message-"]')]
            .filter(el => /^chatik-chat-message-\\d+$/.test(el.getAttribute('data-qa') || ''));
        return bubbles.map(el => {
            const clone = el.cloneNode(true);
            clone.querySelectorAll('script, iframe').forEach(node => node.remove());
            return clone.outerHTML;
        }).join('\\n');
    }""")
    atomic_write_text(root / f"{key}.html", dom)
    atomic_write_bytes(root / f"{key}.png", await page.screenshot())
    atomic_write_text(root / f"{key}.json", json.dumps(dict(
        run_id=journal.run_id, chat_id=chat_id, message_id=message_id,
        decision=decision, snapshot_ref=snapshot_ref), ensure_ascii=False))
    store.update(lambda state: state[key].update(status="saved"))
