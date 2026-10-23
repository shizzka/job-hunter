"""Cooperating private process logs, without long-lived rotated inode handles."""
import logging
import copy
import os
import re
import sys
import queue
import threading

from state_store.private_journal import append_text
from state_store.locking import lock_wait_timeout



def chat_log_path(search_log):
    return os.path.join(os.path.dirname(search_log), "job-hunter-chat.log")


def _context():
    # Reuse analytics' task-local context; no mutable config lookup during emit.
    module = sys.modules.get("analytics")
    return module.current_context() if module is not None else {}


def log_channel(record):
    if hasattr(record, "_private_log_channel"):
        return record._private_log_channel
    if record.name == "chat_responder" or record.name.startswith("chat_responder."):
        return "chat"
    if record.name == "telegram_bot" or record.name.startswith("telegram_app"):
        return "bot"
    return "chat" if _context().get("channel") == "chat" else "search"


class ChannelFilter(logging.Filter):
    def __init__(self, channel):
        super().__init__()
        self.channel = channel

    def filter(self, record):
        return log_channel(record) == self.channel


class OperationalFormatter(logging.Formatter):
    """Safe exception class only, task-local IDs, and no auth URLs in logs."""
    def __init__(self, fmt=None):
        super().__init__(fmt or "%(asctime)s [%(name)s] %(levelname)s: %(context)s%(message)s")

    def format(self, record):
        record = copy.copy(record)
        def argument(value):
            return type(value).__name__ if isinstance(value, BaseException) else value
        if isinstance(record.args, dict):
            record.args = {key: argument(value) for key, value in record.args.items()}
        elif record.args:
            record.args = tuple(argument(value) for value in record.args)
        record.context = ""
        context = {**_context(), **getattr(record, "observation_fields", {})}
        for field, label in (("run_id", "run"), ("source", "source"),
                             ("vacancy_id", "vacancy"), ("stage", "stage")):
            value = str(context.get(field) or "")
            if re.fullmatch(r"[A-Za-z0-9_.:-]{1,120}", value):
                record.context += f"{label}={value} "
        record.exc_text = None
        record.stack_info = None
        text = super().format(record)
        text = re.sub(r"https?://[^\s<>\"']+", "[url]", text)
        text = re.sub(r"(?i)((?:authorization|cookie)[\"']?\s*[=:]\s*)[^\r\n]+", r"\1[redacted]", text)
        text = re.sub(r"(?i)(bearer\s+)[^\s,;]+", r"\1[redacted]", text)
        text = re.sub(r"(?i)((?:token|password|secret)[\"']?\s*[=:]\s*)[^\s,;]+", r"\1[redacted]", text)
        return text

    def formatException(self, exc_info):
        return f"error_kind={exc_info[0].__name__}"


class PrivateFileHandler(logging.FileHandler):
    def __init__(self, filename, *, channel=None):
        super().__init__(filename, mode="a", encoding="utf-8", delay=True)
        self._failure_reported = False
        self.setFormatter(OperationalFormatter(None if channel else "%(message)s"))
        if channel:
            self.addFilter(ChannelFilter(channel))

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


class NonblockingLogHandler(logging.Handler):
    """One bounded mailbox; only its daemon worker touches logging sinks.

    Full queues drop records and count the loss instead of blocking polling.
    Task-local channel/context is captured before handing records to a thread.
    """
    def __init__(self, handlers, *, capacity=1024):
        if capacity < 1:
            raise ValueError("Logging queue capacity must be positive")
        super().__init__()
        self.handlers = tuple(handlers)
        self.records = queue.Queue(maxsize=capacity)
        self.dropped_records = 0
        self.failed_records = 0
        self._stopping = threading.Event()
        self._worker = threading.Thread(target=self._drain, name="telegram-logging", daemon=True)
        self._worker.start()

    def emit(self, record):
        if self._stopping.is_set():
            return
        prepared = copy.copy(record)
        prepared._private_log_channel = log_channel(record)
        prepared.observation_fields = {**_context(), **getattr(record, "observation_fields", {})}
        try:
            self.records.put_nowait(prepared)
        except queue.Full:
            self.dropped_records += 1

    def _dispatch(self, record):
        # Neither a held sidecar nor an inode lock may strand the worker.
        with lock_wait_timeout(0.1):
            for handler in self.handlers:
                if record.levelno >= handler.level:
                    try:
                        handler.handle(record)
                    except Exception:
                        self.failed_records += 1

    def _drain(self):
        reported_drops = 0
        try:
            while not self._stopping.is_set() or not self.records.empty():
                try:
                    record = self.records.get(timeout=0.05)
                except queue.Empty:
                    continue
                try:
                    drops = self.dropped_records
                    if drops > reported_drops:
                        self._dispatch(logging.LogRecord(
                            "telegram_bot", logging.WARNING, __file__, 0,
                            "Telegram logging queue dropped %s records", (drops - reported_drops,), None,
                        ))
                        reported_drops = drops
                    self._dispatch(record)
                finally:
                    self.records.task_done()
        finally:
            for handler in self.handlers:
                handler.close()

    def close(self):
        self._stopping.set()
        if threading.current_thread() is not self._worker:
            self._worker.join(timeout=0.2)
        super().close()
