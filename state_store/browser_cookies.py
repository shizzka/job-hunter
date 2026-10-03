"""Private browser-cookie snapshots with revision-checked publication."""
from __future__ import annotations

import math

from .revision_store import RevisionRepository


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


class CookieRepository(RevisionRepository):
    error_type = CookieStateError
    stale_message = "Cookie session changed; stale browser cannot replace it"

    def _validate(self, payload):
        try:
            return validate_cookies(payload)
        except CookieStateError as exc:
            if self.error_type is CookieStateError:
                raise
            raise self.error_type(str(exc)) from exc
