"""B3-10: offline, private observations cannot become confirmation evidence."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hh import observation, verification
from tests.test_hh_recovery_attempt_window import monitored_client, request

FIXTURES = Path(__file__).parent / 'fixtures'
TWO = (FIXTURES / 'hh_negotiations_two.html').read_text()
ZERO = (FIXTURES / 'hh_negotiations_zero.html').read_text()
VID = '137870570'
HASH = 'synthetic-resume-260248350'


class Trace:
    last_stage = 'PRE_SUBMIT_VERIFY'

    def __init__(self):
        self.events = []

    def event(self, stage, **fields):
        self.last_stage = stage
        self.events.append({'stage': stage, **fields})


def test_saved_page_negotiations_and_resume_hashes():
    result = observation.negotiations_from_html(TWO, VID)
    assert result['found'] and result['total'] == 2
    assert {t['resumeId'] for t in result['topicList']} == {260248350, 272370847}
    assert result['topicList'][0] == {
        'id': 5611332061, 'vacancyId': 137870570, 'resumeId': 260248350,
        'creationTimeMillis': 1790627591201, 'initialTopicType': 'RESPONSE_BY_APPLICANT',
        'archived': False, 'discarded': True, 'hasResponseLetter': True}
    assert result['resume_hashes']['260248350'] == HASH
    assert observation.negotiations_from_html(ZERO, '138151000') == {
        'found': True, 'total': 0, 'topicList': [], 'resume_hashes': {}}


@pytest.mark.parametrize('html,reason', [
    ('<body>No template</body>', 'template_missing'),
    ('<template id="HH-Lux-InitialState">{bad}</template>', 'invalid_json'),
    ('<template id="HH-Lux-InitialState">{}', 'template_unclosed'),
    ('<template id="HH-Lux-InitialState">{}</template>', 'vacancy_missing'),
    (ZERO, 'vacancy_missing'),
    ('<template id="HH-Lux-InitialState">[]</template>', 'invalid_state'),
])
def test_missing_state_is_explicit_without_exception(html, reason):
    assert observation.negotiations_from_html(html, VID) == {'found': False, 'reason': reason}


def test_creation_time_fallback_and_entity_decoding():
    html = TWO.replace('"creationTimeMillis": 1790627591201,', '')
    assert observation.negotiations_from_html(html, VID)['topicList'][0]['creationTimeMillis'] == 1790627591201
    parser = observation._InitialState()
    parser.feed(html)
    raw = ''.join(parser.parts)
    encoded = html.replace(raw, raw.replace('"', '&quot;'))
    assert observation.negotiations_from_html(encoded, VID)['total'] == 2


def test_snapshot_diff_same_resume_repeat_and_unknown_baseline():
    trace, attempt = Trace(), SimpleNamespace()
    before = TWO.replace('"id": 5611332061', '"id": 111')
    observation.snapshot(attempt, before, VID, HASH, 'before', trace=trace)
    observation.snapshot(attempt, TWO, VID, HASH, 'after', trace=trace, read_number=1)
    observation.snapshot(attempt, TWO, VID, HASH, 'after', trace=trace, read_number=2)
    _, first, second = trace.events
    assert first['before_ids'] == [111, 5610954551]
    assert first['after_ids'] == [5610954551, 5611332061]
    assert first['new_ids'] == second['new_ids'] == [5611332061]
    assert first['new_topics'][0]['resumeId'] == 260248350
    assert first['new_topics'][0]['resume_hash_matches'] is True
    assert first['topics'][1]['resume_hash_matches'] is False
    assert first['new_topics'][0]['creationTimeMillis'] == 1790627591201
    # Repeating an existing list never manufactures a new topic.
    observation.snapshot(attempt, TWO, VID, HASH, 'before', trace=trace)
    observation.snapshot(attempt, TWO, VID, HASH, 'after', trace=trace, read_number=1)
    assert trace.events[-1]['new_ids'] == []
    observation.snapshot(SimpleNamespace(), TWO, VID, HASH, 'after', trace=trace)
    assert trace.events[-1]['new_ids'] is None
    assert trace.last_stage == 'PRE_SUBMIT_VERIFY'
    assert HASH not in json.dumps(trace.events)
    assert all(e['read_completed_monotonic'] > 0 for e in trace.events)


@pytest.mark.parametrize('body,category', [(b'', 'empty'), (b' \n', 'empty'),
                                          (b'<html>PRIVATE_LETTER</html>', 'not_json'),
                                          (b'{broken', 'not_json')])
def test_non_json_receipts_have_no_content(body, category):
    result = observation.sanitize_receipt(body, 'text/html; charset=utf-8')
    assert result == {'body_kind': category, 'size_bytes': len(body), 'content_type': 'text/html'}


def test_receipt_allowlist_rejects_payloads_even_under_allowed_keys():
    private = 'Private Person private@example.test PRIVATE_LETTER _xsrf=secret cookie=secret'
    data = {'topic_id': '123', 'chat_id': 456, 'responseStatus': 'SPAM',
            'letter': private, 'xsrf': private, 'cookies': private, 'headers': {'Set-Cookie': private},
            'errors': [{'code': 'SPAM', 'message': private}, {'code': 403}, {'code': private}],
            'alreadyApplied': {'letter': private}}
    result = observation.sanitize_receipt(json.dumps(data).encode(), 'application/json; secret=PRIVATE')
    assert result['topic_id'] == 123 and result['chat_id'] == 456
    assert result['responseStatus'] == 'SPAM'
    assert result['SPAM'] is True and result['alreadyApplied'] is True
    assert result['error_codes'] == ['SPAM', 403, 'other']
    assert private not in json.dumps(result) and 'secret' not in json.dumps(result)
    malicious = observation.sanitize_receipt(json.dumps({
        'topic_id': private, 'chat_id': private, 'responseStatus': private, 'error': {'value': private},
    }).encode(), private)
    assert malicious['topic_id'] is None and malicious['responseStatus'] == 'other'
    assert private not in json.dumps(malicious)


@pytest.mark.parametrize('owned,path,readable', [
    (True, '/applicant/vacancy_response/popup', True),
    (True, '/applicant/%76acancy_response/popup/', True),
    (True, '/applicant/vacancy_response', True),
    (True, '/applicant/vacancy_response/popup', False),
    (False, '/applicant/vacancy_response/popup', True),
    (True, '/applicant/vacancy_response/other', True),
])
def test_response_request_identity_and_completion_time(owned, path, readable):
    async def run():
        client, context, page, monitor = monitored_client()
        req = request(page, 'https://hh.ru' + path)
        post = {'request': req, 'owned': owned, 'status': None}
        trace = Trace()
        attempt = SimpleNamespace(_hh_application_posts=[post], _hh_reconciliation_trace=trace)
        monitor.attempts['test'] = attempt
        response = SimpleNamespace(url=req.url, request=req, status=200, headers={'content-type': 'application/json'},
            body=AsyncMock(return_value=b'{"topic_id":123,"letter":"PRIVATE_LETTER"}',
                           side_effect=None if readable else RuntimeError('PRIVATE_ERROR')))
        # An unrelated request with identical URL is never our receipt.
        context.emit('response', SimpleNamespace(**{**vars(response), 'request': request(page, req.url)}))
        assert post['status'] is None
        context.emit('response', response)
        context.emit('requestfinished', req)
        await observation.finish_receipts(attempt)
        assert post['status'] == 200
        expected = owned and not path.endswith('/other')
        receipts = [event for event in trace.events if event['stage'] == 'HH_POST_RECEIPT']
        assert len(receipts) == int(expected)
        if expected:
            event = receipts[0]
            assert event['stage'] == 'HH_POST_RECEIPT'
            assert event['post_completed_monotonic'] == post['_completed_monotonic']
            assert event['body_kind'] == ('json' if readable else 'unreadable')
            assert 'PRIVATE_' not in json.dumps(event)
        assert trace.last_stage == 'PRE_SUBMIT_VERIFY'
    asyncio.run(run())


def observation_session():
    trace = Trace()
    context = SimpleNamespace(cookies=AsyncMock(return_value=[{
        'name': 'hhtoken', 'value': 'PRIVATE_COOKIE', 'domain': 'hh.ru', 'path': '/'}]),
        request=SimpleNamespace(get=AsyncMock()))
    page = SimpleNamespace(context=context)
    repository = SimpleNamespace(source='hh', get=lambda url: {'owner': 'test-owner'})
    attempt = SimpleNamespace(page=page, context=context, client=None, repository=repository,
        abandoned=False, owner='test-owner', url='https://hh.ru/vacancy/' + VID, uncertain=True,
        _hh_reconciliation_trace=trace)
    isolated_response = SimpleNamespace(text=AsyncMock(return_value=TWO), dispose=AsyncMock())
    isolated = SimpleNamespace(get=AsyncMock(return_value=isolated_response), dispose=AsyncMock())
    factory = AsyncMock(return_value=isolated)
    session = SimpleNamespace(_page=page, _context=context, _external_attempt=attempt,
        _cookie_binding=SimpleNamespace(context=context, closing=False, revoked=False),
        _pw=SimpleNamespace(request=SimpleNamespace(new_context=factory)))
    attempt.client = session
    return session, attempt, trace, isolated, factory


@pytest.mark.parametrize('failure', [None, 'get', 'text', 'dispose', 'parser'])
def test_one_observation_after_early_refusal_never_verifies_or_allows_continuation(monkeypatch, failure):
    session, attempt, trace, isolated, factory = observation_session()
    monkeypatch.setattr(verification, '_owned_post', lambda *args: None)
    if failure in {'get', 'dispose'}:
        getattr(isolated, failure).side_effect = RuntimeError('PRIVATE_ERROR')
    elif failure == 'text':
        isolated.get.return_value.text.side_effect = RuntimeError('PRIVATE_ERROR')
    elif failure == 'parser':
        monkeypatch.setattr(observation, 'negotiations_from_html', lambda *args: 1 / 0)
    async def run():
        await verification.reconcile_post_submit(session, attempt, HASH, 'PRIVATE_LETTER')
        await verification.reconcile_post_submit(session, attempt, HASH, 'PRIVATE_LETTER')
        assert not await verification.prepare_uncertain_continuation(
            session, attempt, {'ok': False, 'uncertain': True}, HASH, 'PRIVATE_LETTER')
    asyncio.run(run())
    isolated.get.assert_awaited_once_with('https://hh.ru/vacancy/' + VID, max_redirects=0,
        max_retries=0, timeout=15000, headers={'Cache-Control': 'no-cache'})
    factory.assert_awaited_once()
    assert factory.call_args.kwargs['storage_state']['cookies'][0]['value'] == 'PRIVATE_COOKIE'
    session._context.request.get.assert_not_awaited()
    isolated.dispose.assert_awaited_once()
    assert attempt.uncertain and not getattr(attempt, '_hh_verified_receipt', None)
    assert not getattr(attempt, '_hh_uncertain_session', None)
    assert not getattr(session, '_hh_verified_attempt', None)
    assert not getattr(session, '_hh_recovery_stop_reason', None)
    assert attempt._hh_reconciliation_diagnostic == {'step': 'owned_post'}
    events = [e for e in trace.events if e['stage'] == 'HH_NEGOTIATIONS_SNAPSHOT']
    assert events and events[0]['phase'] == 'after_observation'
    assert 'PRIVATE_' not in json.dumps(trace.events)


@pytest.mark.parametrize('changed_at', ['before', 'cookies', 'context_creation'])
def test_observation_never_reads_for_a_foreign_owner(monkeypatch, changed_at):
    session, attempt, trace, isolated, factory = observation_session()
    if changed_at == 'before':
        session._external_attempt = object()
    elif changed_at == 'cookies':
        async def cookies(*args):
            session._external_attempt = object()
            return []
        session._context.cookies.side_effect = cookies
    else:
        async def new_context(**kwargs):
            session._external_attempt = object()
            return isolated
        factory.side_effect = new_context
    asyncio.run(observation.after_observation(session, attempt, HASH))
    isolated.get.assert_not_awaited()
    assert trace.events[-1]['error_kind'] in {'ownership_unproven', 'ownership_changed'}


@pytest.mark.parametrize('stale,confirmed,parser_error', [
    (False, True, False), (False, True, True), (True, True, False), (True, True, True),
    (True, False, False), (True, False, True),
])
def test_browser_apply_outcomes_unchanged_by_parser_failure(tmp_path, monkeypatch, stale, confirmed, parser_error):
    from tests.test_e2e_hh_apply_happy_path import run_hh_gate
    from tests.test_hh_post_submit_verification import APPLIED_HTML
    from tests.test_hh_uncertain_continuation import UNCONFIRMED_HTML
    if parser_error:
        monkeypatch.setattr(observation, 'negotiations_from_html', lambda *args: 1 / 0)
    gate = asyncio.run(run_hh_gate(tmp_path, monkeypatch, noise=False, stale_receipt=stale,
        continue_after_apply=stale, confirmation_html=APPLIED_HTML if confirmed else UNCONFIRMED_HTML))
    assert len(gate.application_posts) == 1
    assert gate.entries['2']['action'] == ('applied' if confirmed else 'apply_uncertain')
    assert not gate.result['hh_recovery']['stopped']
    if gate.trace is None:
        return
    events = [e for e in gate.trace._events if e['stage'] == 'HH_NEGOTIATIONS_SNAPSHOT']
    assert events and events[0]['phase'] == 'before'
    if stale:
        after = [e for e in events if e['phase'] == 'after']
        assert [e['read_number'] for e in after] == ([1] if confirmed else [1, 2])
    if parser_error:
        assert all(e.get('error_kind') == 'ZeroDivisionError' for e in events)
    receipts = [e for e in gate.trace._events if e['stage'] == 'HH_POST_RECEIPT']
    # A native form redirect can destroy the response resource before CDP reads
    # its body. That category is explicit and must preserve the same outcome.
    assert len(receipts) == 1 and receipts[0]['body_kind'] in {'json', 'unreadable'}


@pytest.mark.parametrize('failure', [None, 'http', 'ownership', 'get', 'body'])
def test_each_reconciliation_get_has_snapshot_without_extra_get(monkeypatch, failure):
    from tests.test_hh_post_submit_verification import APPLIED_HTML
    session, attempt, trace, isolated, factory = observation_session()
    context, page = attempt.context, attempt.page
    proof = {'url': 'https://hh.ru/applicant/vacancy_response/popup', 'fields': {'vacancy_id': VID}}
    monkeypatch.setattr(verification, '_owned_post', lambda *args: proof)
    attempt._hh_application_posts = [{'request': SimpleNamespace(
        all_headers=AsyncMock(return_value={'cookie': 'hhtoken=PRIVATE_COOKIE'}))}]
    context._hh_action_watch = SimpleNamespace(application_post_sequence=1, unknown=False)
    page._hh_action_monitor = SimpleNamespace(unknown=False)
    response = SimpleNamespace(status=500 if failure == 'http' else 200,
        url=attempt.url, headers={'content-type': 'text/html'},
        text=AsyncMock(return_value=APPLIED_HTML + TWO,
                       side_effect=RuntimeError('PRIVATE_ERROR') if failure == 'body' else None),
        dispose=AsyncMock())
    async def get(*args, **kwargs):
        if failure == 'get':
            raise RuntimeError('PRIVATE_ERROR')
        if failure == 'ownership':
            session._external_attempt = object()
        return response
    context.request.get.side_effect = get
    asyncio.run(verification.reconcile_post_submit(session, attempt, HASH, 'PRIVATE_LETTER'))
    context.request.get.assert_awaited_once()
    factory.assert_not_awaited()
    isolated.get.assert_not_awaited()
    events = [e for e in trace.events if e['stage'] == 'HH_NEGOTIATIONS_SNAPSHOT']
    assert len(events) == 1 and events[0]['phase'] == 'after' and events[0]['read_number'] == 1
    assert events[0]['found'] is (failure not in {'get', 'body'})
    assert bool(getattr(attempt, '_hh_verified_receipt', None)) is (failure is None)
    if failure is None:
        response.text.assert_awaited_once()
    assert 'PRIVATE_' not in json.dumps(trace.events)


def test_real_browser_post_body_is_sanitized_offline(tmp_path):
    from playwright.async_api import async_playwright
    from hh.recovery import monitor_page
    async def run():
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True, args=[
                '--disable-background-networking', '--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE localhost'])
            try:
                context = await browser.new_context(service_workers='block')
                trace = Trace()
                client = SimpleNamespace(_context=context, _external_attempt=None)
                page = await context.new_page()
                client._page = page
                monitor = monitor_page(client, page)
                attempt = SimpleNamespace(_hh_application_posts=[], _hh_reconciliation_trace=trace)
                monitor.attempts['test'] = attempt
                async def offline(route):
                    if route.request.method == 'POST':
                        # Model the already-approved request without invoking any
                        # application action or granting new approval in production.
                        attempt._hh_application_posts.append({
                            'request': route.request, 'owned': True, 'status': None})
                        await route.fulfill(content_type='application/json', body=json.dumps({
                            'topic_id': 123, 'chat_id': 456, 'responseStatus': 'SUCCESS',
                            'letter': 'PRIVATE_LETTER', 'xsrf': 'PRIVATE_XSRF', 'cookies': 'PRIVATE_COOKIE'}))
                    else:
                        await route.fulfill(content_type='text/html', body='<body>Offline fixture</body>')
                await context.route('**/*', offline)
                await page.goto('https://hh.ru/vacancy/42')
                await page.evaluate("fetch('/applicant/vacancy_response/popup', {method:'POST'}).then(r=>r.json())")
                await observation.finish_receipts(attempt)
                receipts = [e for e in trace.events if e['stage'] == 'HH_POST_RECEIPT']
                assert len(receipts) == 1 and receipts[0]['body_kind'] == 'json'
                assert receipts[0]['topic_id'] == 123 and receipts[0]['chat_id'] == 456
                assert receipts[0]['content_type'] == 'application/json'
                assert 'PRIVATE_' not in json.dumps(trace.events)
            finally:
                await browser.close()
    asyncio.run(run())


def test_template_json_keeps_embedded_markup_and_entities_verbatim():
    parser = observation._InitialState()
    parser.feed(TWO)
    data = json.loads(''.join(parser.parts))
    data['unrelated'] = {'description': '<B data-x="value">&quot;Name&quot;</B><!--comment-->',
                         'text': '&amp; and &#34;'}
    html = '<template id="HH-Lux-InitialState">' + json.dumps(data) + '</template>'
    result = observation.negotiations_from_html(html, VID)
    assert result['found'] and result['total'] == 2 and result['resume_hashes']['260248350'] == HASH
