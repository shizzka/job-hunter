"""Supervise Telegram work while independent reads leave the polling FIFO."""
from __future__ import annotations

import asyncio
import logging
import time

from telegram_app.api import telegram_error_summary
from state_store.locking import StateLockBusy, nonblocking_state_locks

log = logging.getLogger(__name__)
STATE_BUSY = "Бот сейчас занят записью данных, повторите через пару секунд"
UPDATE_TIMEOUT = "Не успел посчитать за 30 секунд — попробуйте через минуту"


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
            # 24.10: a request cut at the deadline used to end in silence; tell the user it was not lost
            await self._reply_direct(args[0] if args and isinstance(args[0], dict) else {}, UPDATE_TIMEOUT)
        except StateLockBusy:
            outcome = "busy"
            log.error("Telegram work failed update_id=%s handler=%s error=StateLockBusy", update_id, name)
            # Reply directly via Telegram: resolving a user/menu here could
            # hit the same lock again. Never replay an uncertain mutation.
            await self._reply_direct(args[0] if args and isinstance(args[0], dict) else {}, STATE_BUSY)
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

    async def _reply_direct(self, update, text):
        """Short notice straight to the chat of an update the bot could not finish."""
        callback = update.get("callback_query") or {}
        try:
            async with asyncio.timeout(self.UPDATE_TIMEOUT_SECONDS):
                if callback:
                    await self._answer_callback_query(str(callback.get("id") or ""), text)
                # The router may already have acknowledged the callback; also
                # send a chat reply so a second callback answer cannot fail silently.
                message = callback.get("message") if callback else update.get("message")
                chat_id = ((message or {}).get("chat") or {}).get("id")
                if chat_id:
                    await self._send_text(chat_id, text)
        except Exception as exc:
            log.error("Telegram direct reply failed error=%s", telegram_error_summary(exc))

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

    def _schedule_daily_summary(self):
        for name in ("_daily_summary_task", "_daily_summary_report_task"):
            task = getattr(self, name, None)
            if task is not None and not task.done():
                return
        self._daily_summary_task = asyncio.create_task(self._run_foreground(
            self._maybe_send_daily_summary, foreground=False))

    async def _shutdown_read_updates(self):
        tasks = list(getattr(self, "_read_update_tasks", ()))
        daily = getattr(self, "_daily_summary_task", None)
        if daily is not None:
            tasks.append(daily)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
