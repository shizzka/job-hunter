"""A realistic row count must survive interleaved bot reports and run identities."""
from collections import OrderedDict
import json
import time
from types import SimpleNamespace

import pytest

import analytics
import analytics_read_cache as cache
import application_history
import hiring_research
import manual_apply_queue as queue
from telegram_app import history


def test_real_size_press_sequence_does_not_evict_the_shared_journal(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(cache, '_journals', OrderedDict())
    monkeypatch.setattr(cache, '_reports', OrderedDict())
    monkeypatch.setattr(queue, '_queue_path', lambda profile_name=None: tmp_path / 'queue.json')
    monkeypatch.setattr(queue.company_blacklist, '_path', lambda profile_name=None: tmp_path / 'blacklist.json')
    path = tmp_path / 'analytics_events.jsonl'
    profile = SimpleNamespace(home_dir=tmp_path, analytics_events_file=path,
                              seen_file=tmp_path / 'seen_vacancies.json')
    now = analytics._now().isoformat()
    with path.open('w') as stream:
        for number in range(110_000):
            stream.write(json.dumps({'event': 'decision', 'run_id': f'run-{number % 10}',
                'vacancy_id': str(number), 'source': 'hh', 'decision': 'skipped_low_score',
                'created_at': now, 'details': 'synthetic ' * 175}) + '\n')
        stream.write(json.dumps({'event': 'application_result', 'outcome': 'sent',
            'vacancy_id': 'sent', 'created_at': now}) + '\n')
    item = queue.create_candidate({'id': 'pending', 'title': 'Synthetic'}, {'score': 80}, profile_name='qa')
    reads = []
    original = cache.Journal._read
    def counted(self, filename):
        before = self.bytes_read
        result = original(self, filename)
        if str(filename) == str(path):
            reads.append(self.bytes_read - before)
        return result
    monkeypatch.setattr(cache.Journal, '_read', counted)

    def sequence():
        result = [application_history.summarize(tmp_path, days=7), queue.list_candidates('qa'),
                  history.responses_snapshot(profile), analytics.summarize(events_file=path, all_time=True),
                  hiring_research.summarize(path)]
        for number in range(10):
            result.append(analytics.get_run_summary(f'run-{number}', events_file=path,
                                                    history_file=tmp_path / 'missing'))
        return result

    started = time.perf_counter()
    first = sequence()
    cold = time.perf_counter() - started
    cold_bytes = sum(reads)
    reads.clear()
    started = time.perf_counter()
    second = sequence()
    warm = time.perf_counter() - started
    with capsys.disabled():
        print(f'BENCH press sequence {path.stat().st_size / 1e6:.1f} MB / 110001 rows: '
              f'cold={cold:.3f}s warm={warm:.3f}s cold_read={cold_bytes} warm_read={sum(reads)}')
    assert first == second
    assert sum(reads) == 0, 'Interleaving reports evicted the journal and reread it from offset zero'
    assert cold_bytes == path.stat().st_size, 'Report tags must share the compact journal parse'
    assert warm < 1
    # The full public history API preserves details. Even alongside that view,
    # different run identities must fit the budget and never evict the index.
    assert analytics.history_events(path)[0]['details'] == 'synthetic ' * 175
    cache._reports.clear()
    reads.clear()
    assert sequence() == first
    assert sum(reads) == 0
    # Warm queue and response renders keep their derived reports, too.
    with monkeypatch.context() as patch:
        patch.setattr(cache.json, 'loads', lambda *a, **k: pytest.fail('Warm journal was reparsed')
                      if isinstance(a[0], bytes) else json_loads(*a, **k))
        started = time.perf_counter()
        assert queue.list_candidates('qa')[0]['token'] == item['token']
        assert history.responses_snapshot(profile)['sent'] == 1
        assert time.perf_counter() - started < .1
    with path.open('a') as stream:
        for identity in ('sent', 'pending'):
            stream.write(json.dumps({'event': 'negotiation_status', 'vacancy_id': identity,
                'status_bucket': 'positive', 'created_at': now}) + '\n')
    reads.clear()
    assert queue.list_candidates('qa') == []
    assert history.responses_snapshot(profile)['invitations'] == 1
    assert sum(reads) == path.stat().st_size - cold_bytes


json_loads = json.loads


def test_shared_run_projection_preserves_full_failure_and_artifact_reports(tmp_path):
    from run_summary import build_run_summary
    from tests.test_run_summary import finished_run
    run = finished_run(run_id='selected')
    rows = [
        {'event': 'stage_failed', 'failure_id': 'failure', 'reason_code': 'SOURCE_TIMEOUT',
         'source': 'hh', 'stage': 'collection', 'retryable': True, 'continued': False,
         'recorded_at_utc': '2026-10-23T09:00:00+00:00', 'run_id': 'selected',
         'collection': {'vacancies_retained': 1}, 'extra_diagnostic': 'preserved'},
        {'event': 'decision', 'run_id': 'selected', 'vacancy_id': '1', 'source': 'hh',
         'decision': 'not_processed', 'failure_id': 'failure',
         'recorded_at_utc': '2026-10-23T09:00:00+00:00'},
        {'event': 'debug_artifact', 'run_id': 'selected', 'vacancy_id': '1', 'source': 'hh',
         'artifact_type': 'apply_trace', 'artifact_ref': 'synthetic-reference', 'trace_id': 'trace'},
    ]
    path = tmp_path / 'analytics_events.jsonl'
    path.write_text(''.join(json.dumps(row) + '\n' for row in rows))
    checkpoint = tmp_path / 'runs.jsonl'
    checkpoint.write_text(json.dumps(run) + '\n')
    # Compare against unprojected records with the same ownership enrichment.
    expected = build_run_summary(run, analytics.direction_history.HistoryScope(tmp_path).events(rows))
    actual = analytics.get_run_summary('selected', events_file=path, history_file=checkpoint)
    for field in ('counters', 'primary_failure', 'stage_failures', 'artifacts', 'status'):
        assert actual[field] == expected[field]
