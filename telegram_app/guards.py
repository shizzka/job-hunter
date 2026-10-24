"""Per-user admission, independent of profile execution locks."""
from __future__ import annotations

import asyncio
from functools import wraps
import hashlib
import time
from contextvars import ContextVar

import logging

import telegram_access

log = logging.getLogger(__name__)
from state_store.bot_state import prune_callback_claims
from state_store.locking import nonblocking_state_locks

_read_workers = ContextVar("read_workers", default=None)


@nonblocking_state_locks
async def read_thread(fn, *args, **kwargs):
    task = asyncio.create_task(asyncio.to_thread(fn, *args, **kwargs))
    workers = _read_workers.get()
    if workers is not None:
        workers.append(task)
    return await asyncio.shield(task)

BUSY = "Уже считаю, подождите пару секунд"
DONE = "Уже сделано"
ALIASES = {"/hunter_log": "/log", "/hunter_chat_log": "/chat_log"}
READS = {"/stats", "/runs", "/log", "/raw_log", "/chat_log", "/digest", "/research",
         "/directions", "/direction_summary", "/direction_stats", "/direction_csv",
         "/why_no_apply"}  # 24.10: five quick presses held every other command for ~40 s
AI_LABELS = {"analyze resume", "chat AI reply", "chat AI alternative", "chat AI list",
             "suggest search queries", "manual AI apply", "google form preview", "Проверка черновика Google Form"}


def reserve(bot, key):
    slots = bot.__dict__.setdefault("_request_slots", {})
    now = time.monotonic()
    # None means running; finished slots expire without timers or waiting locks.
    for old, until in list(slots.items()):
        if until is not None and until <= now:
            del slots[old]
    if key in slots:
        return False
    slots[key] = None
    return True


def release(bot, key, cooldown=0):
    if key is None:
        return
    slots = bot.__dict__.setdefault("_request_slots", {})
    if cooldown:
        slots[key] = time.monotonic() + cooldown
    else:
        slots.pop(key, None)


def release_entry(bot, entry):
    # An old launch's finally must not release a newer request with the same key.
    key, entry.request_key = entry.request_key, None
    release(bot, key)


def read_command(fn):
    @wraps(fn)
    async def wrapped(self, chat_id, principal, command, arg):
        canonical = ALIASES.get(command, command)
        if canonical not in READS:
            return await fn(self, chat_id, principal, command, arg)
        key = (int(principal["user_id"]), "read", canonical)
        if not reserve(self, key):
            await self._send_text(chat_id, BUSY)
            return
        workers = []
        token = _read_workers.set(workers)
        try:
            return await fn(self, chat_id, principal, command, arg)
        finally:
            _read_workers.reset(token)
            pending = {task for task in workers if not task.done()}
            def finished(task):
                # Retrieve exceptions even when the foreground read timed out.
                if not task.cancelled():
                    task.exception()
                pending.discard(task)
                if not pending:
                    release(self, key, 5)
            if pending:
                for task in pending.copy():
                    task.add_done_callback(finished)
            else:
                release(self, key, 5)
    return wrapped


def read_callback(data):
    parts = str(data).split(":")
    return (parts[0] == "hist"
            or parts[0] == "gs" and len(parts) > 1 and parts[1] in {"auto", "dirs", "resumes", "check"}
            or parts[0] == "dir" and len(parts) > 2 and parts[2] in {
                "stats", "list", "back", "editmenu", "edit", "delete", "strategy", "letteroffer", "variants"}
            or parts[0] == "rq" and len(parts) > 1 and parts[1] in {"p", "u", "o", "d"}
            or parts[0] in {"manual_why", "chat_ai", "chat_ai_any", "chat_ai_alt", "chat_ai_any_alt",
                            "gform_preview"}
            or parts[0] == "gf" and len(parts) > 1 and parts[1] in {"view", "check"})


def release_callback(bot, callback):
    claim = (callback or {}).get("_guard_claim")
    if claim:
        def mutate(state):
            state.get("callback_claims", {}).pop(claim, None)
        bot._update_state(mutate)


def release_callback_soon(bot, callback, attempts=20):
    """release_callback that never raises on a busy bot-state lock (T31 critic): the queue drainer keeps going,
    the release is retried on the event loop (zero-wait each time) for about 10 s, then left held (fail closed)."""
    from state_store.locking import StateLockBusy
    try:
        release_callback(bot, callback)
    except StateLockBusy:
        if attempts > 1:
            asyncio.get_running_loop().call_later(0.5, release_callback_soon, bot, callback, attempts - 1)
        else:
            log.warning("Callback claim release gave up: bot state lock stayed busy")


def callback_guard(fn):
    """Claim before the first await/queue admission; retain uncertain actions.

    Claims are durable bot control state, so old keyboards remain spent after a
    restart. Read/navigation and AI preview buttons have separate admission.
    """
    @wraps(fn)
    async def wrapped(self, callback):
        data = str(callback.get("data") or "")
        message = callback.get("message") or {}
        user_id = int((callback.get("from") or {}).get("id") or 0)
        chat_id = int((message.get("chat") or {}).get("id") or 0)
        principal = telegram_access.resolve_user(user_id)
        private_only = data.startswith(("dir:", "cbo:", "cbs:", "cop:", "qm:", "gf:"))
        rejected_chat = (message.get("chat") or {}).get("type") not in {None, "", "private"}
        if (not principal or chat_id <= 0 or not data or read_callback(data)
                or private_only and rejected_chat):
            # The router rejects these before any control-state mutation.
            return await fn(self, callback)
        # Action tokens identify apply/send intents across copies of a message.
        semantic = data.startswith(("manual_apply:", "chat_send:", "chat_send_any:", "gform_submit:", "mad:", "stub:", "dir:"))
        identity = (user_id, chat_id, 0 if semantic else message.get("message_id", 0), data)
        claim = hashlib.sha256(repr(identity).encode()).hexdigest()
        accepted = False
        def mutate(state):
            nonlocal accepted
            prune_callback_claims(state)
            claims = state.setdefault("callback_claims", {})
            if claim not in claims:
                claims[claim] = time.time()
                accepted = True
        self._update_state(mutate)
        if not accepted:
            if data.startswith("manual_apply:") and hasattr(self, "_show_uncertain_manual_apply"):
                from manual_apply_queue import parse_manual_apply_callback_data
                profile, token = parse_manual_apply_callback_data(data)
                if await self._show_uncertain_manual_apply(chat_id, principal, profile, token):
                    await self._answer_callback_query(str(callback.get("id") or ""), "Нужна сверка на hh.ru")
                    return
            await self._answer_callback_query(str(callback.get("id") or ""), DONE)
            return
        claimed = {**callback, "_guard_claim": claim}
        result = await fn(self, claimed)
        if result is False:  # Admission failed before the action started.
            release_callback(self, claimed)
        return result
    return wrapped


def background_read(update):
    """Only independent reads leave polling FIFO; mutations keep their order."""
    from telegram_bot_ui import _resolve_message_command
    callback = update.get("callback_query") or {}
    data = str(callback.get("data") or "")
    if data.startswith("hist:"):
        return True
    parts = data.split(":")
    if parts[0] == "dir" and len(parts) > 2 and parts[2] == "stats":
        return True
    message = update.get("message") or {}
    command, _ = _resolve_message_command(str(message.get("text") or ""), "admin")
    return ALIASES.get(command, command) in READS | {"/hh_responses", "/hh_response_count"}
