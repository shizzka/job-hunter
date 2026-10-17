"""TELEM-2: replay only sanitized wire signatures, with no HH transport."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import urlencode
from unittest.mock import AsyncMock

import pytest

import agent
from hh import passive_telemetry as telemetry, recovery, verification
from hh.ui import HHUIGuard
from state_store.private_journal import read_json_records
from tests.test_hh_observation import HASH
from tests.test_hh_post_submit_verification import APPLIED_HTML
from tests.test_hh_recovery_attempt_window import request
from tests.test_hh_server_delivery import html, setup


SIGNATURES = json.loads((Path(__file__).parent / 'fixtures/hh_telem_2_signatures.json').read_text())


def live_requests(page):
    events = []
    for row in SIGNATURES:
        url = 'https://' + row['host'] + row['path'] + '?' + urlencode({k: 'fixture' for k in row['query_fields']})
        event = request(page, url, row['method'])
        event.headers = {'content-type': row['content_type'] + '; charset=UTF-8'}
        length = row['body_length']
        prefix = '{}\n{}\n' if row['host'] == 'sentry.hh.ru' else '{}'
        event.post_data = prefix.ljust(length) if length else ''
        event.post_data_buffer = event.post_data.encode()
        event.frame = page.main_frame
        events.append(event)
    return events


def live_script():
    page = SimpleNamespace(main_frame=object())
    descriptors = [{'url': e.url, 'body': e.post_data, 'headers': e.headers} for e in live_requests(page)]
    return 'for (const e of ' + json.dumps(descriptors) + ") { await fetch(e.url,{method:'POST',mode:'no-cors',headers:e.headers,body:e.body}).catch(()=>{}); }"


@pytest.mark.parametrize('flag', [None, '1', '0'])
def test_reconstructed_journal_keeps_only_owned_post_and_delivery(tmp_path, monkeypatch, flag):
    if flag is None:
        monkeypatch.delenv('HH_PASSIVE_TELEMETRY_ENABLED', raising=False)
    else:
        monkeypatch.setenv('HH_PASSIVE_TELEMETRY_ENABLED', flag)
    client, attempt, trace, _ = setup(tmp_path)
    page, context = attempt.page, attempt.context
    path = tmp_path / 'unknown.jsonl'
    context._hh_action_watch.request_journal_path = path
    page.is_closed = lambda: False
    client._ui_guard = HHUIGuard(tmp_path / 'ui', notify=AsyncMock(return_value=False))
    response = SimpleNamespace(status=200, url=attempt.url, headers={'content-type': 'text/html'},
        text=AsyncMock(return_value=APPLIED_HTML + html(attempt.url.rsplit('/', 1)[-1], [5600000002])),
        dispose=AsyncMock())
    context.request.get = AsyncMock(return_value=response)
    events = live_requests(page)
    assert len(events) == 16
    assert recovery._response_evidence(next(e for e in events if 'sentry.hh.ru' in e.url))['body_state'] == 'malformed'
    for event in events:
        context.emit('request', event)
    asyncio.run(verification.reconcile_post_submit(client, attempt, HASH, 'PRIVATE_LETTER'))
    assert attempt._hh_server_delivery.status == 'confirmed'
    enabled = flag != '0'
    assert len(attempt._hh_application_posts) == (1 if enabled else 2)
    assert context._hh_action_watch.application_post_sequence == (1 if enabled else 2)
    assert context._hh_action_watch.unknown is not enabled
    assert page._hh_action_monitor.unknown is not enabled
    assert bool(attempt.uncertain) is not enabled
    assert verification._owned_post(client, attempt, HASH, 'PRIVATE_LETTER') is (page._hh_request_approval if enabled else None)
    result = verification.confirmed_result(client, attempt.url, {'ok': False}, owned_attempt=attempt, close_attempt=True)
    if enabled:
        assert result['ok'] and not result['uncertain'] and not result.get('hh_hard_stop')
        assert not path.exists()
        assert not any(e['stage'] == 'HH_UNKNOWN_ACTION' for e in trace.events)
    else:
        assert result is None and not verification.can_continue_uncertain(client, attempt)
        assert len(read_json_records(path)) == 32


@pytest.mark.parametrize('endpoint', ['anatskytics', 'sentry', 'cpa', 'targetads'])
@pytest.mark.parametrize('change', ['limit', 'extra_query', 'host', 'path', 'content_type', 'encoding', 'method'])
def test_partial_signature_stops_and_journals_after_owned_200(tmp_path, endpoint, change):
    client, attempt, _, _ = setup(tmp_path)
    page, context = attempt.page, attempt.context
    path = tmp_path / 'unknown.jsonl'
    context._hh_action_watch.request_journal_path = path
    event = next(e for e in live_requests(page) if endpoint in e.url)
    limits = {'anatskytics': telemetry.ANATSKYTICS_MAX_BODY, 'sentry': telemetry.SENTRY_MAX_BODY,
              'cpa': telemetry.CPA_MAX_BODY, 'targetads': telemetry.TARGETADS_MAX_BODY}
    if change == 'limit':
        event.post_data_buffer = b'x' * (limits[endpoint] + 1)
        event.post_data = event.post_data_buffer.decode()
    elif change == 'extra_query':
        event.url += '&unproven=fixture'
    elif change == 'host':
        event.url = event.url.replace(event.url.split('/')[2], 'unproven.invalid')
    elif change == 'path':
        event.url = event.url.replace('?', '/other?')
    elif change == 'content_type':
        event.headers['content-type'] = 'application/json'
    elif change == 'encoding':
        event.headers['content-encoding'] = 'gzip'
    else:
        event.method = 'PUT'
    assert telemetry.classify_request(event) is None
    context.emit('request', event)
    assert context._hh_action_watch.unknown and page._hh_action_monitor.unknown and attempt.uncertain
    assert client._hh_recovery_stop_reason == 'possible_external_action'
    assert len(read_json_records(path)) == 2
    assert attempt.repository.get(attempt.url)['status'] == 'uncertain'
    assert not attempt.repository.claim(attempt.url, '', '')


@pytest.mark.parametrize('host,allowed', [('hh.ru', True), ('spb.hh.ru', True), ('rostov-na-donu.hh.ru', True),
    ('nested.spb.hh.ru', False), ('hh.ru.invalid', False), ('evilhh.ru', False), ('hh.ru.', False)])
def test_anatskytics_has_only_exact_hh_or_single_region_host(host, allowed):
    event = request(None, 'https://' + host + '/anatskytics?event=fixture')
    event.headers = {'content-type': 'application/x-www-form-urlencoded'}
    event.post_data = ''
    assert bool(telemetry.classify_request(event)) is allowed


@pytest.mark.parametrize('endpoint,limit', [('anatskytics', 256), ('sentry', 65536), ('cpa', 256), ('targetads', 4096)])
def test_body_limit_counts_wire_bytes_inclusively(endpoint, limit):
    event = next(e for e in live_requests(SimpleNamespace(main_frame=object())) if endpoint in e.url)
    event.post_data = 'я' * (limit // 2)
    event.post_data_buffer = event.post_data.encode()
    assert telemetry.classify_request(event) == 'passive_telemetry'
    event.post_data_buffer += b'x'
    assert telemetry.classify_request(event) is None


@pytest.mark.parametrize('url', ['https://sentry.hh.ru/api/1/envelope/', 'https://sentry.hh.ru/api/0012/envelope/'])
def test_sentry_path_allows_only_numeric_project(url):
    event = request(None, url)
    event.headers = {'content-type': 'text/plain'}
    assert telemetry.classify_request(event) == 'passive_telemetry'


@pytest.mark.parametrize('url', ['https://sentry.hh.ru/api/x/envelope/', 'https://sentry.hh.ru/api/607/envelope',
    'https://sentry.hh.ru/api/607/envelope/extra', 'https://sentry.hh.ru/api/%36%30%37/envelope/',
    'http://sentry.hh.ru/api/607/envelope/', 'https://sentry.hh.ru:444/api/607/envelope/',
    'https://user@sentry.hh.ru/api/607/envelope/'])
def test_unproven_origin_or_path_is_not_passive(url):
    event = request(None, url)
    event.headers = {'content-type': 'text/plain'}
    assert telemetry.classify_request(event) is None


@pytest.mark.parametrize('field', ['headers', 'post_data_buffer', 'post_data', 'url', 'method'])
def test_unreadable_signature_never_becomes_passive(field):
    event = live_requests(SimpleNamespace(main_frame=object()))[0]

    class Unreadable:
        def __getattr__(self, name):
            if name == field:
                raise RuntimeError('unreadable signature')
            if field == 'post_data' and name == 'post_data_buffer':
                return None
            return getattr(event, name)

    assert telemetry.classify_request(Unreadable()) is None


@pytest.mark.parametrize('source', ['page', 'unmonitored', 'worker'])
def test_exact_passive_requests_do_not_depend_on_frame_observation(tmp_path, source):
    client, attempt, _, _ = setup(tmp_path)
    for event in live_requests(attempt.page):
        if source != 'page':
            event.frame = SimpleNamespace(page=object() if source == 'unmonitored' else None)
        attempt.context.emit('request', event)
    assert not attempt.context._hh_action_watch.unknown
    assert len(attempt._hh_application_posts) == 1


def test_passive_request_never_clears_sticky_unknown_or_durable_uncertain(tmp_path):
    client, attempt, _, _ = setup(tmp_path)
    monitor, watch = attempt.page._hh_action_monitor, attempt.context._hh_action_watch
    monitor.mark_unknown(reason='child_frame_interaction')
    for event in live_requests(attempt.page):
        attempt.context.emit('request', event)
    assert monitor.unknown and watch.unknown and attempt.uncertain
    assert attempt.repository.get(attempt.url)['status'] == 'uncertain'
    assert not verification.can_continue_uncertain(client, attempt)
    assert not attempt.repository.claim(attempt.url, '', '')


def test_passive_events_preserve_typed_uncertain_session_but_second_popup_stops(tmp_path):
    client, attempt, _, _ = setup(tmp_path)
    page, context = attempt.page, attempt.context
    page.is_closed = lambda: False
    client._ui_guard = HHUIGuard(tmp_path / 'ui', notify=AsyncMock(return_value=False))
    attempt.uncertain = True
    attempt.repository.transition(attempt.url, attempt.owner, 'uncertain')
    attempt._hh_uncertain_session = verification._UncertainSession(attempt, client._cookie_binding, context.request, 1)
    receipt = agent._HHApplyReceipt({'source': 'hh', 'url': attempt.url}, client, lambda _: None)
    receipt.result, receipt.owned_attempt = {'uncertain': True, 'ok': False}, attempt
    for event in live_requests(page):
        context.emit('request', event)
    assert receipt.can_continue()
    assert attempt.uncertain and attempt.repository.get(attempt.url)['status'] == 'uncertain'
    context.emit('request', request(page, 'https://spb.hh.ru/applicant/vacancy_response/popup'))
    assert len(attempt._hh_application_posts) == 2
    assert not receipt.can_continue()
    assert not attempt.repository.claim(attempt.url, '', '')
