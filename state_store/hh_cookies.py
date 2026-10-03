"""Private HH cookie snapshots with optimistic ownership, never silent repair."""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path

from .json_store import atomic_write_json, file_lock


class HHCookieStateError(RuntimeError):
    pass


def validate_cookies(payload):
    cookies = payload.get("cookies") if isinstance(payload, dict) else payload
    if not isinstance(cookies, list):
        raise HHCookieStateError("Invalid HH cookie collection; restore a verified backup")
    for item in cookies:
        if not isinstance(item, dict) or not isinstance(item.get("name"), str) or not item["name"]:
            raise HHCookieStateError("Invalid HH cookie record; restore a verified backup")
        if not isinstance(item.get("value"), str):
            raise HHCookieStateError("Invalid HH cookie value; restore a verified backup")
        for field in ("domain", "path", "sameSite"):
            if field in item and not isinstance(item[field], str):
                raise HHCookieStateError("Invalid HH cookie metadata")
        for field in ("httpOnly", "secure"):
            if field in item and type(item[field]) is not bool:
                raise HHCookieStateError("Invalid HH cookie flag")
        if "expires" in item:
            value = item["expires"]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise HHCookieStateError("Invalid HH cookie expiry")
    return cookies


class HHCookieRepository:
    def __init__(self, path):
        self.path = Path(path).absolute()

    def _snapshot_unlocked(self):
        try:
            with self.path.open("rb") as stream:
                data = stream.read()
                info = os.fstat(stream.fileno())
        except FileNotFoundError:
            return None, None
        try:
            cookies = validate_cookies(json.loads(data.decode("utf-8")))
        except (ValueError, UnicodeError) as exc:
            raise HHCookieStateError("Corrupt HH cookies; restore a verified backup") from exc
        revision = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns,
                    hashlib.sha256(data).hexdigest())
        return cookies, revision

    def snapshot(self):
        with file_lock(self.path):
            return self._snapshot_unlocked()

    def save(self, cookies, *, expected_revision=...):
        validate_cookies(cookies)
        with file_lock(self.path):
            previous, revision = self._snapshot_unlocked()
            if expected_revision is not ... and revision != expected_revision:
                raise HHCookieStateError("HH session changed; stale browser cannot replace it")
            if previous == cookies:
                return revision
            atomic_write_json(self.path, cookies)
            return self._snapshot_unlocked()[1]
