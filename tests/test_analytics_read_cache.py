"""Report parity, incremental I/O and reproducible large-file timings."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import json
import os
import time
from unittest.mock import Mock

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
        path.write_bytes(path.read_bytes().replace(b'applied_auto', b'apply_failed'))
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
