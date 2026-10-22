"""Real worker process, synthetic journals and no configured network target."""
import asyncio
import json
from pathlib import Path
from queue import Queue
import time
from unittest.mock import AsyncMock

import pytest

import agent
import audit_worker
import notifier
import run_auditor
from state_store.json_store import file_lock


def eventually(predicate):
    deadline = time.monotonic() + 15
    while not predicate():
        assert time.monotonic() < deadline, 'audit worker did not finish'
        time.sleep(.02)


@pytest.mark.parametrize('lock_kind', ['input', 'output'])
def test_run_and_event_loop_finish_while_worker_waits_for_filesystem(tmp_path, monkeypatch, lock_kind):
    events = tmp_path / 'analytics_events.jsonl'
    events.write_text(json.dumps({'event': 'search_finished', 'run_id': 'run',
                                 'finished_at': '2026-10-22T10:00:00+03:00'}) + '\n')
    journal = tmp_path / run_auditor.JOURNAL
    worker = audit_worker.AuditWorker()
    monkeypatch.setattr(audit_worker, 'submit', worker.submit)
    monkeypatch.setattr(notifier, 'capture_audit_owner_target', lambda: ('', (), ''))
    destination = {'home': str(tmp_path), 'profile': 'synthetic'}
    # Hold a real lock that only the child will read; returning from asyncio.run
    # also proves no executor shutdown waits for the audit.
    try:
        if lock_kind == 'input':
            import fcntl
            stream = events.open()
            fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
            try:
                asyncio.run(agent._audit_completed_run('run', destination))
                eventually(lambda: worker.queue.qsize() == 0)
                assert worker.process.is_alive() and not journal.exists()
            finally:
                stream.close()
        else:
            with file_lock(tmp_path / 'audit_transaction'):
                asyncio.run(agent._audit_completed_run('run', destination))
                eventually(lambda: worker.queue.qsize() == 0)
                assert worker.process.is_alive() and not journal.exists()
        eventually(lambda: journal.exists() and 'audit_completed' in journal.read_text())
    finally:
        worker.close()


def test_full_handoff_is_bounded_and_does_not_wait(caplog):
    worker = audit_worker.AuditWorker.__new__(audit_worker.AuditWorker)
    worker.queue = Queue(maxsize=1)
    assert worker.submit({'run_id': 'one'})
    assert worker.submit({'run_id': 'two'}) is False
    assert worker.queue.qsize() == 1 and 'queue_full' in caplog.text


def test_handoff_captures_owner_target_without_audit_reads(monkeypatch, tmp_path):
    jobs = []
    monkeypatch.setattr(audit_worker, 'submit', lambda job: jobs.append(job) or True)
    monkeypatch.setattr(notifier, 'capture_audit_owner_target', lambda: ('control', (7,), ''))
    monkeypatch.setattr(run_auditor, '_load', lambda *args: pytest.fail('audit read on caller'))
    monkeypatch.setattr(run_auditor, '_records', lambda *args: pytest.fail('audit lock on caller'))
    assert run_auditor.enqueue_completed_run('run', home=str(tmp_path), profile='first')
    assert jobs == [{'kind': 'run', 'run_id': 'run', 'destination': {
        'home': str(tmp_path), 'profile': 'first', 'target': ('control', (7,), '')}}]


def test_worker_owns_delivery_loop_and_closes_its_session(monkeypatch, tmp_path):
    audit = AsyncMock()
    close = AsyncMock()
    monkeypatch.setattr(run_auditor, 'audit_completed_run', audit)
    monkeypatch.setattr(notifier, 'close_session', close)
    destination = {'home': str(tmp_path), 'profile': 'synthetic', 'target': ('', (), '')}
    asyncio.run(audit_worker._process({'kind': 'run', 'run_id': 'run', 'destination': destination}))
    audit.assert_awaited_once_with('run', **destination, offload=False)
    close.assert_awaited_once()
