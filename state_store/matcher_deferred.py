"""Durable unscored vacancies; transient Matcher errors are not rejections."""
from __future__ import annotations

import math
import copy
import time
import uuid
from pathlib import Path

from state_store.protected import ProtectedJsonStore


def _valid_state(state):
    items = state.get("items")
    if not isinstance(items, dict):
        return False
    for key, entry in items.items():
        if not isinstance(key, str) or not isinstance(entry, dict):
            return False
        vacancy = entry.get("vacancy")
        if not isinstance(vacancy, dict) or not isinstance(vacancy.get("id"), str) or not vacancy["id"].strip():
            return False
        source = vacancy.get("source", "hh")
        if not isinstance(source, str) or source not in {"hh", "habr", "geekjob", "superjob"} or key != f"{source}:{vacancy['id']}":
            return False
        if not isinstance(entry.get("revision"), str) or not entry["revision"]:
            return False
        if not isinstance(entry.get("details"), str) or not isinstance(entry.get("error_kind"), str):
            return False
        attempts = entry.get("attempts")
        if isinstance(attempts, bool) or not isinstance(attempts, int) or attempts < 1:
            return False
        for name in ("updated_at", "next_attempt_at"):
            value = entry.get(name)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                return False
    return True


class MatcherDeferredQueue:
    def __init__(self, home_dir, *, cooldown_seconds=300, clock=None):
        self.path = Path(home_dir) / "matcher_deferred.json"
        self.clock = clock or time.time
        self.cooldown_seconds = max(1, int(cooldown_seconds))
        self.store = ProtectedJsonStore(self.path, default_factory=lambda: {"items": {}}, validator=_valid_state)

    @staticmethod
    def key(vacancy):
        vacancy_id = str(vacancy.get("id") or "").strip()
        if not vacancy_id:
            raise ValueError("Deferred vacancy requires an ID")
        return f"{vacancy.get('source') or 'hh'}:{vacancy_id}"

    def defer(self, vacancy, details, error_kind):
        key = self.key(vacancy)
        snapshot = copy.deepcopy({k: v for k, v in vacancy.items() if not k.startswith("_matcher_deferred_")})
        snapshot["source"] = snapshot.get("source") or "hh"
        snapshot["id"] = str(snapshot["id"]).strip()
        if not isinstance(snapshot["source"], str) or snapshot["source"] not in {"hh", "habr", "geekjob", "superjob"}:
            raise ValueError("Unknown deferred vacancy source")
        now = self.clock()
        if not isinstance(now, (int, float)) or isinstance(now, bool) or not math.isfinite(now):
            raise ValueError("Invalid deferred queue clock")

        def update(state):
            previous = state["items"].get(key, {})
            state["items"][key] = {
                **previous, "vacancy": snapshot, "details": str(details or previous.get("details") or ""),
                "error_kind": str(error_kind), "revision": uuid.uuid4().hex,
                "attempts": previous.get("attempts", 0) + 1,
                "updated_at": max(now, previous.get("updated_at", 0)),
                "next_attempt_at": max(now + self.cooldown_seconds, previous.get("next_attempt_at", 0)),
            }
            return state

        self.store.update(update)

    def resolve(self, vacancy):
        """Only the observed revision may be removed; never a newer deferral."""
        key = self.key(vacancy)
        revision = vacancy.get("_matcher_deferred_revision")

        def update(state):
            current = state["items"].get(key)
            if current and revision and current["revision"] == revision:
                del state["items"][key]
            return state

        self.store.update(update)

    def merge_ready(self, vacancies, enabled_sources):
        """Retry even if discovery no longer returns the vacancy; enforce cooldown."""
        items = self.store.load()["items"]
        now = self.clock()
        if not isinstance(now, (int, float)) or isinstance(now, bool) or not math.isfinite(now):
            raise ValueError("Invalid deferred queue clock")
        merged, included = [], set()
        for vacancy in vacancies:
            if vacancy.get("source", "hh") not in enabled_sources:
                continue
            key = self.key(vacancy)
            if key in included:
                continue
            entry = items.get(key)
            if entry and now < entry["next_attempt_at"]:
                continue
            candidate = dict(vacancy)
            if entry:
                candidate["_matcher_deferred_revision"] = entry["revision"]
                if not candidate.get("details"):
                    candidate["details"] = entry["details"]
            merged.append(candidate)
            included.add(key)
        for key, entry in items.items():
            if key in included or now < entry["next_attempt_at"]:
                continue
            vacancy = dict(entry["vacancy"])
            if vacancy.get("source", "hh") not in enabled_sources:
                continue
            vacancy["_matcher_deferred_revision"] = entry["revision"]
            vacancy["details"] = entry["details"]
            merged.append(vacancy)
        return merged
