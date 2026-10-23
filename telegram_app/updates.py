"""Supervise Telegram work while independent reads leave the polling FIFO."""
from __future__ import annotations

import asyncio
import logging
import time

from telegram_app.api import telegram_error_summary
from state_store.locking import nonblocking_state_locks

log = logging.getLogger(__name__)


def _await_chain(task):
    """Function names only: frames/arguments can contain auth codes and tokens."""
    names = []
    coroutine = task.get_coro()
    for _ in range(32):
        code = getattr(coroutine, "cr_code", None) or getattr(coroutine, "gi_code", None)
        if code is None:
            break
        names.append(code.co_name)
        coroutine = getattr(coroutine, "cr_await", None) or getattr(coroutine, "gi_yieldfrom", None)
    return ">".join(names)


class TelegramUpdateSupervisor:
    UPDATE_TIMEOUT_SECONDS = 30.0
    SLOW_UPDATE_SECONDS = 10.0

    @nonblocking_state_locks
    async def _run_foreground(self, handler, *args, update_id=None, handler_name=None, foreground=True):
        """Cancel a slow request and contain its failure before advancing FIFO.

        Child tasks inherit ContextVars; the original access checks still run.
        Background commands launched by a handler retain their own lifecycle.
        """
        name = handler_name or handler.__name__
        started = time.monotonic()
        task = asyncio.create_task(handler(*args))
        if foreground:
            self._foreground_task = task
        outcome = "ok"
        log.info("Telegram work started update_id=%s handler=%s", update_id, name)

        def slow():
            log.warning(
                "Telegram work slow update_id=%s handler=%s duration=%.3fs awaiting=%s",
                update_id, name, time.monotonic() - started, _await_chain(task),
            )

        timer = asyncio.get_running_loop().call_later(self.SLOW_UPDATE_SECONDS, slow)
        try:
            await asyncio.wait_for(task, timeout=self.UPDATE_TIMEOUT_SECONDS)
        except asyncio.TimeoutError:
            outcome = "timeout"
            log.error("Telegram work timeout update_id=%s handler=%s duration=%.3fs",
                      update_id, name, time.monotonic() - started)
        except asyncio.CancelledError:
            outcome = "cancelled"
            # Preserve owner cancellation/shutdown, but contain a handler which
            # cancels itself instead of terminating the sole update consumer.
            if asyncio.current_task().cancelling():
                raise
            if not self._stop_event.is_set():
                log.error("Telegram work cancelled update_id=%s handler=%s duration=%.3fs",
                          update_id, name, time.monotonic() - started)
        except Exception as exc:
            outcome = "error"
            log.error("Telegram work failed update_id=%s handler=%s duration=%.3fs error=%s",
                      update_id, name, time.monotonic() - started, telegram_error_summary(exc))
        finally:
            timer.cancel()
            if foreground:
                self._foreground_task = None
            log.info("Telegram work finished update_id=%s handler=%s duration=%.3fs outcome=%s",
                     update_id, name, time.monotonic() - started, outcome)

    async def _run_update(self, update):
        handler = "_handle_callback_query" if update.get("callback_query") else "_handle_update"
        from telegram_app.guards import background_read
        if background_read(update):
            tasks = self.__dict__.setdefault("_read_update_tasks", set())
            task = asyncio.create_task(self._run_foreground(
                self._handle_update, update, update_id=update.get("update_id"),
                handler_name=handler, foreground=False))
            tasks.add(task)
            task.add_done_callback(tasks.discard)
            await asyncio.sleep(0)
            return
        await self._run_foreground(self._handle_update, update, update_id=update.get("update_id"),
                                   handler_name=handler)

    async def _shutdown_read_updates(self):
        tasks = list(getattr(self, "_read_update_tasks", ()))
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
