"""Scoped lock deadlines for synchronous stores used by asynchronous callers."""
import contextlib
import asyncio
from contextvars import ContextVar
from functools import wraps
import fcntl
import time

_wait_seconds = ContextVar("state_lock_wait_seconds", default=None)
BOT_WORKER_LOCK_WAIT = 0.5


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
        # to_thread and child tasks copy this budget. acquire_flock clamps it
        # to zero on the event loop, while workers tolerate brief contention.
        with lock_wait_timeout(BOT_WORKER_LOCK_WAIT):
            return await fn(*args, **kwargs)
    return wrapped


def acquire_flock(fd, operation):
    wait = _wait_seconds.get()
    if wait is None:
        fcntl.flock(fd, operation)
        return
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        wait = 0  # Scoped bot tasks, including children, never sleep on flock.
    deadline = time.monotonic() + wait
    while True:
        try:
            fcntl.flock(fd, operation | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise StateLockBusy("State lock is busy") from None
            # Only worker threads can reach this sleep.
            time.sleep(min(remaining, 0.01))
