"""Bounded, nonblocking handoff to an independent audit process."""
from __future__ import annotations

import asyncio
import logging
import multiprocessing
from queue import Full
import threading

log = logging.getLogger('run_auditor')


async def _process(job):
    import notifier
    import run_auditor
    try:
        if job['kind'] == 'daily':
            await run_auditor.audit_daily_summary(**job['destination'])
        else:
            await run_auditor.audit_completed_run(job['run_id'], **job['destination'], offload=False)
    except Exception as exc:
        log.warning('Audit worker failed: error_kind=%s', type(exc).__name__)
    finally:
        # Each job has its own event loop; never reuse its HTTP session later.
        await notifier.close_session()


def _serve(queue):
    while True:
        asyncio.run(_process(queue.get()))


class AuditWorker:
    def __init__(self, capacity=64):
        context = multiprocessing.get_context('spawn')
        self.queue = context.Queue(maxsize=capacity)
        # A full IPC pipe must not delay parent interpreter shutdown either.
        self.queue.cancel_join_thread()
        self.process = context.Process(target=_serve, args=(self.queue,), daemon=True,
                                       name='run-auditor')
        self.process.start()

    def submit(self, job):
        try:
            self.queue.put_nowait(job)
            return True
        except Full:
            log.warning('Audit handoff unavailable: queue_full')
            return False

    def close(self):
        """Used for explicit lifecycle cleanup; never awaited by a run finalizer."""
        self.process.terminate()
        self.process.join(timeout=2)
        self.queue.close()


_worker = None
_start_lock = threading.Lock()


def submit(job):
    global _worker
    with _start_lock:
        if _worker is None or not _worker.process.is_alive():
            _worker = AuditWorker()
        return _worker.submit(job)
