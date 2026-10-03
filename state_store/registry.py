"""Transactional registries that never silently reset corrupt access or quotas."""
from __future__ import annotations

import logging
from collections.abc import Callable

from .json_store import JsonStore


class RegistryStateError(RuntimeError):
    """An existing registry needs explicit restoration before it can be used."""


class RegistryStore(JsonStore):
    def __init__(self, path, *, normalize: Callable, collections: tuple[str, ...]):
        self._normalize = normalize
        self._primary_collection = collections[0]
        super().__init__(
            path,
            default_factory=lambda: normalize(None),
            validator=lambda state: self._primary_collection in state and all(
                isinstance(state.get(name, []), list)
                and all(isinstance(item, dict) for item in state.get(name, []))
                for name in collections
            ),
            logger=logging.getLogger(__name__),
            read_error_message="registry read failed",
        )

    def _recover_corrupt_unlocked(self) -> dict:
        # Keep the original in place: moving it away would make the next load
        # mistake corruption for a first-time setup and grant fresh defaults.
        raise RegistryStateError(f"Corrupt registry {self.path}; restore it from a verified backup.")

    def _load_unlocked(self) -> dict:
        value = super()._load_unlocked()
        seen_ids = set()
        for item in value[self._primary_collection]:
            try:
                user_id = int(item.get("user_id") or 0)
            except (TypeError, ValueError):
                return self._recover_corrupt_unlocked()
            if user_id <= 0 or user_id in seen_ids:
                # Owner bootstrapping must not mask an invalid/dropped record.
                return self._recover_corrupt_unlocked()
            seen_ids.add(user_id)
        normalized = self._normalize(value)
        if normalized != value or not self.path.exists():
            self._save_unlocked(normalized)
        return normalized

    def _save_unlocked(self, value: dict) -> None:
        # Keep the original normalization rules on writes as well as reads
        # (notably the configured owner entry in the access registry).
        super()._save_unlocked(self._normalize(value))
