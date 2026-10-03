"""Private browser-cookie snapshots with revision-checked publication."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path

from .json_store import atomic_write_json, file_lock


class CookieStateError(RuntimeError):
    pass


def validate_cookies(payload):
    cookies = payload.get("cookies") if isinstance(payload, dict) else payload
    if not isinstance(cookies, list):
        raise CookieStateError("Invalid cookie collection; restore a verified backup")
    for item in cookies:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"]:
            raise CookieStateError("Invalid cookie record; restore a verified backup")
        if not isinstance(item.get("value"), str):
            raise CookieStateError("Invalid cookie value; restore a verified backup")
        for field in ("domain", "path", "sameSite"):
            if field in item and not isinstance(item[field], str):
                raise CookieStateError("Invalid cookie metadata")
        for field in ("httpOnly", "secure"):
            if field in item and type(item[field]) is not bool:
                raise CookieStateError("Invalid cookie flag")
        if "expires" in item:
            value = item["expires"]
            try:
                valid = not isinstance(value, bool) and isinstance(value, (int, float)) and math.isfinite(value)
            except OverflowError:
                valid = False
            if not valid:
                raise CookieStateError("Invalid cookie expiry")
    return cookies


class CookieRepository:
    error_type = CookieStateError

    def __init__(self, path):
        self.path = Path(path).absolute()

    def _validate(self, payload):
        try:
            return validate_cookies(payload)
        except CookieStateError as exc:
            if self.error_type is CookieStateError:
                raise
            raise self.error_type(str(exc)) from exc

    def _snapshot_unlocked(self):
        try:
            with self.path.open("rb") as stream:
                data = stream.read()
                info = os.fstat(stream.fileno())
        except FileNotFoundError:
            return None, None
        try:
            cookies = self._validate(json.loads(data.decode("utf-8")))
        except (ValueError, UnicodeError) as exc:
            raise self.error_type("Corrupt cookies; restore a verified backup") from exc
        revision = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns,
                    hashlib.sha256(data).hexdigest())
        return cookies, revision

    def snapshot(self):
        with file_lock(self.path):
            return self._snapshot_unlocked()

    def _write(self, cookies):
        atomic_write_json(self.path, cookies)

    def save(self, cookies, *, expected_revision=...):
        self._validate(cookies)
        with file_lock(self.path):
            previous, revision = self._snapshot_unlocked()
            if expected_revision is not ... and revision != expected_revision:
                raise self.error_type("Cookie session changed; stale browser cannot replace it")
            if previous == cookies:
                return revision
            self._write(cookies)
            return self._snapshot_unlocked()[1]
