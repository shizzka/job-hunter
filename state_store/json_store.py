"""Small JSON object store with atomic writes."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from collections.abc import Callable
from pathlib import Path
from typing import Any


class JsonStore:
    def __init__(
        self,
        path: str | os.PathLike[str],
        *,
        default_factory: Callable[[], dict[str, Any]] = dict,
        logger: logging.Logger | None = None,
        read_error_message: str = "json state read failed",
    ) -> None:
        self.path = Path(path)
        self._default_factory = default_factory
        self._logger = logger
        self._read_error_message = read_error_message

    def _default(self) -> dict[str, Any]:
        value = self._default_factory()
        return value if isinstance(value, dict) else {}

    def load(self) -> dict[str, Any]:
        try:
            with self.path.open(encoding="utf-8") as stream:
                value = json.load(stream)
        except FileNotFoundError:
            return self._default()
        except Exception as exc:
            if self._logger is not None:
                self._logger.warning("%s: %s", self._read_error_message, exc)
            return self._default()
        return value if isinstance(value, dict) else self._default()

    def save(self, value: dict[str, Any]) -> None:
        if not isinstance(value, dict):
            raise TypeError("JsonStore only accepts dictionary state")

        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = -1
        tmp_path = ""
        try:
            fd, tmp_path = tempfile.mkstemp(
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
            )
            stream = os.fdopen(fd, "w", encoding="utf-8")
            fd = -1
            with stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(tmp_path, self.path)
        except Exception:
            if fd >= 0:
                with contextlib.suppress(OSError):
                    os.close(fd)
            if tmp_path:
                with contextlib.suppress(OSError):
                    os.unlink(tmp_path)
            raise
