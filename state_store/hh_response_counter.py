"""Ordered HH counter observations; no network work under the state lock."""
from datetime import datetime
from pathlib import Path

from .protected import ProtectedJsonStore


def observation_time(value):
    if not isinstance(value, str) or not value:
        raise ValueError("Invalid HH counter observation time")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("HH counter observation time must include timezone")
    return parsed


def valid_snapshot(state):
    if not state:
        return True
    if not isinstance(state.get("profile"), str) or not state["profile"]:
        return False
    try:
        observation_time(state.get("fetched_at"))
        if state.get("previous_fetched_at"):
            observation_time(state["previous_fetched_at"])
    except (TypeError, ValueError):
        return False
    for key in ("active", "archived", "deleted", "total", "active_pages", "archived_pages"):
        if type(state.get(key)) is not int or state[key] < 0:
            return False
    if state["total"] != state["active"] + state["archived"] + state["deleted"]:
        return False
    if type(state.get("refresh_sequence", 0)) is not int or state.get("refresh_sequence", 0) < 0:
        return False
    delta = state.get("delta", {})
    return isinstance(delta, dict) and all(key in {"active", "archived", "deleted", "total"}
                                           and type(value) is int for key, value in delta.items())


def valid_order(state):
    return (type(state.get("issued")) is int and state["issued"] >= 0
            and ("profile" not in state or isinstance(state["profile"], str) and bool(state["profile"])))


class HHCounterRepository:
    def __init__(self, path):
        self.path = Path(path).absolute()
        self.snapshot = ProtectedJsonStore(self.path, validator=valid_snapshot)
        self.order = ProtectedJsonStore(self.path.with_name("hh_response_counter_order.json"),
            default_factory=lambda: {"issued": 0}, validator=valid_order,
            lock_path=self.path.with_name(f".{self.path.name}.lock"))

    def begin(self, profile_name):
        if not isinstance(profile_name, str) or not profile_name:
            raise ValueError("HH counter profile is required")
        sequence = 0
        def issue(order):
            nonlocal sequence
            previous = self.snapshot._load_unlocked()  # Same stable sidecar already held.
            if (previous and previous["profile"] != profile_name) or order.get("profile", profile_name) != profile_name:
                raise ValueError("HH counter profile does not match its destination")
            sequence = max(order["issued"], previous.get("refresh_sequence", 0)) + 1
            order["issued"] = sequence
            order["profile"] = profile_name
        self.order.update(issue)
        return sequence

    def commit(self, current, sequence, *, enforce_timestamp=False):
        if type(sequence) is not int or sequence <= 0 or not current or not valid_snapshot(current):
            raise ValueError("Invalid HH counter observation")
        def update(previous):
            order = self.order._load_unlocked()  # Same stable sidecar already held.
            if sequence > order["issued"]:
                raise ValueError("HH counter refresh sequence was not issued")
            if order.get("profile") != current["profile"]:
                raise ValueError("HH counter refresh profile changed")
            if previous:
                if previous["profile"] != current["profile"]:
                    raise ValueError("HH counter profile does not match its destination")
                if sequence <= previous.get("refresh_sequence", 0) or (enforce_timestamp and
                        observation_time(current["fetched_at"]) < observation_time(previous["fetched_at"])):
                    return previous
            result = {**previous, **current, "refresh_sequence": sequence,
                      "previous_fetched_at": previous.get("fetched_at", ""),
                      "delta": {key: current[key] - previous[key]
                                for key in ("active", "archived", "deleted", "total")} if previous else {}}
            if not valid_snapshot(result):
                raise ValueError("Invalid HH counter result")
            return result
        return self.snapshot.update(update)
