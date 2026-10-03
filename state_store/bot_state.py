"""Bot control state: transactions without resetting corrupt offsets/settings."""
from .json_store import JsonStore


def _valid_state(state: dict) -> bool:
    for name in ("user_state", "guest_state", "health_check", "daily_summary"):
        if name in state and not isinstance(state[name], dict):
            return False
    for name in ("user_state", "guest_state"):
        if any(not isinstance(entry, dict) for entry in state.get(name, {}).values()):
            return False
    if state.get("last_update_id") is not None:
        try:
            return int(state["last_update_id"]) >= 0
        except (TypeError, ValueError, OverflowError):
            return False
    return True


class BotStateStore(JsonStore):
    def __init__(self, path):
        super().__init__(path, validator=_valid_state)

    def _recover_corrupt_unlocked(self) -> dict:
        raise RuntimeError(f"Corrupt bot state {self.path}; restore it from a verified backup.")
