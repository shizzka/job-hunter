"""Unknown-event evidence contains categories only; response GETs are counted."""
import asyncio
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hh.recovery import install_frame_observation
from hh import recovery
from hh.submit_boundary import RESPONSE_PATH, _install_popup_gate
from state_store.native_apply import NativeApplyRepository, run_native_attempt
from tests.test_hh_late_outcome_adversarial_e import Emitter
from tests.test_hh_recovery_attempt_window import URL, monitored_client, request
from tests.test_hh_post_submit_traffic import payload


@pytest.mark.parametrize('key', ['resume_id', 'resumeId', 'resumeHash', 'resume', 'letter'])
@pytest.mark.parametrize('source', ['main', 'child', 'worker'])
def test_response_get_counts_like_post_without_changing_post_receipts(tmp_path, key, source):
    client, context, page, monitor = monitored_client()
    page.main_frame = SimpleNamespace(page=page)
    repository = NativeApplyRepository(tmp_path / 'cookies.json', 'hh')

    async def operation():
        attempt = client._external_attempt
        for method in ('POST', 'GET', 'HEAD', 'PUT', 'PATCH', 'DELETE'):
            event = request(page, 'https://hh.ru' + RESPONSE_PATH + '?' + key + '=private', method)
            if source == 'main':
                event.frame = page.main_frame
            elif source == 'worker':
                event.frame = None
                event.service_worker = object()
            before = context._hh_action_watch.application_post_sequence
            context.emit('request', event)
            assert context._hh_action_watch.application_post_sequence == before + 1
        # Reconciliation still accepts only actual POST receipts.
        assert len(attempt._hh_application_posts) == 1
        assert attempt._hh_application_posts[0]['request'].method == 'POST'
        assert attempt.uncertain
        return {'ok': False}

    asyncio.run(run_native_attempt(client, repository, URL, operation))
    assert repository.get(URL)['status'] == 'uncertain'


@pytest.mark.parametrize('path', [RESPONSE_PATH, RESPONSE_PATH + '/popup'])
@pytest.mark.parametrize('method', ['GET', 'HEAD'])
def test_response_entry_navigation_does_not_increment_sequence(path, method):
    client, context, page, monitor = monitored_client()
    context.emit('request', request(page, 'https://hh.ru' + path + '?vacancyId=42', method))
    assert context._hh_action_watch.application_post_sequence == 0
    assert context._hh_action_watch.observations == 0


@pytest.mark.parametrize('key', ['resume_id', 'resumeId', 'resumeHash', 'resume', 'letter'])
@pytest.mark.parametrize('method', ['GET', 'HEAD'])
def test_response_controls_on_unknown_hh_path_also_increment_sequence(key, method):
    client, context, page, monitor = monitored_client()
    context.emit('request', request(page, 'https://spb.hh.ru/unknown?' + key + '=private', method))
    assert context._hh_action_watch.application_post_sequence == 1


@pytest.mark.parametrize('source', ['main', 'child', 'worker', 'unmonitored'])
@pytest.mark.parametrize('host', ['hh.ru', 'spb.hh.ru', 'PRIVATE_HOST.invalid'])
@pytest.mark.parametrize('path,expected_path', [
    (RESPONSE_PATH + '/popup', RESPONSE_PATH + '/popup'),
    ('/resume/PRIVATE_PATH', '/[other]'),
    ('/vacancy/PRIVATE_PATH', '/vacancy/[id]'),
    (RESPONSE_PATH + '/PRIVATE_PATH', RESPONSE_PATH + '/[other]'),
])
def test_unknown_classification_is_logged_and_traced_without_private_values(
        tmp_path, caplog, source, host, path, expected_path):
    client, context, page, monitor = monitored_client()
    page.main_frame = SimpleNamespace(page=page)
    repository = NativeApplyRepository(tmp_path / 'cookies.json', 'hh')
    traced = []
    page.content = AsyncMock(return_value='<body>PRIVATE_PAGE_BODY</body>')
    context.cookies = AsyncMock(return_value=[{'name': 'hhtoken', 'value': 'PRIVATE_COOKIE'}])
    unmonitored_page = Emitter()
    unmonitored_page.main_frame = object()
    caplog.set_level(logging.INFO, logger='hh_recovery')

    class PrivateRequest:
        method = 'GET'
        url = ('https://PRIVATE_USER:PRIVATE_PASSWORD@' + host + path
               + '?resume_id=PRIVATE_RESUME&resumeHash=PRIVATE_HASH&letter=PRIVATE_LETTER'
               + '&PRIVATE_QUERY_KEY=PRIVATE_QUERY_VALUE#PRIVATE_FRAGMENT')
        service_worker = object() if source == 'worker' else None

        @property
        def frame(self):
            if source == 'worker':
                raise RuntimeError('PRIVATE_ERROR')
            if source == 'main':
                return page.main_frame
            return SimpleNamespace(page=page if source == 'child' else unmonitored_page)

        @property
        def headers(self):
            pytest.fail('diagnostics must not read cookies or headers: PRIVATE_COOKIE')

        @property
        def post_data(self):
            return None

    async def operation():
        attempt = client._external_attempt
        attempt._hh_reconciliation_trace = SimpleNamespace(
            event=lambda stage, **fields: traced.append({'stage': stage, **fields}))
        context.emit('request', PrivateRequest())
        return {'ok': False}

    asyncio.run(run_native_attempt(client, repository, URL, operation))
    diagnostic = context._hh_action_watch.last_unknown_diagnostic
    assert diagnostic == {
        'reason': 'unobserved_frame' if source in {'worker', 'unmonitored'} else 'unapproved_mutating_request',
        'method': 'GET', 'path': expected_path,
        'path_category': ('response' if path.startswith(RESPONSE_PATH) else
                          'vacancy' if path.startswith('/vacancy/') else 'other'),
        'host': 'third_party' if host == 'PRIVATE_HOST.invalid' else 'hh',
        'frame': 'child' if source == 'unmonitored' else source,
        'query_fields': ['letter', 'resumeHash', 'resume_id'],
        'field_sources': {'query': ['letter', 'resumeHash', 'resume_id']},
        'body_state': 'empty',
        'application_reason': 'none' if host == 'PRIVATE_HOST.invalid' else 'query_fields',
        'scope': 'context', 'observation': 1,
        'requires_stop': host != 'PRIVATE_HOST.invalid', 'unknown_before': False,
    }
    assert context._hh_action_watch.unknown is diagnostic['requires_stop']
    assert [row['scope'] for row in traced] == (
        ['context'] if source in {'worker', 'unmonitored'} else ['page', 'context'])
    assert all(row['stage'] == 'HH_UNKNOWN_ACTION' for row in traced)
    page.content.assert_not_called()
    context.cookies.assert_not_called()
    assert 'PRIVATE_' not in json.dumps([diagnostic, monitor.last_unknown_diagnostic, traced])
    assert 'PRIVATE_' not in caplog.text


@pytest.mark.parametrize('wire,source', [
    ('form', 'form'), ('multipart', 'multipart'), ('multipart_escaped', 'multipart'),
    ('multipart_continued', 'multipart'), ('json', 'json'), ('plain', 'body'),
])
def test_body_field_evidence_in_unknown_trace_and_post_accounting_is_private(tmp_path, caplog, wire, source):
    client, context, page, monitor = monitored_client()
    repository = NativeApplyRepository(tmp_path / 'cookies.json', 'hh')
    event = request(page, 'https://hh.ru/analytics/PRIVATE_PATH?PRIVATE_KEY=PRIVATE_QUERY')
    payload(event, {'resume_hash': 'PRIVATE_RESUME', 'vacancyId': 'PRIVATE_VACANCY',
                    'letter': 'PRIVATE_LETTER', 'PRIVATE_FIELD': 'PRIVATE_BODY'}, wire)
    event.headers['cookie'] = 'hhtoken=PRIVATE_COOKIE'
    event.headers['authorization'] = 'PRIVATE_AUTHORIZATION'
    traced = []
    caplog.set_level(logging.INFO, logger='hh_recovery')

    async def operation():
        attempt = client._external_attempt
        attempt.begin()
        attempt._hh_reconciliation_trace = SimpleNamespace(
            event=lambda stage, **fields: traced.append({'stage': stage, **fields}))
        context.emit('request', event)
        post, = attempt._hh_application_posts
        classification = post['classification']
        assert classification['field_sources'] == {source: ['letter', 'resume_hash', 'vacancyId']}
        assert classification['body_state'] == 'readable'
        assert classification['path'] == '/[other]'
        assert classification['path_category'] == 'analytics'
        assert classification['application_reason'] == 'resume_and_vacancy_fields'
        assert [row['scope'] for row in traced] == ['page', 'context']
        assert all(row['field_sources'] == classification['field_sources'] for row in traced)
        assert all(row['requires_stop'] for row in traced)
        assert 'PRIVATE_' not in json.dumps([classification, traced]) + caplog.text
        assert monitor.unknown and context._hh_action_watch.unknown
        return {'ok': False}

    asyncio.run(run_native_attempt(client, repository, URL, operation))


@pytest.mark.parametrize('event,reason,frame', [
    ('popup', 'popup_created', 'main'),
    ('worker', 'page_worker_created', 'worker'),
    ('serviceworker', 'service_worker_created', 'worker'),
    ('websocket', 'websocket_frame_sent', 'unknown'),
    ('interaction', 'child_frame_interaction', 'child'),
    ('malformed', 'request_observation_error', 'unknown'),
    ('gate', 'popup_gate_rejected', 'main'),
])
def test_unknown_event_call_sites_name_the_trigger(event, reason, frame, caplog):
    client, context, page, monitor = monitored_client()
    page.main_frame = SimpleNamespace(page=page)
    # A manual claim is sufficient to make every non-request event sticky.
    client._manual_apply_guard = object()
    caplog.set_level(logging.INFO, logger='hh_recovery')
    node = Emitter()
    node.url = 'https://hh.ru/resume/PRIVATE_PATH?letter=PRIVATE_QUERY'
    if event == 'serviceworker':
        context.emit(event, node)
    elif event == 'websocket':
        page.emit(event, node)
        node.emit('framesent', 'PRIVATE_SOCKET_BODY')
    elif event == 'malformed':
        context.emit('request', object())
    elif event == 'interaction':
        async def expose(name, callback):
            context.interaction = callback

        async def init(**kwargs):
            pass

        context.expose_binding, context.add_init_script = expose, init
        asyncio.run(install_frame_observation(client, context))
        node.page = page
        context.interaction({'context': context, 'page': page, 'frame': node})
    elif event == 'gate':
        async def route(pattern, callback):
            page.gate = callback

        page.route = route
        route_request = request(page, 'https://hh.ru' + RESPONSE_PATH + '/popup')
        route_request.frame = page.main_frame
        route_stub = SimpleNamespace(request=route_request, abort=AsyncMock())
        asyncio.run(_install_popup_gate(client, 'fixture-boundary', {}))
        asyncio.run(page.gate(route_stub))
        route_stub.abort.assert_awaited_once()
    else:
        page.emit(event, node)
    diagnostic = context._hh_action_watch.last_unknown_diagnostic
    assert diagnostic['reason'] == reason
    assert diagnostic['frame'] == frame
    assert diagnostic['requires_stop']
    assert context._hh_action_watch.unknown
    assert 'PRIVATE_' not in json.dumps(diagnostic) + caplog.text


def test_diagnostic_trace_failure_cannot_prevent_unknown(tmp_path):
    client, context, page, monitor = monitored_client()
    repository = NativeApplyRepository(tmp_path / 'cookies.json', 'hh')

    def broken_trace(*args, **kwargs):
        raise RuntimeError('PRIVATE_TRACE_ERROR')

    async def operation():
        client._external_attempt._hh_reconciliation_trace = SimpleNamespace(event=broken_trace)
        context.emit('request', request(page, 'https://hh.ru/unknown'))
        assert monitor.unknown and context._hh_action_watch.unknown
        return {'ok': False}

    asyncio.run(run_native_attempt(client, repository, URL, operation))
    assert repository.get(URL)['status'] == 'uncertain'


def test_diagnostic_failure_cannot_prevent_unknown(tmp_path, monkeypatch):
    client, context, page, monitor = monitored_client()
    repository = NativeApplyRepository(tmp_path / 'cookies.json', 'hh')

    def broken_diagnostic(*args, **kwargs):
        raise RuntimeError('PRIVATE_DIAGNOSTIC_ERROR')

    monkeypatch.setattr(recovery, '_record_unknown', broken_diagnostic)

    async def operation():
        context.emit('request', request(page, 'https://hh.ru/unknown'))
        assert monitor.unknown and context._hh_action_watch.unknown
        assert client._external_attempt.uncertain
        return {'ok': False}

    asyncio.run(run_native_attempt(client, repository, URL, operation))
    assert repository.get(URL)['status'] == 'uncertain'
