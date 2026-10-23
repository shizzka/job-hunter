"""Report parity, incremental I/O and reproducible large-file timings."""
from concurrent.futures import ThreadPoolExecutor
from collections import OrderedDict
from datetime import datetime, timedelta, timezone
import json
import os
import time
from unittest.mock import Mock
from types import SimpleNamespace

import pytest

import analytics
import analytics_read_cache as cache
from tests.direction_view_fakes import two_directions


def write(path, rows, mode='w'):
    with open(path, mode) as stream:
        for row in rows:
            stream.write(json.dumps(row) + '\n')


def decision(**extra):
    return {'event': 'decision', 'created_at': '2026-10-23T08:00:00+00:00',
            'run_id': 'r', 'vacancy_id': '1', 'source': 'hh', 'decision': 'applied_auto', **extra}


@pytest.fixture
def small_cache(monkeypatch):
    monkeypatch.setattr(cache, '_journals', OrderedDict())
    monkeypatch.setattr(cache, '_reports', OrderedDict())
    monkeypatch.setattr(cache, 'MAX_CACHED_ROWS', 4)


def test_row_budget_evicts_lru_and_preserves_report_output(tmp_path, small_cache):
    paths = [tmp_path / str(n) / 'analytics_events.jsonl' for n in range(3)]
    expected = []
    for n, path in enumerate(paths):
        path.parent.mkdir()
        write(path, [decision(vacancy_id=str(n), resume_variant='test'),
                     decision(vacancy_id=str(n), decision='apply_uncertain' if n else 'manual_questions')])
        expected.append(analytics._summarize(events_file=str(path), all_time=True))
    for path, report in zip(paths[:2], expected):
        assert analytics.summarize(events_file=str(path), all_time=True) == report
    first = cache.journal(paths[0])
    first.read(paths[0])  # Refresh LRU even if the file has not changed.
    assert analytics.summarize(events_file=str(paths[2]), all_time=True) == expected[2]
    assert (str(paths[0]), 'summary') in cache._journals
    assert (str(paths[1]), 'summary') not in cache._journals
    assert sum(len(j.rows) + len(j.tail) for j in cache._journals.values()) == 4
    cache._reports.clear()  # Require recomputation from the reloaded projection.
    for path, report in zip(paths, expected):
        assert analytics.summarize(events_file=str(path), all_time=True) == report
        assert sum(len(j.rows) + len(j.tail) for j in cache._journals.values()) <= 4


def test_append_and_partial_tail_obey_budget_without_changing_snapshots(tmp_path, small_cache):
    path = tmp_path / 'analytics_events.jsonl'
    write(path, [decision(vacancy_id=str(n)) for n in range(4)])
    index = cache.journal(path)
    snapshot, _ = index.read(path)
    with path.open('a') as stream:
        stream.write(json.dumps(decision(vacancy_id='tail')))  # Valid incomplete line counts too.
    rows, _ = index.read(path)
    assert len(snapshot) == 4 and len(rows) == 5
    assert not cache._journals  # One oversized journal is never retained.
    assert analytics.summarize(events_file=str(path), all_time=True) == analytics._summarize(events_file=str(path), all_time=True)
    assert not cache._journals


def test_idle_summary_ownership_and_reports_expire_while_active_cache_survives(tmp_path, monkeypatch, small_cache):
    now = [100.0]
    monkeypatch.setattr(cache, 'time', SimpleNamespace(monotonic=lambda: now[0], time=time.time))
    path = tmp_path / 'idle.jsonl'
    write(path, [decision()])
    for tag in ('summary', ('ownership', True)):
        cache.journal(path, tag=tag).read(path)
    cache.memo('idle', lambda: ('old', None))
    now[0] += cache.IDLE_SECONDS - 1
    active = tmp_path / 'active.jsonl'
    write(active, [decision()])
    current = cache.journal(active)
    current.read(active)
    cache.memo('active', lambda: ('active', None))
    assert len(cache._journals) == 3
    now[0] += 1
    assert cache.memo('active', lambda: pytest.fail('Active report recomputed')) == 'active'
    assert list(cache._journals.values()) == [current]
    assert list(cache._reports) == ['active']
    assert analytics.summarize(events_file=str(path), all_time=True) == analytics._summarize(events_file=str(path), all_time=True)


def test_summary_cache_is_instant_copied_and_scoped(tmp_path, monkeypatch):
    two_directions(tmp_path)
    path = tmp_path / 'analytics_events.jsonl'
    write(path, [decision(direction='qa'), decision(direction='electric', vacancy_id='2')])
    compute = Mock(wraps=analytics._summarize)
    monkeypatch.setattr(analytics, '_summarize', compute)
    def summary(scope):
        return analytics.summarize(events_file=str(path), all_time=True, scope=scope)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = list(pool.map(summary, ['qa', 'qa']))
    assert first == second and first['auto_applied'] == 1
    assert compute.call_count == 1
    first['by_source']['hh']['decisions'] = 999
    assert summary('qa') == second
    assert summary('*')['auto_applied'] == 2
    assert summary('electric')['auto_applied'] == 1
    assert compute.call_count == 3


def test_append_reads_only_new_bytes_and_preserves_late_uncertainty(tmp_path):
    path = tmp_path / 'analytics_events.jsonl'
    write(path, [decision(), decision(vacancy_id='2', decision='skipped_low_score')])
    first = analytics.summarize(events_file=str(path), all_time=True)
    index = cache.journal(path)
    before = index.bytes_read
    write(path, [decision(decision='apply_uncertain')], 'a')
    appended = path.stat().st_size - before
    result = analytics.summarize(events_file=str(path), all_time=True)
    assert index.bytes_read - before == appended
    assert result == analytics._summarize(events_file=str(path), all_time=True)
    assert first['auto_applied'] == 1 and result['auto_applied'] == 0 and result['uncertain'] == 1


@pytest.mark.parametrize('change', ['rewrite', 'replace', 'truncate', 'mtime', 'delete'])
def test_file_changes_invalidate_reports_and_projection(tmp_path, monkeypatch, change):
    path = tmp_path / 'analytics_events.jsonl'
    write(path, [decision()])
    analytics.summarize(events_file=str(path), all_time=True)
    compute = Mock(wraps=analytics._summarize)
    monkeypatch.setattr(analytics, '_summarize', compute)
    if change == 'rewrite':
        # Same length replacement must invalidate by mtime/ctime.
        previous = path.stat()
        path.write_bytes(path.read_bytes().replace(b'applied_auto', b'apply_failed'))
        # WSL filesystems may coalesce two adjacent write timestamps.
        os.utime(path, ns=(previous.st_atime_ns, previous.st_mtime_ns + 1_000_000_000))
    elif change == 'replace':
        other = tmp_path / 'other'
        write(other, [decision(decision='manual_questions')])
        os.replace(other, path)
    elif change == 'truncate':
        path.write_text('')
    elif change == 'mtime':
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1))
    else:
        path.unlink()
    assert analytics.summarize(events_file=str(path), all_time=True) == analytics._summarize(events_file=str(path), all_time=True)
    assert compute.call_count == 2  # cached miss + explicit uncached reference


def test_partial_tail_is_reparsed_without_duplicate_events(tmp_path):
    path = tmp_path / 'analytics_events.jsonl'
    path.write_text(json.dumps(decision()))
    assert analytics.summarize(events_file=str(path), all_time=True)['events'] == 1
    with path.open('a') as stream:
        stream.write('\n' + json.dumps(decision(vacancy_id='2'))[:40])
    assert analytics.summarize(events_file=str(path), all_time=True)['events'] == 1
    with path.open('a') as stream:
        stream.write(json.dumps(decision(vacancy_id='2'))[40:] + '\n')
    assert analytics.summarize(events_file=str(path), all_time=True)['events'] == 2


def test_moving_window_expires_without_file_change(tmp_path, monkeypatch):
    path = tmp_path / 'events.jsonl'
    now = datetime(2026, 10, 24, 7, 59, 59, tzinfo=timezone.utc)
    monkeypatch.setattr(analytics, '_now', lambda: now)
    write(path, [decision()])
    assert analytics.summarize(days=1, events_file=str(path))['decisions'] == 1
    now += timedelta(seconds=2)
    assert analytics.summarize(days=1, events_file=str(path))['decisions'] == 0


def test_direction_and_snapshot_metadata_invalidate_cache(tmp_path):
    import directions
    two_directions(tmp_path)
    path = tmp_path / 'analytics_events.jsonl'
    write(path, [decision()])
    before = analytics.summarize(events_file=str(path), all_time=True, scope='qa')
    # New ownership evidence must apply to old status records, too.
    write(path, [dict(event='negotiation_status', source='hh', vacancy_id='x',
                     created_at='2026-10-23T08:00:00+00:00', status_bucket='positive')], 'a')
    summary = analytics.summarize(events_file=str(path), all_time=True, scope='qa')
    write(tmp_path / 'negotiation_snapshots.jsonl', [dict(vacancyId='x', direction='qa')])
    assert analytics.summarize(events_file=str(path), all_time=True, scope='qa')['positive_statuses'] == summary['positive_statuses'] + 1
    active = directions.load_direction(tmp_path, 'qa')
    active['title'] = 'Renamed'
    directions.save_direction(tmp_path, active, texts={})
    assert analytics.summarize(events_file=str(path), all_time=True, scope='qa')['direction_title'] == 'Renamed'


def test_large_synthetic_summary_before_after(tmp_path, capsys):
    """233 MB includes realistic bulky diagnostic payloads, never production data.

    Timing is reported; structural I/O assertions avoid machine-speed thresholds.
    Run with pytest -s to retain the benchmark line.
    """
    path = tmp_path / 'analytics_events.jsonl'
    row = json.dumps(decision(details='synthetic ' * 4096)) + '\n'
    count = (233_000_000 + len(row) - 1) // len(row)
    with path.open('w') as stream:
        for _ in range(count):
            stream.write(row)
    def timed(fn):
        started = time.perf_counter()
        result = fn()
        return result, time.perf_counter() - started
    before, old_seconds = timed(lambda: analytics._summarize(events_file=str(path), all_time=True))
    first, cold_seconds = timed(lambda: analytics.summarize(events_file=str(path), all_time=True))
    index = cache.journal(path)
    read_before = index.bytes_read
    hit, hit_seconds = timed(lambda: analytics.summarize(events_file=str(path), all_time=True))
    assert first == hit == before
    assert index.bytes_read == read_before == path.stat().st_size
    assert all('details' not in r for r in index.rows)
    write(path, [decision(vacancy_id='new')], 'a')
    appended_bytes = path.stat().st_size - read_before
    appended, append_seconds = timed(lambda: analytics.summarize(events_file=str(path), all_time=True))
    assert appended['events'] == first['events'] + 1
    assert index.bytes_read - read_before == appended_bytes
    with capsys.disabled():
        print(f'BENCH summary {read_before / 1_000_000:.1f} MB / {count} events: '
              f'before={old_seconds:.4f}s cold={cold_seconds:.4f}s cached={hit_seconds:.6f}s '
              f'append={append_seconds:.4f}s; append_read={appended_bytes} bytes; cached_read=0 bytes')


def test_projection_preserves_conversion_dimensions_and_lifecycle_report(tmp_path):
    from run_summary import build_run_summary
    from tests.test_run_summary import finished_run
    path = tmp_path / 'analytics_events.jsonl'
    write(path, [decision(resume_variant='control', search_query='query', cover_style='short',
                          retry_reason='manual', cluster='qa'),
                 decision(vacancy_id='2', decision='manual_questions', resume_variant='test'),
                 dict(event='negotiation_status', vacancy_id='1', source='hh',
                      created_at='2026-10-23T09:00:00+00:00', status_bucket='positive',
                      status_detail_bucket='interview')])
    assert analytics.summarize(events_file=str(path), all_time=True) == analytics._summarize(events_file=str(path), all_time=True)
    lifecycle = {**finished_run(), 'event': 'search_finished', 'created_at': '2026-10-23T09:00:00+00:00'}
    write(path, [lifecycle])
    result = analytics.get_run_summary(events_file=str(path), history_file=str(tmp_path / 'missing'))
    expected = build_run_summary({**lifecycle, 'status': 'finished', 'started_at': lifecycle.get('started_at')}, [lifecycle])
    assert result['counters'] == expected['counters']
    assert result['duration_seconds'] == expected['duration_seconds']
    assert result['started_at'] == expected['started_at']
    assert result['finished_at'] == expected['finished_at']
