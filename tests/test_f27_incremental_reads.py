"""F27 local queue/history reads reuse journals and keep cold IO off polling."""
import asyncio
import json
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

import analytics
import analytics_read_cache as cache
import manual_apply_queue as queue
from telegram_app import history
from tests.test_f27_review import Bot


def append(path, row):
    with path.open('a') as stream:
        stream.write(json.dumps(row) + '\n')


@pytest.fixture
def local(tmp_path, monkeypatch):
    monkeypatch.setattr(queue, '_queue_path', lambda profile_name=None: tmp_path / 'queue.json')
    monkeypatch.setattr(queue.company_blacklist, '_path', lambda profile_name=None: tmp_path / 'blacklist.json')
    return SimpleNamespace(home_dir=tmp_path, analytics_events_file=tmp_path / 'analytics_events.jsonl',
                           seen_file=tmp_path / 'seen_vacancies.json')


def test_large_unchanged_queue_and_responses_do_not_parse_and_append_is_visible(local, monkeypatch):
    path = local.analytics_events_file
    # 200 MiB of diagnostic payload, with a modest realistic row count.
    line = json.dumps({'event': 'diagnostic', 'payload': 'x' * 10190}) + '\n'
    with path.open('w') as stream:
        for _ in range(20480):
            stream.write(line)
    assert path.stat().st_size > 199 * 1024 * 1024
    item = queue.create_candidate({'id': 'pending', 'title': 'Synthetic'}, {'score': 80}, profile_name='qa')
    stamp = analytics._now().isoformat()
    append(path, {'event': 'decision', 'decision': 'applied_auto', 'vacancy_id': 'sent', 'created_at': stamp})
    results = {}
    for name, render in [('queue', lambda: queue.list_candidates('qa')),
                         ('responses', lambda: history.responses_snapshot(local))]:
        start = time.perf_counter()
        render()
        cold = time.perf_counter() - start
        parser = Mock(wraps=json.loads)
        with monkeypatch.context() as patch:
            patch.setattr(json, 'loads', parser)
            start = time.perf_counter()
            result = render()
            warm = time.perf_counter() - start
        journal_parses = [call for call in parser.call_args_list if isinstance(call.args[0], bytes)]
        results[name] = (cold, warm, len(journal_parses))
        if name == 'responses':
            assert result['sent'] == 1
    print('synthetic 200MiB cold/warm/parser_calls:', results)
    assert all(warm < .1 and parses == 0 for _, warm, parses in results.values())
    append(path, {'event': 'negotiation_status', 'vacancy_id': 'pending', 'created_at': stamp})
    append(path, {'event': 'negotiation_status', 'vacancy_id': 'sent', 'status_bucket': 'positive', 'created_at': stamp})
    assert queue.list_candidates('qa') == []
    assert queue.get_candidate(item['token'], profile_name='qa')['status'] == 'already_applied'
    assert history.responses_snapshot(local)['invitations'] == 1


def test_outcomes_reuse_seen_and_only_parse_appended_tail(local, monkeypatch):
    path = local.analytics_events_file
    append(path, {'event': 'negotiation_status', 'vacancy_id': 'old'})
    local.seen_file.write_text(json.dumps({'old': {'action': 'apply_uncertain'}}))
    assert queue._local_application_outcomes(local.home_dir)['old'] == 'apply_uncertain'
    seen_reader = Mock(wraps=queue.seen.application_outcomes_from_file)
    monkeypatch.setattr(queue.seen, 'application_outcomes_from_file', seen_reader)
    assert queue._local_application_outcomes(local.home_dir)['old'] == 'apply_uncertain'
    assert seen_reader.call_count == 0
    append(path, {'event': 'negotiation_observation', 'vacancy_id': 'new'})
    journal = cache.journal(path)
    before = journal.bytes_read
    outcomes = queue._local_application_outcomes(local.home_dir)
    assert outcomes == {'old': 'apply_uncertain', 'new': 'already_applied'}
    assert 0 < journal.bytes_read - before < path.stat().st_size


def test_history_events_reuses_parser_and_preserves_full_records(local, monkeypatch):
    path = local.analytics_events_file
    append(path, {'event': 'decision', 'vacancy_id': 'one', 'details': 'Keep full public API details'})
    assert analytics.history_events(path)[0]['details'] == 'Keep full public API details'
    parser = Mock(wraps=json.loads)
    monkeypatch.setattr(json, 'loads', parser)
    assert len(analytics.history_events(path)) == 1
    assert not [c for c in parser.call_args_list if isinstance(c.args[0], bytes)]
    append(path, {'event': 'decision', 'vacancy_id': 'two'})
    assert len(analytics.history_events(path)) == 2
    assert len([c for c in parser.call_args_list if isinstance(c.args[0], bytes)]) == 1


@pytest.mark.parametrize('method', ['_show_review_queue', '_show_review_list', '_open_review_card'])
def test_cold_review_journal_read_runs_in_worker(local, monkeypatch, method):
    item = queue.create_candidate({'id': 'pending', 'title': 'Synthetic'}, {'score': 80}, profile_name='qa')
    bot = Bot(local.home_dir)
    loop_thread = threading.get_ident()
    original = queue._local_application_outcomes
    def cold_read(home):
        assert threading.get_ident() != loop_thread, 'Cold journal IO ran on the event loop'
        return original(home)
    monkeypatch.setattr(queue, '_local_application_outcomes', cold_read)
    principal = {'user_id': 42, 'role': 'user', 'profile': 'qa'}
    if method == '_open_review_card':
        asyncio.run(bot._open_review_card(42, 'qa', item['token']))
    else:
        asyncio.run(getattr(bot, method)(42, principal, profile_name='qa'))


def test_snapshot_append_is_incremental_and_rebinds_response_owner(local, monkeypatch):
    import directions
    from tests.direction_view_fakes import two_directions
    two_directions(local.home_dir)
    stamp = analytics._now().isoformat()
    append(local.analytics_events_file, {'event': 'decision', 'decision': 'applied_auto',
           'vacancy_id': 'shared', 'direction': 'qa', 'created_at': stamp})
    append(local.analytics_events_file, {'event': 'negotiation_status', 'vacancy_id': 'shared',
           'status_bucket': 'positive', 'created_at': stamp})
    assert history.responses_snapshot(local)['invitations'] == 1
    path = local.home_dir / 'negotiation_snapshots.jsonl'
    append(path, {'vacancyId': 'shared', 'resumeId': 222})
    assert history.responses_snapshot(local)['invitations'] == 0
    index = cache.journal(path)
    before = index.bytes_read
    parser = Mock(wraps=json.loads)
    with monkeypatch.context() as patch:
        patch.setattr(json, 'loads', parser)
        assert history.responses_snapshot(local)['invitations'] == 0
    assert not [c for c in parser.call_args_list if isinstance(c.args[0], bytes)]
    append(path, {'vacancyId': 'other', 'resumeId': 111})
    assert history.responses_snapshot(local)['invitations'] == 0
    assert 0 < index.bytes_read - before < path.stat().st_size
    directions.set_active_direction(local.home_dir, 'electric')
    assert history.responses_snapshot(local)['sent'] == 0


def test_transient_response_read_error_is_not_memoized(local, monkeypatch):
    import application_history
    stamp = analytics._now().isoformat()
    append(local.analytics_events_file, {'event': 'decision', 'decision': 'applied_auto',
           'vacancy_id': 'sent', 'created_at': stamp})
    append(local.analytics_events_file, {'event': 'negotiation_status', 'vacancy_id': 'sent',
           'status_bucket': 'positive', 'created_at': stamp})
    application_history.summarize(local.home_dir, days=30, recent_limit=None)
    with monkeypatch.context() as patch:
        def unavailable(*args):
            raise OSError('Synthetic transient read error')
        patch.setattr(cache.Journal, 'read', unavailable)
        assert history.responses_snapshot(local)['invitations'] == 0
    assert history.responses_snapshot(local)['invitations'] == 1
