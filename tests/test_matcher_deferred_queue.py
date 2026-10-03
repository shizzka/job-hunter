"""Protected per-profile retry queue: corruption, failures and contention."""
import json
import multiprocessing
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor

import pytest

from state_store.matcher_deferred import MatcherDeferredQueue
from state_store import json_store


def vacancy(number=1, source='hh'):
    return {'id': str(number), 'source': source, 'title': 'Synthetic vacancy', 'company': 'Synthetic employer'}


def worker(home, lane, barrier):
    queue = MatcherDeferredQueue(home, clock=lambda: 1000)
    barrier.wait(timeout=10)
    for number in range(10):
        queue.defer(vacancy(f'{lane}-{number}'), 'Synthetic details', 'llm_limits_exhausted')


@pytest.fixture
def queue(tmp_path):
    clock = [1000]
    repository = MatcherDeferredQueue(tmp_path, cooldown_seconds=300, clock=lambda: clock[0])
    return repository, clock


def test_cooldown_is_enforced_for_fresh_and_saved_discovery(queue):
    repository, clock = queue
    repository.defer(vacancy(), 'Saved details', 'llm_limits_exhausted')
    assert repository.merge_ready([vacancy()], {'hh'}) == []
    clock[0] += 299
    assert repository.merge_ready([], {'hh'}) == []
    clock[0] += 1
    pending = repository.merge_ready([vacancy(), vacancy()], {'hh'})
    assert len(pending) == 1
    assert pending[0]['details'] == 'Saved details'
    assert pending[0]['_matcher_deferred_revision']
    assert len(repository.merge_ready([], {'hh'})) == 1


def test_disabled_source_is_retained_not_evaluated_or_deleted(queue):
    repository, clock = queue
    repository.defer(vacancy(), 'details', 'llm_error')
    clock[0] += 300
    assert repository.merge_ready([vacancy()], {'habr'}) == []
    assert len(repository.store.load()['items']) == 1
    assert len(repository.merge_ready([], {'hh'})) == 1


def test_queue_does_not_expire_unscored_vacancy(queue):
    repository, clock = queue
    repository.defer(vacancy(), 'details', 'llm_error')
    clock[0] += 20 * 365 * 86400
    assert len(repository.merge_ready([], {'hh'})) == 1


def test_resolve_is_revision_checked_and_preserves_newer_deferral(queue):
    repository, clock = queue
    repository.defer(vacancy(), 'old details', 'llm_error')
    clock[0] += 300
    old = repository.merge_ready([], {'hh'})[0]
    repository.defer(vacancy(), 'new details', 'llm_error')
    repository.resolve(old)
    remaining = repository.store.load()['items']['hh:1']
    assert remaining['details'] == 'new details'
    assert remaining['attempts'] == 2
    repository.resolve(vacancy())
    assert repository.store.load()['items']['hh:1']['revision'] == remaining['revision']
    clock[0] += 300
    repository.resolve(repository.merge_ready([], {'hh'})[0])
    assert repository.store.load()['items'] == {}


def test_new_candidate_does_not_delete_someone_elses_deferred_record(queue):
    repository, _ = queue
    repository.defer(vacancy(), 'details', 'llm_error')
    before = repository.path.read_bytes()
    repository.resolve(vacancy())
    assert repository.path.read_bytes() == before


def test_metadata_other_vacancies_and_sources_are_preserved(queue):
    repository, _ = queue
    repository.store.save({'items': {}, 'metadata': {'preserved': True}})
    repository.defer(vacancy(1, 'hh'), 'HH', 'llm_error')
    repository.defer(vacancy(1, 'habr'), 'Habr', 'llm_error')
    state = repository.store.load()
    state['items']['hh:1']['metadata'] = {'preserved': True}
    repository.store.save(state)
    repository.defer(vacancy(1, 'hh'), '', 'llm_error')
    state = repository.store.load()
    assert state['metadata'] == {'preserved': True}
    assert state['items']['hh:1']['metadata'] == {'preserved': True}
    assert state['items']['hh:1']['details'] == 'HH'
    assert state['items']['habr:1']['details'] == 'Habr'


@pytest.mark.parametrize('content', [b'{', b'[]', b'\xff', b'{"items":[]}', b'{"items":{"hh:1":null}}',
    b'{"items":{"hh:1":{"vacancy":{"id":"1"},"revision":"r","attempts":1,"details":"d","error_kind":"e","updated_at":1,"next_attempt_at":NaN}}}'])
def test_invalid_state_is_preserved_across_repeated_reads_and_writes(queue, content):
    repository, _ = queue
    repository.path.write_bytes(content)
    for _ in range(2):
        for operation in (lambda: repository.merge_ready([], {'hh'}), lambda: repository.defer(vacancy(), '', 'llm_error'), lambda: repository.resolve(vacancy())):
            with pytest.raises(RuntimeError, match='restore'):
                operation()
            assert repository.path.read_bytes() == content


@pytest.mark.parametrize('failure', ['replace', 'fsync', 'serialization', 'read'])
def test_write_and_read_failures_preserve_previous_queue(queue, monkeypatch, failure):
    repository, _ = queue
    repository.defer(vacancy(), 'details', 'llm_error')
    before = repository.path.read_bytes()
    new = vacancy(2)
    if failure == 'serialization':
        new['unsupported'] = object()
        exception = TypeError
    elif failure == 'read':
        original = Path.open

        def denied(path, *args, **kwargs):
            if path == repository.path:
                raise PermissionError('Synthetic read denial')
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, 'open', denied)
        exception = PermissionError
    else:
        def fail(*args, **kwargs):
            raise OSError('Synthetic storage failure')
        monkeypatch.setattr(json_store.os, failure, fail)
        exception = OSError
    with pytest.raises(exception):
        repository.defer(new, 'details', 'llm_error')
    monkeypatch.undo()
    assert repository.path.read_bytes() == before
    assert not list(repository.path.parent.glob('*.tmp'))


def test_parallel_threads_keep_all_records_and_retry_counts(queue):
    repository, _ = queue
    def save(index):
        repository.defer(vacancy(index), 'details', 'llm_error')
        repository.defer(vacancy(99), 'details', 'llm_error')
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(save, range(30)))
    items = repository.store.load()['items']
    assert len(items) == 31
    assert items['hh:99']['attempts'] == 30


def test_parallel_processes_keep_all_records(queue):
    repository, _ = queue
    ctx = multiprocessing.get_context('spawn')
    barrier = ctx.Barrier(4)
    processes = [ctx.Process(target=worker, args=(str(repository.path.parent), lane, barrier)) for lane in range(4)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)
        assert not process.is_alive()
        assert process.exitcode == 0
    assert len(repository.store.load()['items']) == 40


@pytest.mark.parametrize('invalid', [float('nan'), float('inf'), True, 'bad'])
def test_invalid_clock_cannot_create_corrupt_queue(tmp_path, invalid):
    repository = MatcherDeferredQueue(tmp_path, clock=lambda: invalid)
    with pytest.raises(ValueError):
        repository.defer(vacancy(), 'details', 'llm_error')
    assert not repository.path.exists()
    with pytest.raises(ValueError):
        repository.merge_ready([], {'hh'})


@pytest.mark.parametrize('bad_vacancy', [{'id': ['1'], 'source': 'hh'}, {'id': '1', 'source': ['hh']}])
def test_invalid_vacancy_schema_is_retained_not_reset(queue, bad_vacancy):
    repository, _ = queue
    repository.defer(vacancy(), 'details', 'llm_error')
    state = json.loads(repository.path.read_text())
    state['items']['hh:1']['vacancy'] = bad_vacancy
    before = json.dumps(state).encode()
    repository.path.write_bytes(before)
    with pytest.raises(RuntimeError, match='restore'):
        repository.merge_ready([], {'hh'})
    assert repository.path.read_bytes() == before


def test_profiles_are_isolated_and_paths_are_captured(tmp_path):
    a = MatcherDeferredQueue(tmp_path / 'a', clock=lambda: 1)
    b = MatcherDeferredQueue(tmp_path / 'b', clock=lambda: 1)
    a.defer(vacancy(), 'a details', 'llm_error')
    b.defer(vacancy(), 'b details', 'llm_error')
    assert a.store.load()['items']['hh:1']['details'] == 'a details'
    assert b.store.load()['items']['hh:1']['details'] == 'b details'
    assert a.path.stat().st_mode & 0o777 == 0o600
