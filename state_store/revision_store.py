"""Protected JSON snapshots: corruption is retained, publication uses CAS."""
import hashlib
import json
import os
from pathlib import Path

from .json_store import atomic_write_json, file_lock


class RevisionStateError(RuntimeError):
    pass


class RevisionRepository:
    error_type = RevisionStateError
    stale_message = "State session changed; stale owner cannot replace it"

    def __init__(self, path):
        self.path = Path(path).absolute()

    def _validate(self, payload):
        if not isinstance(payload, dict):
            raise self.error_type("Invalid state; restore a verified backup")
        return payload

    def _snapshot_unlocked(self):
        try:
            with self.path.open("rb") as stream:
                data = stream.read()
                info = os.fstat(stream.fileno())
        except FileNotFoundError:
            return None, None
        try:
            value = self._validate(json.loads(data.decode("utf-8")))
        except (ValueError, UnicodeError) as exc:
            raise self.error_type("Corrupt state; restore a verified backup") from exc
        revision = (info.st_dev, info.st_ino, info.st_mtime_ns, info.st_ctime_ns,
                    hashlib.sha256(data).hexdigest())
        return value, revision

    def snapshot(self):
        with file_lock(self.path):
            return self._snapshot_unlocked()

    def _write(self, value):
        atomic_write_json(self.path, value)

    def save(self, value, *, expected_revision=...):
        self._validate(value)
        with file_lock(self.path):
            previous, revision = self._snapshot_unlocked()
            if expected_revision is not ... and revision != expected_revision:
                raise self.error_type(self.stale_message)
            if previous == value:
                return revision
            self._write(value)
            return self._snapshot_unlocked()[1]
