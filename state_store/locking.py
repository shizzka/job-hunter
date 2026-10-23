"""Scoped lock deadlines for synchronous stores used by asynchronous callers."""
import contextlib
from contextvars import ContextVar
from functools import wraps
import fcntl
import time

_wait_seconds = ContextVar("state_lock_wait_seconds", default=None)


class StateLockBusy(RuntimeError):
    """No state was accessed under a contended lock; callers must fail closed."""


@contextlib.contextmanager
def lock_wait_timeout(seconds):
    token = _wait_seconds.set(seconds)
    try:
        yield
    finally:
        _wait_seconds.reset(token)


def nonblocking_state_locks(fn):
    """Keep synchronous store APIs fail-fast in the bot's event-loop tasks.

    Do not replay handlers or leave state-writing threads behind on timeout.
    Other processes retain their existing transactional lock behavior.
    """
    @wraps(fn)
    async def wrapped(*args, **kwargs):
        with lock_wait_timeout(0):
            return await fn(*args, **kwargs)
    return wrapped


def acquire_flock(fd, operation):
    wait = _wait_seconds.get()
    if wait is None:
        fcntl.flock(fd, operation)
        return
    deadline = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(fd, operation | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise StateLockBusy("State lock is busy") from None
            # A positive budget is for the logging worker only. Bot tasks use
            # zero and never sleep while acquiring a synchronous state lock.
            time.sleep(min(remaining, 0.01))
