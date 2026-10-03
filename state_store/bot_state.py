"""Bot control state: transactions without resetting corrupt offsets/settings."""
import math

from .json_store import JsonStore


def _valid_state(state: dict) -> bool:
    for name in ("user_state", "guest_state", "health_check", "daily_summary", "form_pending"):
        if name in state and not isinstance(state[name], dict):
            return False
    for name in ("user_state", "guest_state", "form_pending"):
        if any(not isinstance(entry, dict) for entry in state.get(name, {}).values()):
            return False
    for entry in state.get('form_pending', {}).values():
        if not isinstance(entry.get('profile'), str) or not isinstance(entry.get('token'), str):
            return False
        for name in ('index', 'prompt_id'):
            if isinstance(entry.get(name), bool) or not isinstance(entry.get(name), int) or entry[name] < 0:
                return False
        created = entry.get('created_at')
        if isinstance(created, bool) or not isinstance(created, (float, int)) or not math.isfinite(created):
            return False
        if 'request_id' in entry and (not isinstance(entry['request_id'], str) or not entry['request_id']):
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
