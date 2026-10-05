"""Protected per-profile warning claims; no lock is held across delivery."""
import math
import re
import time
import uuid
from pathlib import Path

from .protected import ProtectedJsonStore


MAX_FINGERPRINTS = 64
MAX_RECENT_RUNS = 64
STAGES = ("search", "apply", "chat", "other")


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
    observations = state.get("observations", {})
    if not isinstance(observations, dict) or len(observations) > MAX_FINGERPRINTS:
        return False
    for key, item in observations.items():
        if not isinstance(key, str) or not re.fullmatch(r"[0-9a-f]{64}", key) or not isinstance(item, dict):
            return False
        for field in ("first_seen_at", "last_seen_at"):
            value = item.get(field)
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                return False
        for field in ("occurrences", "affected_runs"):
            if type(item.get(field)) is not int or item[field] < 0:
                return False
        by_stage = item.get("by_stage")
        if not isinstance(by_stage, dict) or set(by_stage) != set(STAGES):
            return False
        if any(type(value) is not int or value < 0 for value in by_stage.values()):
            return False
        runs = item.get("recent_run_ids")
        if not isinstance(runs, list) or len(runs) > MAX_RECENT_RUNS:
            return False
        if any(not isinstance(run, str) or not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", run) for run in runs):
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

    def observe(self, fingerprint, stage, run_id=""):
        """Count triggers independently of notification claims/cooldown.

        Run dedup uses a bounded recent window, not an unbounded ID ledger.
        """
        if not re.fullmatch(r"[0-9a-f]{64}", fingerprint) or stage not in STAGES:
            raise ValueError("Invalid HH UI observation")
        if run_id and not re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", run_id):
            raise ValueError("Invalid HH UI run ID")
        now = self._now()

        def update(state):
            observations = state.setdefault("observations", {})
            if fingerprint not in observations and len(observations) >= MAX_FINGERPRINTS:
                oldest = min(observations, key=lambda key: (observations[key]["last_seen_at"], key))
                del observations[oldest]
            item = observations.setdefault(fingerprint, {
                "first_seen_at": now, "last_seen_at": now, "occurrences": 0,
                "affected_runs": 0, "by_stage": {stage: 0 for stage in STAGES}, "recent_run_ids": [],
            })
            item["last_seen_at"] = now
            item["occurrences"] += 1
            item["by_stage"][stage] += 1
            if run_id and run_id not in item["recent_run_ids"]:
                item["affected_runs"] += 1
                item["recent_run_ids"] = (item["recent_run_ids"] + [run_id])[-MAX_RECENT_RUNS:]
            return state
        self.store.update(update)

    def claim_notification(self, fingerprint):
        return self.claim(fingerprint)

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
