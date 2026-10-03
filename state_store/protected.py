"""JSON object state that requires explicit restoration instead of data loss."""
import contextlib

from .json_store import JsonStore, file_lock


class ProtectedJsonStore(JsonStore):
    def __init__(self, path, *, lock_path=None, **kwargs):
        super().__init__(path, **kwargs)
        self._lock_path = lock_path

    @contextlib.contextmanager
    def _locked(self):
        # Preserve legacy sidecar names where older running code uses them.
        with file_lock(self.path, lock_path=self._lock_path):
            yield

    def _recover_corrupt_unlocked(self) -> dict:
        # Leaving the file in place makes repeated reads fail closed too.
        raise RuntimeError(f"Corrupt state {self.path}; restore it from a verified backup.")

    def update(self, mutator) -> dict:
        # Preserve no-op semantics: unknown queue tokens and duplicate facts
        # must not create/rewrite JSON or fail due to an unnecessary replace.
        return super().update(mutator, skip_unchanged=True)
