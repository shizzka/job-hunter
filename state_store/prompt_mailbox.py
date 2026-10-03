"""Short locked IPC mutations, owner-checked replies and captured wait paths."""
import json
import math
import os
import time
from pathlib import Path

from .json_store import atomic_write_json, file_lock


class PromptStateError(RuntimeError):
    pass


def read_prompt(path):
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (ValueError, UnicodeError) as exc:
        raise PromptStateError("Corrupt auth prompt; restore a verified backup") from exc
    return validate_prompt(payload)


def validate_prompt(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("id"), str) or not payload["id"]:
        raise PromptStateError("Invalid auth prompt identity")
    for key in ("started_at", "timeout_at", "received_at"):
        if key in payload:
            try:
                valid = type(payload[key]) in (int, float) and math.isfinite(payload[key])
            except OverflowError:
                valid = False
            if not valid:
                raise PromptStateError("Invalid auth prompt timestamp")
    for key in ("answer", "profile_name", "kind"):
        if key in payload and not isinstance(payload[key], str):
            raise PromptStateError("Invalid auth prompt metadata")
    return payload


class PromptMailbox:
    def __init__(self, pending_path, response_path, *, profile_name=None):
        self.pending = Path(pending_path).absolute()
        self.response = Path(response_path).absolute()
        self.lock_path = self.pending.with_name(f".{self.pending.name}.workflow.lock")
        self.profile_name = profile_name

    def _lock(self):
        return file_lock(self.pending, lock_path=self.lock_path)

    def _read(self):
        # Validate both before any mutation: corrupt peer files are not deleted.
        pending, response = read_prompt(self.pending), read_prompt(self.response)
        if pending and self.profile_name is not None and pending.get("profile_name") != self.profile_name:
            raise PromptStateError("Auth prompt profile/path mismatch")
        return pending, response

    def _unlink(self, path):
        path.unlink()
        fd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(fd)
        finally:
            os.close(fd)

    @staticmethod
    def _expired(pending):
        return bool(pending and pending.get("timeout_at") and pending["timeout_at"] <= time.time())

    def create(self, payload, *, writer=atomic_write_json):
        validate_prompt(payload)
        with self._lock():
            _, response = self._read()
            writer(self.pending, payload)
            if response is not None:
                self._unlink(self.response)

    def respond(self, request_id, answer, *, writer=atomic_write_json):
        if not isinstance(answer, str) or not answer.strip():
            raise ValueError("Empty auth response")
        with self._lock():
            pending, response = self._read()
            if not pending or pending["id"] != request_id or self._expired(pending):
                raise ValueError("Unknown, expired or superseded auth request")
            answer = answer.strip()
            if response and response["id"] == request_id:
                if response.get("answer") == answer:
                    return
                raise PromptStateError("Auth request already has an answer")
            writer(self.response, {"id": request_id, "kind": pending.get("kind", ""),
                   "profile_name": pending.get("profile_name", ""), "answer": answer,
                   "received_at": time.time()})

    def answer(self, request_id):
        with self._lock():
            pending, response = self._read()
            if not pending or pending["id"] != request_id or self._expired(pending):
                return False, None
            answer = response.get("answer", "").strip() if response and response["id"] == request_id else None
            return True, answer or None

    def complete(self, request_id):
        with self._lock():
            pending, response = self._read()
            for path, value in ((self.pending, pending), (self.response, response)):
                if value is not None and value["id"] == request_id:
                    self._unlink(path)

    def peek(self):
        with self._lock():
            pending, response = self._read()
            if self._expired(pending):
                self._unlink(self.pending)
                if response and response["id"] == pending["id"]:
                    self._unlink(self.response)
                return None
            if response and pending and response["id"] == pending["id"]:
                return None
            return pending
