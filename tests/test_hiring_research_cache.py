"""Incremental research reports retain semantics and expire at time boundaries."""
from datetime import datetime, timedelta, timezone
import json
from unittest.mock import Mock

import analytics
import analytics_read_cache as cache
import hiring_research as research
from tests.test_hiring_research import apply, status


def write(path, rows, mode='w'):
    with path.open(mode) as stream:
        for row in rows:
            stream.write(json.dumps(row) + '\n')


def test_research_cache_reads_zero_unchanged_bytes_and_only_append_afterwards(tmp_path, monkeypatch):
    path = tmp_path / 'events.jsonl'
    rows = [apply('1', submission_mode='manual', cover_letter_status='confirmed', resume_variant='control',
                  cover_letter_resume_sha256='a' * 64, details='Synthetic large diagnostic ' * 3000),
            apply('2', historical=True),
            {**status('2026-09-28T10:01:00+00:00', 'pending', status_detail_bucket='pending_viewed'),
             'created_at': None, 'observed_at_utc': '2026-09-28T10:01:00+00:00'},
            {'event': 'screening_observation', 'recorded_at_utc': '2026-09-28T10:02:00+00:00',
             'source': 'hh', 'url': 'https://hh.ru/vacancy/1', 'screening_type': 'hh_system_ai'},
            {'event': 'relevance_verification', 'created_at': '2026-09-28T10:03:00+00:00',
             'source': 'hh', 'vacancy_id': '1', 'verdict': 'review'}]
    write(path, rows)
    now = datetime(2026, 9, 28, 15, tzinfo=timezone.utc)
    baseline = research._summarize(str(path), now=now)
    compute = Mock(wraps=research._summarize)
    monkeypatch.setattr(research, '_summarize', compute)
    first = research.summarize(str(path), now=now)
    index = cache.journal(path)  # Cohorts now share the report journal and its offset.
    assert first == baseline
    before = index.bytes_read
    assert before == path.stat().st_size
    # The production cache path must never call the full-journal reader.
    monkeypatch.setattr(analytics, '_iter_events', Mock(side_effect=AssertionError('Full journal read')))
    assert research.summarize(str(path), now=now) == first
    assert index.bytes_read == before and compute.call_count == 1
    first['groups']['mode']['manual']['total'] = 999
    assert research.summarize(str(path), now=now) == baseline
    write(path, [status('2026-09-28T10:04:00+00:00', 'rejected')], 'a')
    result = research.summarize(str(path), now=now)
    assert result['outcomes']['rejected'] == 1
    assert result['rejection_delays']['<5 мин'] == 1
    assert result['rejection_views']['просмотр замечен'] == 1
    assert compute.call_count == 2
    assert index.bytes_read - before == path.stat().st_size - before
    assert all('details' not in row for row in index.rows)


def clock(monkeypatch, now):
    current = [now]
    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0].astimezone(tz) if tz is not None else current[0].replace(tzinfo=None)
    monkeypatch.setattr(research, 'datetime', Clock)
    return current


def test_implicit_research_time_cache_expires_when_future_observation_arrives(tmp_path, monkeypatch):
    path = tmp_path / 'events.jsonl'
    write(path, [apply(), status('2026-09-28T10:04:00+00:00', 'rejected')])
    current = clock(monkeypatch, datetime(2026, 9, 28, 10, 3, 59, tzinfo=timezone.utc))
    assert research.summarize(str(path))['outcomes']['unknown'] == 1
    current[0] += timedelta(seconds=2)
    assert research.summarize(str(path))['outcomes']['rejected'] == 1


def test_implicit_research_time_cache_expires_at_seven_days(tmp_path, monkeypatch):
    path = tmp_path / 'events.jsonl'
    write(path, [apply(at='2026-09-21T10:00:00+00:00')])
    current = clock(monkeypatch, datetime(2026, 9, 28, 9, 59, 59, tzinfo=timezone.utc))
    assert research.summarize(str(path))['recent'] == 1
    current[0] += timedelta(seconds=2)
    assert research.summarize(str(path))['recent'] == 0


def test_implicit_research_time_cache_expires_at_moscow_midnight(tmp_path, monkeypatch):
    path = tmp_path / 'events.jsonl'
    write(path, [apply()])
    current = clock(monkeypatch, datetime(2026, 9, 28, 20, 59, 59, tzinfo=timezone.utc))
    assert research.summarize(str(path), days=1)['total'] == 1
    current[0] += timedelta(seconds=2)
    assert research.summarize(str(path), days=1)['total'] == 0


def test_research_scope_cache_keeps_ownership_isolated(tmp_path):
    from tests.direction_view_fakes import two_directions
    two_directions(tmp_path)
    path = tmp_path / 'events.jsonl'
    write(path, [apply('1', direction='qa'), apply('2', direction='electric'),
                 status('2026-09-28T10:01:00+00:00', 'positive', direction='qa')])
    now = datetime(2026, 9, 28, 15, tzinfo=timezone.utc)
    for scope, count in [('qa', 1), ('electric', 1), ('*', 2), ('?', 0)]:
        result = research.summarize(str(path), now=now, scope=scope)
        assert result == research._summarize(str(path), now=now, scope=scope)
        assert result['total'] == count


def test_unavailable_research_journal_preserves_existing_report_behavior(tmp_path):
    path = tmp_path / 'events.jsonl'
    path.mkdir()
    now = datetime(2026, 9, 28, 15, tzinfo=timezone.utc)
    assert research.summarize(str(path), now=now) == research._summarize(str(path), now=now)
