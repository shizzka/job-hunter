"""Post-submit telemetry shares no response payload; unknown evidence stays sticky."""
import asyncio
from types import SimpleNamespace

import pytest

from hh import verification
from hh.submit_boundary import RESPONSE_PATH
from state_store.native_apply import NativeApplyRepository, run_native_attempt
from tests.test_hh_recovery_attempt_window import URL, monitored_client, request


@pytest.mark.parametrize('method', ['GET', 'HEAD', 'POST', 'PUT', 'PATCH', 'DELETE'])
@pytest.mark.parametrize('key', ['resume_id', 'resumeId', 'resumeHash', 'resume_hash', 'resume', 'letter'])
@pytest.mark.parametrize('wire', ['query', 'form', 'multipart', 'multipart_escaped', 'multipart_continued', 'json', 'plain'])
def test_response_payload_on_unknown_path_counts_and_stops_after_owned_post(
        tmp_path, method, key, wire):
    client, context, page, monitor = monitored_client()
    repository = NativeApplyRepository(tmp_path / 'cookies.json', 'hh')
    event = request(page, 'https://spb.hh.ru/unknown', method)
    if wire == 'query':
        event.url += '?' + key + '='
        event.post_data = None
    elif wire == 'form':
        event.headers = {'content-type': 'application/x-www-form-urlencoded'}
        event.post_data = key.replace('_', '%5F') + '='
    elif wire.startswith('multipart'):
        event.headers = {'content-type': 'multipart/form-data; boundary=fixture'}
        name = ('name*0="' + key[:2] + '"; name*1="' + key[2:] + '"'
                if wire == 'multipart_continued' else
                'name="' + (key[:1] + '\\' + key[1:] if wire == 'multipart_escaped' else key) + '"')
        event.post_data = '--fixture\r\nContent-Disposition: form-data; ' + name + '\r\n\r\n\r\n--fixture--\r\n'
    elif wire == 'json':
        event.headers = {'content-type': 'application/json'}
        event.post_data = '{"nested":{"\\u%04x%s":""}}' % (ord(key[0]), key[1:])
    else:
        event.headers = {'content-type': 'text/plain'}
        event.post_data = key + '=\r\n'

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
def test_telemetry_never_clears_unknown_before_owned_post(tmp_path, body, headers, earlier_unknown, post_status):
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


@pytest.mark.parametrize('failure', ['opaque', 'malformed_json', 'malformed_multipart', 'compressed', 'unreadable'])
def test_unproven_payload_still_stops_after_owned_post(tmp_path, failure):
    client, context, page, monitor = monitored_client()
    repository = NativeApplyRepository(tmp_path / 'cookies.json', 'hh')
    event = request(page, 'https://hh.ru/unknown')
    event.headers = {'content-type': 'text/plain'}
    if failure == 'opaque':
        event.headers['content-type'] = 'application/octet-stream'
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
        context.emit('request', event)
        assert monitor.unknown and context._hh_action_watch.unknown
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
