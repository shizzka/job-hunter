"""Compatibility API for HH's shared private cookie storage contract."""
from .browser_cookies import CookieRepository, CookieStateError, validate_cookies as _validate
from .json_store import atomic_write_json


class HHCookieStateError(CookieStateError):
    pass


def validate_cookies(payload):
    try:
        return _validate(payload)
    except CookieStateError as exc:
        raise HHCookieStateError(str(exc)) from exc


class HHCookieRepository(CookieRepository):
    error_type = HHCookieStateError

    def _write(self, cookies):
        # Keep the original module-level writer seam for existing callers/tests.
        atomic_write_json(self.path, cookies)
