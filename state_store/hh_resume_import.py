"""Bound HH resume imports: immutable exports and sticky partial publication."""
import os
import tempfile
import uuid
from pathlib import Path

from .json_store import atomic_write_text, file_lock
from .revision_store import RevisionRepository, RevisionStateError


class ResumeCatalogRepository(RevisionRepository):
    def _validate(self, value):
        if (not isinstance(value, list) or any(not isinstance(item, dict)
                or not isinstance(item.get("id"), str) or not item["id"] for item in value)):
            raise RevisionStateError("Invalid HH resume catalog; restore a verified backup")
        return value


class ImportControlRepository(RevisionRepository):
    def _validate(self, value):
        if (not isinstance(value, dict) or not isinstance(value.get("id"), str) or not value["id"]
                or value.get("status") not in {"active", "publishing", "uncertain", "completed", "failed"}):
            raise RevisionStateError("Invalid HH resume import control state")
        return value


def read_optional(path):
    try:
        return Path(path).read_bytes()
    except FileNotFoundError:
        return None


class HHResumeImport:
    def __init__(self, home_dir, resume_file, *, catalog_path=None, exports_dir=None):
        self.home = Path(home_dir).absolute()
        self.resume = Path(resume_file).absolute()
        self.env = self.home / "profile.env"
        self.catalog = ResumeCatalogRepository(catalog_path or self.home / "hh_resumes.json")
        self.control = ImportControlRepository(self.home / "hh_resume_import.json")
        self.lock = self.home / ".hh_resume_import.workflow.lock"
        self.exports_root = Path(exports_dir or self.home / "hh_resumes").absolute()
        self.owner = uuid.uuid4().hex
        self.publishing = False

    def begin(self):
        with file_lock(self.control.path, lock_path=self.lock):
            previous, revision = self.control.snapshot()
            if previous and previous.get("status") in ("active", "publishing", "uncertain"):
                raise RevisionStateError("HH resume import needs manual review; no automatic retry")
            _, self.catalog_revision = self.catalog.snapshot()
            self.original_resume = read_optional(self.resume)
            self.original_env = read_optional(self.env)
            if self.original_env is not None:
                self.original_env.decode("utf-8")
            self.control.save({"id": self.owner, "status": "active"}, expected_revision=revision)
        self.exports_root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.exports = Path(tempfile.mkdtemp(prefix="import-", dir=self.exports_root))

    def _owned(self):
        value, revision = self.control.snapshot()
        if not value or value.get("id") != self.owner:
            raise RevisionStateError("HH resume import ownership changed")
        return value, revision

    def publish(self, records, raw, update_env):
        with file_lock(self.control.path, lock_path=self.lock):
            value, revision = self._owned()
            if value.get("status") != "active":
                raise RevisionStateError("HH resume import is not active")
            if self.catalog.snapshot()[1] != self.catalog_revision:
                raise RevisionStateError("HH resume catalog changed during import")
            if read_optional(self.resume) != self.original_resume or read_optional(self.env) != self.original_env:
                raise RevisionStateError("HH resume or profile settings changed during import")
            self.publishing = True
            self.control.save({"id": self.owner, "status": "publishing"}, expected_revision=revision)
            self.catalog.save(records, expected_revision=self.catalog_revision)
            if records:
                update_env(self.env, self.original_env.decode("utf-8") if self.original_env is not None else None)
                with file_lock(self.resume):
                    if read_optional(self.resume) != self.original_resume:
                        raise RevisionStateError("HH resume changed before publication")
                    atomic_write_text(self.resume, raw.rstrip() + "\n")
            _, revision = self._owned()
            self.control.save({"id": self.owner, "status": "completed"}, expected_revision=revision)

    def fail(self):
        with file_lock(self.control.path, lock_path=self.lock):
            _, revision = self._owned()
            self.control.save({"id": self.owner, "status": "uncertain" if self.publishing else "failed"},
                              expected_revision=revision)
