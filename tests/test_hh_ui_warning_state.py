"""Claim durability and contention without browser/network/production state."""
import asyncio
import json
import multiprocessing
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from hh.ui import HHUIGuard, HHUnexpectedUI
from state_store.hh_ui import HHUIWarnings
from state_store import json_store
from tests.test_hh_ui_safety import FakePage


def claim_worker(home, barrier, results):
    warnings = HHUIWarnings(home, clock=lambda: 1000)
    barrier.wait(timeout=10)
    results.put(bool(warnings.claim('a' * 64)))


def test_four_processes_get_only_one_warning_claim(tmp_path):
    ctx = multiprocessing.get_context('spawn')
    barrier, results = ctx.Barrier(4), ctx.Queue()
    workers = [ctx.Process(target=claim_worker, args=(str(tmp_path), barrier, results)) for _ in range(4)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join(timeout=15)
        assert not worker.is_alive() and worker.exitcode == 0
    assert sum(results.get(timeout=5) for _ in workers) == 1
    results.close()


def test_thirty_concurrent_sessions_make_one_delivery_and_clean_every_image(tmp_path):
    notify = AsyncMock(return_value=True)
    async def attempt():
        with pytest.raises(HHUnexpectedUI):
            await HHUIGuard(tmp_path, notify=notify, clock=lambda: 1000).ensure(FakePage(), 'synthetic')
    async def run():
        await asyncio.gather(*(attempt() for _ in range(30)))
    asyncio.run(run())
    notify.assert_awaited_once()
    assert not list(tmp_path.glob('.hh-ui-*'))


@pytest.mark.parametrize('content', [b'{', b'[]', b'\xff', b'{"alerts":[]}', b'{"alerts":{"invalid":null}}',
    json.dumps({'alerts': {'a' * 64: {'attempt_id': 'a', 'status': ['sent'], 'attempt_at': 1, 'sent_at': 1}}}).encode(),
    json.dumps({'alerts': {'a' * 64: {'attempt_id': 'a', 'status': 'sent', 'attempt_at': True, 'sent_at': 1}}}).encode(),
    json.dumps({'alerts': {'a' * 64: {'attempt_id': 'a', 'status': 'sent', 'attempt_at': 1, 'sent_at': float('nan')}}}).encode(),
])
def test_corrupt_schema_retained_after_repeated_claims_and_completions(tmp_path, content):
    warnings = HHUIWarnings(tmp_path)
    warnings.store.path.write_bytes(content)
    for _ in range(2):
        for action in (lambda: warnings.claim('a' * 64), lambda: warnings.finish('a' * 64, 'old', 'sent')):
            with pytest.raises(RuntimeError, match='restore'):
                action()
            assert warnings.store.path.read_bytes() == content


@pytest.mark.parametrize('failure', ['replace', 'fsync', 'read', 'serialization'])
def test_storage_failure_preserves_old_records(tmp_path, monkeypatch, failure):
    warnings = HHUIWarnings(tmp_path, clock=lambda: 1000)
    warnings.claim('a' * 64)
    before = warnings.store.path.read_bytes()
    if failure == 'read':
        original = Path.open
        def denied(path, *args, **kwargs):
            if path == warnings.store.path:
                raise PermissionError('Synthetic denied read')
            return original(path, *args, **kwargs)
        monkeypatch.setattr(Path, 'open', denied)
        error = PermissionError
    elif failure != 'serialization':
        def fail(*args, **kwargs):
            raise OSError('Synthetic storage failure')
        monkeypatch.setattr(json_store.os, failure, fail)
        error = OSError
    else:
        error = TypeError
    with pytest.raises(error):
        if failure == 'serialization':
            warnings.store.update(lambda state: {**state, 'unsupported': object()})
        else:
            warnings.claim('b' * 64)
    monkeypatch.undo()
    assert warnings.store.path.read_bytes() == before
    assert not list(tmp_path.glob('*.tmp'))


@pytest.mark.parametrize('now', [True, float('inf'), float('nan'), 'bad'])
def test_invalid_clock_blocks_before_state_creation(tmp_path, now):
    with pytest.raises(ValueError):
        HHUIWarnings(tmp_path, clock=lambda: now).claim('a' * 64)
    assert not (tmp_path / 'hh_ui_warnings.json').exists()


def test_profiles_metadata_and_noop_are_preserved(tmp_path):
    a = HHUIWarnings(tmp_path / 'a', clock=lambda: 1000)
    b = HHUIWarnings(tmp_path / 'b', clock=lambda: 1000)
    a.store.save({'alerts': {}, 'metadata': {'retained': True}})
    a.claim('a' * 64)
    a.claim('b' * 64)
    b.claim('a' * 64)
    before = a.store.path.read_bytes()
    inode = a.store.path.stat().st_ino
    assert a.claim('a' * 64) is None
    a.finish('a' * 64, 'wrong-owner', 'sent')
    assert a.store.path.read_bytes() == before and a.store.path.stat().st_ino == inode
    assert a.store.path.stat().st_mode & 0o777 == 0o600
    assert a.store.load()['metadata'] == {'retained': True}
    assert len(a.store.load()['alerts']) == 2 and len(b.store.load()['alerts']) == 1
