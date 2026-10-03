"""Cooperating private process logs, without long-lived rotated inode handles."""
import logging
import sys

from state_store.private_journal import append_text


class PrivateFileHandler(logging.FileHandler):
    def __init__(self, filename):
        super().__init__(filename, mode="a", encoding="utf-8", delay=True)
        self._failure_reported = False

    def emit(self, record):
        try:
            append_text(self.baseFilename, self.format(record) + self.terminator)
            self._failure_reported = False
        except Exception as exc:
            # Never echo the failed record/exception (it may contain credentials).
            if not self._failure_reported:
                self._failure_reported = True
                try:
                    sys.stderr.write(f"Private log write failed: {type(exc).__name__}\n")
                except Exception:
                    pass
