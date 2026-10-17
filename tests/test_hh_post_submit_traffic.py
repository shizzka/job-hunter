"""Post-submit telemetry shares no response payload; unknown evidence stays sticky."""
import asyncio
import json
from types import SimpleNamespace
from urllib.parse import urlencode
from unittest.mock import AsyncMock

import pytest

from hh import verification
from hh.submit_boundary import RESPONSE_PATH
from state_store.native_apply import NativeApplyRepository, run_native_attempt
from tests.test_hh_recovery_attempt_window import URL, monitored_client, request

POPUP_FIELDS = {'_xsrf': 'PRIVATE_TOKEN', 'vacancy_id': '42', 'resume_hash': 'PRIVATE_RESUME',
                'ignore_postponed': 'true', 'incomplete': 'false',
                'mark_applicant_visible_in_vacancy_country': 'false', 'country_ids': '[]',
                'letter': 'PRIVATE_LETTER', 'lux': 'true', 'withoutTest': 'no',
                'hhtmFromLabel': '', 'hhtmSourceLabel': ''}


def payload(event, fields, wire):
    if wire == 'query':
        event.url += '?' + urlencode(fields)
        event.post_data = None
    elif wire == 'form':
        event.headers = {'content-type': 'application/x-www-form-urlencoded'}
        event.post_data = urlencode(fields).replace('_', '%5F')
    elif wire.startswith('multipart'):
        event.headers = {'content-type': 'multipart/form-data; boundary=fixture'}
        parts = []
        for key, value in fields.items():
            name = ('name*0="' + key[:2] + '"; name*1="' + key[2:] + '"'
                    if wire == 'multipart_continued' else
                    'name="' + (key[:1] + '\\' + key[1:] if wire == 'multipart_escaped' else key) + '"')
            parts.append('--fixture\r\nContent-Disposition: form-data; ' + name + '\r\n\r\n' + value + '\r\n')
        event.post_data = ''.join(parts) + '--fixture--\r\n'
    elif wire == 'json':
        event.headers = {'content-type': 'application/json'}
        event.post_data = json.dumps({'nested': fields}).replace('resume', '\\u0072esume')
    else:
        event.headers = {'content-type': 'text/plain'}
        event.post_data = urlencode(fields) + '\r\n'


@pytest.mark.parametrize('method', ['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE'])
@pytest.mark.parametrize('key', ['resume_id', 'resumeId', 'resumeHash', 'resume_hash', 'resume', 'letter'])
@pytest.mark.parametrize('wire', ['query', 'form', 'multipart', 'multipart_escaped', 'multipart_continued', 'json', 'plain'])
def test_resume_and_vacancy_payload_on_unknown_path_counts_and_stops_after_owned_post(
        tmp_path, method, key, wire):
    client, context, page, monitor = monitored_client()
    repository = NativeApplyRepository(tmp_path / 'cookies.json', 'hh')
    event = request(page, 'https://spb.hh.ru/unknown', method)
    fields = {key: '', 'vacancy_id': ''}
    if key == 'letter':
        fields['resume_hash'] = ''
    payload(event, fields, wire)

    async def operation():
        attempt = client._external_attempt
        attempt.begin()
        # The exact approval matcher is exercised by the browser E2E gates;
        # this test isolates request classification after that owned receipt.
        attempt._hh_application_posts.append({'owned': True, 'status': 200})
        context._hh_action_watch.application_post_sequence = 1
        context.emit('request', event)
        assert monitor.unknown and context._hh_action_watch.unknown
        assert context._hh_action_watch.application_post_sequence == 2
        assert attempt.uncertain
        return {'ok': True}

    result = asyncio.run(run_native_attempt(client, repository, URL, operation))
    assert result['uncertain'] and not result['ok']
    assert repository.get(URL)['status'] == 'uncertain'


@pytest.mark.parametrize('wire', ['form', 'multipart', 'multipart_escaped', 'multipart_continued', 'json', 'plain'])
@pytest.mark.parametrize('key', ['resume_id', 'resumeId', 'resumeHash', 'resume_hash', 'resume', 'letter'])
@pytest.mark.parametrize('earlier_unknown', [False, True])
def test_single_body_field_never_counts_and_only_relaxes_after_owned_post(
        tmp_path, monkeypatch, wire, key, earlier_unknown):
    # Rollback mode preserves the pre-TELEM-2 broad post-200 relaxation.
    monkeypatch.setenv('HH_PASSIVE_TELEMETRY_ENABLED', '0')
    client, context, page, monitor = monitored_client()
    repository = NativeApplyRepository(tmp_path / 'cookies.json', 'hh')
    event = request(page, 'https://hh.ru/analytics/pageview')
    payload(event, {key: 'PRIVATE_VALUE'}, wire)

    async def operation():
        attempt = client._external_attempt
        attempt.begin()
        if earlier_unknown:
            context.emit('request', event)
            assert monitor.unknown and context._hh_action_watch.unknown
            assert attempt.uncertain
        attempt._hh_application_posts.append({'owned': True, 'status': 200})
        context._hh_action_watch.application_post_sequence = 1
        context.emit('request', event)
        assert context._hh_action_watch.application_post_sequence == 1
        assert len(attempt._hh_application_posts) == 1
        assert monitor.unknown is earlier_unknown
        assert context._hh_action_watch.unknown is earlier_unknown
        diagnostic = monitor.last_unknown_diagnostic
        assert diagnostic['requires_stop'] is False
        assert diagnostic['unknown_before'] is earlier_unknown
        source = 'multipart' if wire.startswith('multipart') else 'body' if wire == 'plain' else wire
        assert diagnostic['field_sources'] == {source: [key]}
        assert diagnostic['path_category'] == 'analytics'
        assert diagnostic['body_state'] == 'readable'
        assert diagnostic['application_reason'] == 'none'
        return {'ok': True}

    result = asyncio.run(run_native_attempt(client, repository, URL, operation))
    assert bool(result.get('uncertain')) is earlier_unknown


@pytest.mark.parametrize('wire', ['form', 'multipart', 'json'])
@pytest.mark.parametrize('path', ['/analytics/pageview', '/unknown', RESPONSE_PATH])
def test_full_popup_payload_always_counts_as_second_submission(tmp_path, wire, path):
    client, context, page, monitor = monitored_client()
    repository = NativeApplyRepository(tmp_path / 'cookies.json', 'hh')
    event = request(page, 'https://hh.ru' + path)
    payload(event, POPUP_FIELDS, wire)

    async def operation():
        attempt = client._external_attempt
        attempt.begin()
        attempt._hh_application_posts.append({'owned': True, 'status': 200})
        context._hh_action_watch.application_post_sequence = 1
        context.emit('request', event)
        assert context._hh_action_watch.application_post_sequence == 2
        assert len(attempt._hh_application_posts) == 2
        assert attempt._hh_application_posts[1]['owned'] is False
        classification = attempt._hh_application_posts[1]['classification']
        assert classification['application_reason'] == (
            'response_path' if path == RESPONSE_PATH else 'resume_and_vacancy_fields')
        assert len(classification['field_sources'][wire]) == 12
        assert 'PRIVATE_' not in json.dumps(classification)
        assert monitor.unknown and context._hh_action_watch.unknown and attempt.uncertain
        return {'ok': True}

    result = asyncio.run(run_native_attempt(client, repository, URL, operation))
    assert result['uncertain'] and not result['ok']


@pytest.mark.parametrize('method', ['POST', 'PUT', 'PATCH', 'DELETE'])
@pytest.mark.parametrize('path', [RESPONSE_PATH, RESPONSE_PATH + '/popup'])
def test_response_path_counts_without_any_body_fields(tmp_path, method, path):
    client, context, page, monitor = monitored_client()
    repository = NativeApplyRepository(tmp_path / 'cookies.json', 'hh')
    event = request(page, 'https://hh.ru' + path, method)
    event.post_data = None

    async def operation():
        attempt = client._external_attempt
        attempt.begin()
        attempt._hh_application_posts.append({'owned': True, 'status': 200})
        context._hh_action_watch.application_post_sequence = 1
        context.emit('request', event)
        assert context._hh_action_watch.application_post_sequence == 2
        assert len(attempt._hh_application_posts) == (2 if method == 'POST' else 1)
        assert monitor.unknown and context._hh_action_watch.unknown
        assert monitor.last_unknown_diagnostic['application_reason'] == 'response_path'
        return {'ok': True}

    result = asyncio.run(run_native_attempt(client, repository, URL, operation))
    assert result['uncertain'] and not result['ok']


def test_two_owned_posts_block_reconciliation_and_passive_traffic_relaxation(tmp_path):
    client, context, page, monitor = monitored_client()
    context.request = SimpleNamespace(get=AsyncMock())
    page.main_frame = SimpleNamespace(page=page)
    repository = NativeApplyRepository(tmp_path / 'cookies.json', 'hh')
    event = request(page, 'https://hh.ru' + RESPONSE_PATH + '/popup')
    event.frame = page.main_frame
    payload(event, POPUP_FIELDS, 'multipart')
    event.post_data_buffer = event.post_data.encode('utf-8')

    async def operation():
        attempt = client._external_attempt
        attempt.begin()
        proof = {'kind': 'hh-react-popup', 'url': event.url, 'fields': POPUP_FIELDS,
                 'owner': attempt.owner, 'used': True}
        page._hh_request_approval = proof
        attempt._hh_application_posts.append({'request': event, 'owned': True, 'status': 200})
        assert verification._owned_post(client, attempt, POPUP_FIELDS['resume_hash'], POPUP_FIELDS['letter']) is proof
        attempt._hh_application_posts.append({'request': event, 'owned': True, 'status': 200})
        context._hh_action_watch.application_post_sequence = 2
        assert verification._owned_post(client, attempt, POPUP_FIELDS['resume_hash'], POPUP_FIELDS['letter']) is None
        await verification.reconcile_post_submit(client, attempt, POPUP_FIELDS['resume_hash'], POPUP_FIELDS['letter'])
        assert attempt._hh_reconciliation_diagnostic['step'] == 'owned_post'
        context.request.get.assert_not_awaited()
        assert getattr(attempt, '_hh_verified_receipt', None) is None
        telemetry = request(page, 'https://hh.ru/analytics/pageview')
        payload(telemetry, {'resume_hash': 'PRIVATE_VALUE'}, 'form')
        context.emit('request', telemetry)
        assert monitor.unknown and context._hh_action_watch.unknown
        assert monitor.last_unknown_diagnostic['requires_stop'] is True
        assert len(attempt._hh_application_posts) == 2
        return {'ok': True}

    result = asyncio.run(run_native_attempt(client, repository, URL, operation))
    assert result['uncertain'] and not result['ok']


@pytest.mark.parametrize('body, headers', [
    ('pageview=applied', {'content-type': 'text/plain;charset=UTF-8'}),
    ('event=applied', {'content-type': 'application/x-www-form-urlencoded'}),
    ('--fixture\r\nContent-Disposition: form-data; name="event"\r\n\r\napplied\r\n--fixture--\r\n',
     {'content-type': 'multipart/form-data; boundary=fixture'}),
    ('{"event":"applied","details":{"count":1}}', {'content-type': 'application/json'}),
    (None, {}),
])
@pytest.mark.parametrize('earlier_unknown', [False, True])
@pytest.mark.parametrize('post_status', [None, 500, 200])
def test_telemetry_never_clears_unknown_before_owned_post(tmp_path, monkeypatch, body, headers, earlier_unknown, post_status):
    monkeypatch.setenv('HH_PASSIVE_TELEMETRY_ENABLED', '0')
    client, context, page, monitor = monitored_client()
    repository = NativeApplyRepository(tmp_path / 'cookies.json', 'hh')
    event = request(page, 'https://hh.ru/analytics/pageview')
    event.post_data, event.headers = body, headers

    async def operation():
        attempt = client._external_attempt
        attempt.begin()
        if earlier_unknown:
            monitor.mark_unknown(reason='child_frame_interaction')
        attempt._hh_application_posts.append({'owned': True, 'status': post_status})
        context._hh_action_watch.application_post_sequence = 1
        context.emit('request', event)
        assert monitor.unknown is (earlier_unknown or post_status != 200)
        assert context._hh_action_watch.unknown is (earlier_unknown or post_status != 200)
        assert context._hh_action_watch.application_post_sequence == 1
        assert monitor.last_unknown_diagnostic['requires_stop'] is (post_status != 200)
        assert monitor.last_unknown_diagnostic['unknown_before'] is earlier_unknown
        return {'ok': True}

    result = asyncio.run(run_native_attempt(client, repository, URL, operation))
    assert bool(result.get('uncertain')) is (earlier_unknown or post_status != 200)


@pytest.mark.parametrize('failure', ['opaque', 'binary_buffer', 'malformed_json', 'malformed_multipart', 'compressed', 'unreadable'])
def test_unproven_payload_still_stops_after_owned_post(tmp_path, failure):
    client, context, page, monitor = monitored_client()
    repository = NativeApplyRepository(tmp_path / 'cookies.json', 'hh')
    event = request(page, 'https://hh.ru/unknown')
    event.headers = {'content-type': 'text/plain'}
    if failure == 'opaque':
        event.headers['content-type'] = 'application/octet-stream'
    elif failure == 'binary_buffer':
        event.post_data = None
        event.post_data_buffer = b'\x00\xff'
    elif failure == 'malformed_json':
        event.headers['content-type'] = 'application/json'
    elif failure == 'malformed_multipart':
        event.headers['content-type'] = 'multipart/form-data; boundary=missing'
    elif failure == 'compressed':
        event.headers['content-encoding'] = 'gzip'
    else:
        class Unreadable(SimpleNamespace):
            @property
            def post_data(self):
                raise RuntimeError('unreadable payload')

        event = Unreadable(method='POST', url=event.url, frame=event.frame, headers=event.headers)

    async def operation():
        client._external_attempt.begin()
        client._external_attempt._hh_application_posts.append({'owned': True, 'status': 200})
        context._hh_action_watch.application_post_sequence = 1
        context.emit('request', event)
        assert monitor.unknown and context._hh_action_watch.unknown
        assert context._hh_action_watch.application_post_sequence == 2
        posts = client._external_attempt._hh_application_posts
        assert len(posts) == 2 and posts[1]['owned'] is False
        diagnostic = posts[1]['classification']
        expected = {'opaque': 'opaque', 'binary_buffer': 'opaque', 'malformed_json': 'malformed',
                    'malformed_multipart': 'malformed', 'compressed': 'encoded', 'unreadable': 'unreadable'}[failure]
        assert diagnostic['body_state'] == expected
        assert diagnostic['application_reason'] == expected + '_body'
        return {'ok': True}

    result = asyncio.run(run_native_attempt(client, repository, URL, operation))
    assert result['uncertain'] and not result['ok']


def test_initial_unknown_blocks_reconciliation_even_with_owned_post(monkeypatch):
    from unittest.mock import AsyncMock

    context = SimpleNamespace(request=SimpleNamespace(get=AsyncMock()),
                              _hh_action_watch=SimpleNamespace(application_post_sequence=1, unknown=True))
    page = SimpleNamespace(_hh_action_monitor=SimpleNamespace(unknown=True))
    attempt = SimpleNamespace(page=page, context=context)
    session = SimpleNamespace(_page=page, _context=context, _external_attempt=attempt)
    proof = {'url': 'https://hh.ru' + RESPONSE_PATH}
    monkeypatch.setattr(verification, '_owned_post', lambda *args: proof)
    asyncio.run(verification.reconcile_post_submit(session, attempt, 'resume', 'letter'))
    assert attempt._hh_reconciliation_diagnostic['step'] == 'ownership_changed'
    assert set(attempt._hh_reconciliation_diagnostic['failed']) == {'page_monitor', 'context_watch'}
    context.request.get.assert_not_awaited()
    assert getattr(attempt, '_hh_verified_receipt', None) is None
