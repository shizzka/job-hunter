"""Protected per-profile warning claims; no lock is held across delivery."""
import math
import re
import time
import uuid
from pathlib import Path

from .protected import ProtectedJsonStore


def _valid(state):
    alerts = state.get("alerts")
    if not isinstance(alerts, dict):
        return False
    for key, item in alerts.items():
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key) or not isinstance(item, dict):
            return False
        if not isinstance(item.get("attempt_id"), str) or not item["attempt_id"]:
            return False
        if not isinstance(item.get("status"), str) or item["status"] not in {"attempting", "sent", "failed", "uncertain"}:
            return False
        for field in ("attempt_at", "sent_at"):
            value = item.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                return False
    return True


class HHUIWarnings:
    def __init__(self, home, *, clock=None):
        self.store = ProtectedJsonStore(Path(home) / "hh_ui_warnings.json", default_factory=lambda: {"alerts": {}}, validator=_valid)
        self.clock = clock or time.time

    def _now(self):
        now = self.clock()
        if isinstance(now, bool) or not isinstance(now, (int, float)) or not math.isfinite(now):
            raise ValueError("Invalid HH UI warning clock")
        return now

    def claim(self, fingerprint):
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint):
            raise ValueError("Invalid HH UI fingerprint")
        now, attempt = self._now(), uuid.uuid4().hex
        claimed = False

        def update(state):
            nonlocal claimed
            previous = state["alerts"].get(fingerprint)
            if previous and (now < previous["attempt_at"] + 300 or
                             (previous["status"] == "sent" and now < previous["sent_at"] + 86400)):
                return state
            state["alerts"][fingerprint] = {**(previous or {}), "attempt_id": attempt,
                "attempt_at": now, "sent_at": 0, "status": "attempting"}
            claimed = True
            return state

        self.store.update(update)
        return attempt if claimed else None

    def finish(self, fingerprint, attempt, status):
        if status not in {"sent", "failed", "uncertain"}:
            raise ValueError("Invalid HH UI warning result")
        now = self._now()

        def update(state):
            current = state["alerts"].get(fingerprint)
            if current and current["attempt_id"] == attempt:
                current["status"] = status
                if status == "sent":
                    current["sent_at"] = now
            return state

        self.store.update(update)
