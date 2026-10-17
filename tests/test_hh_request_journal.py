"""Offline TELEM-1 evidence collection must never authorize a request."""
import asyncio
import json
import logging
import stat
from types import SimpleNamespace
from urllib.parse import urlencode

import pytest

from hh import recovery, request_journal, verification
from state_store.native_apply import NativeApplyRepository, run_native_attempt
from state_store.private_journal import read_json_records
from tests.test_hh_late_outcome_adversarial_e import Emitter
from tests.test_hh_recovery_attempt_window import URL, request
from tests.test_hh_server_delivery import run_check, setup


def monitored_profile(tmp_path):
    context, page = Emitter(), Emitter()
    page.context = context
    client = SimpleNamespace(_page=page, _context=context,
        _cookie_paths=SimpleNamespace(state_dir=str(tmp_path / 'profile-state')))
    monitor = recovery.monitor_page(client, page)
    return client, context, page, monitor


@pytest.mark.parametrize('source', ['page', 'worker', 'unmonitored'])
@pytest.mark.parametrize('host', ['spb.hh.ru', 'counter.invalid'])
def test_private_signature_has_exact_route_and_no_values(tmp_path, caplog, source, host):
    client, context, page, monitor = monitored_profile(tmp_path)
    repository = NativeApplyRepository(tmp_path / 'cookies.json', 'hh')
    fields = {'vacancyId': 'PRIVATE_VACANCY', 'hhtmFromLabel': 'PRIVATE_FROM',
              'hhtmSourceLabel': 'PRIVATE_SOURCE', 'unknownKey': 'PRIVATE_QUERY'}
    event = request(page if source == 'page' else None,
        'https://PRIVATE_USER:PRIVATE_PASSWORD@' + host + '/exact/unclassified'
        '?' + urlencode(fields) + '#PRIVATE_FRAGMENT')
    if source == 'unmonitored':
        event.frame.page = Emitter()
    event.headers = {'content-type': 'application/json; charset=PRIVATE_CHARSET',
                     'cookie': 'PRIVATE_COOKIE', 'authorization': 'PRIVATE_AUTH'}
    event.post_data = '{PRIVATE_BODY'
    event.post_data_buffer = event.post_data.encode()
    caplog.set_level(logging.INFO)

    async def operation():
        attempt = client._external_attempt
        attempt.begin()
        # A synthetic owned receipt isolates the observation from dispatch.
        attempt._hh_application_posts.append({'owned': True, 'status': 200,
            '_started_monotonic': request_journal.time.monotonic() - 0.5})
        context.emit('request', event)
        assert context._hh_action_watch.unknown is (host == 'spb.hh.ru')
        return {'ok': True}

    asyncio.run(run_native_attempt(client, repository, URL, operation))
    path = context._hh_action_watch.request_journal_path
    rows = read_json_records(path)
    assert [r['scope'] for r in rows] == (['page', 'context'] if source == 'page' else ['context'])
    for row in rows:
        assert row['host'] == host and row['path'] == '/exact/unclassified'
        assert row['method'] == 'POST' and row['content_type'] == 'application/json'
        assert row['query_fields'] == sorted(fields)
        assert row['body_length'] == len(event.post_data_buffer)
        assert row['since_owned_post_ms'] >= 500
        assert row['observed_at'].endswith('+00:00')
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert 'PRIVATE_' not in path.read_text() + caplog.text
    # Exact addresses stay out of the ordinary trace/log surface.
    assert '/exact/unclassified' not in caplog.text


def test_journal_is_always_on_without_trace_and_pins_profile_destination(tmp_path):
    client, context, page, _ = monitored_profile(tmp_path)
    path = context._hh_action_watch.request_journal_path
    path.parent.mkdir()
    path.write_text('')
    path.chmod(0o644)
    client._cookie_paths = SimpleNamespace(state_dir=str(tmp_path / 'foreign-profile'))
    event = request(page, 'https://hh.ru/unknown?blank=&repeated=one&repeated=two')
    event.post_data = 'телеметрия'
    event.headers = {'content-type': 'text/plain; charset=UTF-8'}
    # Idle traffic is outside the HH attempt window.
    context.emit('request', event)
    assert path.read_text() == ''
    client._manual_apply_guard = object()
    context.emit('request', event)
    del client._manual_apply_guard
    rows = read_json_records(path)
    assert [r['scope'] for r in rows] == ['page', 'context']
    assert all(r['since_owned_post_ms'] is None for r in rows)
    assert all(r['body_length'] == len(event.post_data.encode()) for r in rows)
    assert rows[0]['query_fields'] == ['blank', 'repeated']
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not (tmp_path / 'foreign-profile').exists()


@pytest.mark.parametrize('foreign_active', [False, True])
def test_retained_attempt_and_opaque_bytes_are_recorded(tmp_path, foreign_active):
    client, context, page, _ = monitored_profile(tmp_path)
    repository = NativeApplyRepository(tmp_path / 'cookies.json', 'hh')

    async def operation():
        return {'ok': False}

    asyncio.run(run_native_attempt(client, repository, URL, operation))
    assert client._external_attempt is None
    if foreign_active:
        client._external_attempt = SimpleNamespace(client=client, context=object(),
            repository=repository, acting=False)
    event = request(page, 'https://hh.ru/opaque')
    event.post_data = None
    event.post_data_buffer = b'\x00\xff\x00'
    context.emit('request', event)
    rows = read_json_records(context._hh_action_watch.request_journal_path)
    assert len(rows) == 2 and all(r['body_length'] == 3 for r in rows)
    assert repository.get(URL)['status'] == 'uncertain'
    assert not repository.claim(URL, '', '')


def test_unreadable_request_still_records_available_signature(tmp_path):
    client, context, _, _ = monitored_profile(tmp_path)
    client._manual_apply_guard = object()

    class Unreadable:
        method = 'POST'
        url = 'https://hh.ru/exact-unreadable?knownName=PRIVATE_QUERY'

        def __getattr__(self, name):
            raise RuntimeError('PRIVATE_PROPERTY_ERROR')

    context.emit('request', Unreadable())
    row, = read_json_records(context._hh_action_watch.request_journal_path)
    assert row['scope'] == 'context' and row['path'] == '/exact-unreadable'
    assert row['method'] == 'POST' and row['query_fields'] == ['knownName']
    assert row['body_length'] is None and row['content_type'] is None
    assert context._hh_action_watch.unknown
    assert 'PRIVATE_' not in json.dumps(row)


@pytest.mark.parametrize('failure', ['write', 'trace', 'symlink'])
@pytest.mark.parametrize('host', ['hh.ru', 'counter.invalid'])
def test_evidence_failure_does_not_change_safety(tmp_path, monkeypatch, caplog, failure, host):
    client, context, page, monitor = monitored_profile(tmp_path)
    repository = NativeApplyRepository(tmp_path / 'cookies.json', 'hh')
    path = context._hh_action_watch.request_journal_path

    def fail(*args, **kwargs):
        raise OSError('PRIVATE_WRITE_ERROR')

    if failure == 'write':
        monkeypatch.setattr(request_journal, 'append_json', fail)
    elif failure == 'trace':
        monkeypatch.setattr(recovery, '_record_unknown', fail)
    else:
        path.parent.mkdir()
        victim = tmp_path / 'victim'
        victim.write_text('unchanged')
        path.symlink_to(victim)

    async def operation():
        client._external_attempt.begin()
        event = request(page, 'https://' + host + '/unapproved')
        context.emit('request', event)
        assert monitor.unknown is (host == 'hh.ru')
        assert context._hh_action_watch.unknown is (host == 'hh.ru')
        return {'ok': True}

    result = asyncio.run(run_native_attempt(client, repository, URL, operation))
    assert bool(result.get('uncertain')) is (host == 'hh.ru')
    assert repository.get(URL)['status'] == ('uncertain' if host == 'hh.ru' else 'completed')
    if failure == 'trace':
        assert len(read_json_records(path)) == 2
    if failure == 'symlink':
        assert victim.read_text() == 'unchanged'
    assert 'PRIVATE_' not in caplog.text


@pytest.mark.parametrize('extra', ['malformed', 'response', 'unknown_body'])
@pytest.mark.parametrize('flag', ['0', '1'])
def test_live_shape_delivery_never_excuses_second_possible_action(tmp_path, monkeypatch, extra, flag):
    # No passive classifier is installed until exact live signatures are known.
    monkeypatch.setenv('HH_PASSIVE_TELEMETRY_ENABLED', flag)
    client, attempt, _, _ = setup(tmp_path)
    context, page = attempt.context, attempt.page
    path = tmp_path / 'profile-state' / request_journal.JOURNAL_NAME
    context._hh_action_watch.request_journal_path = path
    route = '/applicant/vacancy_response/popup' if extra == 'response' else '/unclassified'
    event = request(page, 'https://spb.hh.ru' + route)
    if extra == 'malformed':
        # Valid envelope-like text is not a single JSON document; alternatively
        # a broken action body has exactly the same public classification.
        event.headers = {'content-type': 'text/plain'}
        event.post_data = '{}\n{"type":"event"}\n{"private":"PRIVATE_BODY"}'
    else:
        event.headers = {'content-type': 'application/json'}
        event.post_data = json.dumps({'resume_hash': 'PRIVATE_RESUME', 'vacancyId': 'PRIVATE_VACANCY'})
    context.emit('request', event)
    for _ in range(16):
        telemetry = request(page, 'https://spb.hh.ru/unidentified-telemetry'
            '?hhtmFromLabel=PRIVATE_FROM&hhtmSourceLabel=PRIVATE_SOURCE&vacancyId=PRIVATE_VACANCY')
        telemetry.post_data = None
        context.emit('request', telemetry)
    assert run_check(client, attempt).status == 'confirmed'
    assert len(attempt._hh_application_posts) == 2
    assert attempt._hh_application_posts[1]['owned'] is False
    if extra == 'malformed':
        assert attempt._hh_application_posts[1]['classification']['application_reason'] == 'malformed_body'
    assert attempt.uncertain and page._hh_action_monitor.unknown and context._hh_action_watch.unknown
    assert not verification.can_continue_uncertain(client, attempt)
    attempt.repository.transition(attempt.url, attempt.owner, 'uncertain')
    assert not attempt.repository.claim(attempt.url, '', '')
    rows = read_json_records(path)
    assert len(rows) == 34
    assert rows[0]['path'] == route and rows[0]['since_owned_post_ms'] >= 0
    assert all(r['body_length'] == 0 for r in rows[2:])
    assert 'PRIVATE_' not in path.read_text()
