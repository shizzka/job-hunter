"""Small JSON object store with atomic writes."""

from __future__ import annotations

import contextlib
import copy
import fcntl
import json
import logging
import os
import tempfile
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any


def _atomic_write(path: str | os.PathLike[str], write) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)

    fd = -1
    tmp_path = ""
    try:
        fd, tmp_path = tempfile.mkstemp(
            dir=target.parent,
            prefix=f".{target.name}.",
            suffix=".tmp",
        )
        os.fchmod(fd, 0o600)
        stream = os.fdopen(fd, "w", encoding="utf-8")
        fd = -1
        with stream:
            write(stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(tmp_path, target)
        tmp_path = ""
        os.chmod(target, 0o600)

        directory_fd = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    except Exception:
        if fd >= 0:
            with contextlib.suppress(OSError):
                os.close(fd)
        if tmp_path:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
        raise


def atomic_write_json(path: str | os.PathLike[str], value: Any) -> None:
    """Atomically write JSON with permissions suitable for runtime secrets."""
    _atomic_write(path, lambda stream: json.dump(value, stream, ensure_ascii=False, indent=2))


def atomic_write_text(path: str | os.PathLike[str], value: str) -> None:
    """Atomically write private text, keeping the old file if replacement fails."""
    _atomic_write(path, lambda stream: stream.write(value))


@contextlib.contextmanager
def file_lock(path: str | os.PathLike[str], *, lock_path: str | os.PathLike[str] | None = None):
    """Lock a stable sidecar, not the inode replaced by atomic writes."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lock_target = Path(lock_path) if lock_path is not None else target.with_name(f".{target.name}.lock")
    lock_target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_target, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.fchmod(fd, 0o600)
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


class JsonStore:
    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        default_factory: Callable[[], dict[str, Any]] = dict,
        corrupt_factory: Callable[[], dict[str, Any]] | None = None,
        validator: Callable[[dict[str, Any]], bool] | None = None,
        logger: logging.Logger | None = None,
        read_error_message: str = "json state read failed",
    ) -> None:
        self.path = Path(path)
        self._default_factory = default_factory
        self._corrupt_factory = corrupt_factory
        self._validator = validator
        self._logger = logger
        self._read_error_message = read_error_message

    def _default(self) -> dict[str, Any]:
        value = self._default_factory()
        return value if isinstance(value, dict) else {}

    def _corrupt_default(self) -> dict[str, Any]:
        if self._corrupt_factory is None:
            return self._default()
        value = self._corrupt_factory()
        return value if isinstance(value, dict) else self._default()

    @contextlib.contextmanager
    def _locked(self):
        with file_lock(self.path):
            yield

    def _preserve_corrupt_file(self) -> None:
        if not self.path.exists():
            return
        suffix = f".corrupt-{time.time_ns()}-{os.getpid()}"
        os.replace(self.path, self.path.with_name(f"{self.path.name}{suffix}"))
        self._fsync_parent()

    def _recover_corrupt_unlocked(self) -> dict[str, Any]:
        self._preserve_corrupt_file()
        recovered = self._corrupt_default()
        try:
            self._save_unlocked(recovered)
        except Exception as exc:
            if self._logger is not None:
                self._logger.warning("json state recovery save failed: %s", exc)
        return recovered

    def _load_unlocked(self) -> dict[str, Any]:
        try:
            with self.path.open(encoding="utf-8") as stream:
                value = json.load(stream)
        except FileNotFoundError:
            return self._default()
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            if self._logger is not None:
                self._logger.warning("%s: %s", self._read_error_message, exc)
            return self._recover_corrupt_unlocked()
        except OSError as exc:
            if self._logger is not None:
                self._logger.warning("%s: %s", self._read_error_message, type(exc).__name__)
            # A permission/I/O failure is not evidence that valid state is corrupt.
            raise
        if not isinstance(value, dict):
            if self._logger is not None:
                self._logger.warning("%s: expected a JSON object", self._read_error_message)
            return self._recover_corrupt_unlocked()
        if self._validator is not None and not self._validator(value):
            if self._logger is not None:
                self._logger.warning("%s: invalid state schema", self._read_error_message)
            return self._recover_corrupt_unlocked()
        return value

    def load(self) -> dict[str, Any]:
        with self._locked():
            return self._load_unlocked()

    def _fsync_parent(self) -> None:
        directory_fd = -1
        try:
            directory_fd = os.open(self.path.parent, os.O_RDONLY)
            os.fsync(directory_fd)
        finally:
            if directory_fd >= 0:
                os.close(directory_fd)

    def _save_unlocked(self, value: dict[str, Any]) -> None:
        if not isinstance(value, dict):
            raise TypeError("JsonStore only accepts dictionary state")
        atomic_write_json(self.path, value)

    def save(self, value: dict[str, Any]) -> None:
        with self._locked():
            self._save_unlocked(value)

    def update(
        self,
        mutator: Callable[[dict[str, Any]], dict[str, Any] | None],
        *,
        skip_unchanged: bool = False,
    ) -> dict[str, Any]:
        """Atomically load, mutate and save state while holding one file lock."""
        with self._locked():
            value = self._load_unlocked()
            before = copy.deepcopy(value) if skip_unchanged else None
            updated = mutator(value)
            if updated is None:
                updated = value
            if not skip_unchanged or updated != before:
                self._save_unlocked(updated)
            return updated
