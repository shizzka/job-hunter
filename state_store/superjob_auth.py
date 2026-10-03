"""SuperJob token ownership and durable pre-network rotation claims."""
import copy
import math
import time
import uuid

from .revision_store import RevisionRepository, RevisionStateError


class SuperJobAuthError(RevisionStateError):
    pass


def validate_auth(payload):
    if not isinstance(payload, dict):
        raise SuperJobAuthError("Invalid SuperJob auth collection")
    for key in ("access_token", "refresh_token", "token_type"):
        if key in payload and not isinstance(payload[key], str):
            raise SuperJobAuthError("Invalid SuperJob auth token metadata")
    for key in ("expires_at", "resume_id"):
        if key in payload:
            value = payload[key]
            try:
                valid = type(value) in (int, float) and math.isfinite(value) and value >= 0
            except OverflowError:
                valid = False
            if not valid:
                raise SuperJobAuthError("Invalid SuperJob auth numeric metadata")
    if "user" in payload and not isinstance(payload["user"], dict):
        raise SuperJobAuthError("Invalid SuperJob account metadata")
    attempt = payload.get("_token_attempt")
    if attempt is not None:
        if (not isinstance(attempt, dict) or not isinstance(attempt.get("id"), str)
                or not attempt["id"] or attempt.get("status") not in ("active", "uncertain")
                or attempt.get("kind") not in ("refresh", "login")):
            raise SuperJobAuthError("Invalid SuperJob token attempt")
    return payload


class SuperJobAuthRepository(RevisionRepository):
    error_type = SuperJobAuthError

    def _validate(self, payload):
        return validate_auth(payload)


class SuperJobAuthSession:
    def __init__(self, path):
        self.repository = SuperJobAuthRepository(path)
        self.revision = None
        self.loaded = False
        self.revoked = False
        self.value = {}

    def get(self):
        if self.revoked:
            raise SuperJobAuthError("SuperJob auth ownership is superseded")
        value, revision = self.repository.snapshot()
        if self.loaded and revision != self.revision:
            self.revoked = True
            raise SuperJobAuthError("SuperJob account/session changed; restart client")
        self.loaded = True
        self.revision = revision
        self.value = value or {}
        return copy.deepcopy(self.value)

    def publish(self, value):
        if self.revoked:
            raise SuperJobAuthError("SuperJob auth ownership is superseded")
        try:
            revision = self.repository.save(value, expected_revision=self.revision)
        except BaseException:
            self.revoked = True
            raise
        self.value, self.revision, self.loaded = copy.deepcopy(value), revision, True

    def begin(self, kind):
        value = self.get()
        if value.get("_token_attempt"):
            raise SuperJobAuthError("SuperJob token attempt needs manual review; no automatic retry")
        attempt = {"id": uuid.uuid4().hex, "kind": kind, "status": "active", "started_at": time.time()}
        value["_token_attempt"] = attempt
        self.publish(value)
        return attempt["id"]

    def finish(self, owner, payload):
        value = self.get()
        if value.get("_token_attempt", {}).get("id") != owner:
            raise SuperJobAuthError("SuperJob token attempt ownership changed")
        if not isinstance(payload, dict) or not isinstance(payload.get("access_token"), str) or not payload["access_token"]:
            raise SuperJobAuthError("SuperJob did not return a valid access token")
        def seconds(field):
            number = payload.get(field) or 0
            if isinstance(number, str):
                try:
                    number = int(number)
                except ValueError as exc:
                    raise SuperJobAuthError("Invalid SuperJob token expiry") from exc
            try:
                valid = type(number) in (int, float) and math.isfinite(number) and number >= 0
            except OverflowError:
                valid = False
            if not valid:
                raise SuperJobAuthError("Invalid SuperJob token expiry")
            return int(number)
        expires = seconds("ttl") or int(time.time()) + seconds("expires_in")
        value.update(access_token=payload["access_token"],
                     refresh_token=payload.get("refresh_token", value.get("refresh_token", "")),
                     token_type=payload.get("token_type", "bearer"), expires_at=expires)
        value.pop("_token_attempt")
        self.publish(value)

    def uncertain(self, owner):
        if self.revoked:
            return
        value = self.get()
        if value.get("_token_attempt", {}).get("id") != owner:
            return
        value["_token_attempt"]["status"] = "uncertain"
        self.publish(value)
