"""Offline server diff evidence is independent of dirty HH session receipts."""
import asyncio
import json
import time
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from hh import delivery, observation, verification
from state_store.native_apply import NativeApplyRepository, NativeAttempt
from tests.test_hh_observation import Trace, TWO, VID, HASH
from tests.test_hh_recovery_attempt_window import monitored_client
from tests.test_hh_react_response_state_helpers import POPUP_FIELDS, _request


def html(vid, ids=(), *, resume_hash=None, vacancy=None, kind='RESPONSE_BY_APPLICANT', created=None):
    state = {'applicantVacancyResponseStatuses': {str(vid): {'negotiations': {
        'total': len(ids), 'topicList': [dict(id=i, vacancyId=int(vacancy or vid), resumeId=260248350,
            initialTopicType=kind, creationTimeMillis=created or int(time.time()*1000), hasResponseLetter=True)
            for i in ids]}}}, 'resumes': {}}
    if resume_hash is not None:
        state['resumes'] = {'260248350': {'hash': resume_hash}}
    return '<html><body><template id="HH-Lux-InitialState">' + json.dumps(state) + '</template></body></html>'


def setup(tmp_path, vid='138433749', topic=5657384020):
    client, context, page, monitor = monitored_client()
    url = 'https://spb.hh.ru/vacancy/' + vid
    repository = NativeApplyRepository(tmp_path / 'cookies.json', 'hh')
    owner = repository.claim(url, '', '')
    attempt = NativeAttempt(repository, url, owner, client=client)
    client._external_attempt = attempt
    monitor.register(attempt)
    trace = Trace()
    attempt._hh_reconciliation_trace = trace
    page.main_frame = SimpleNamespace(page=page)
    observation.snapshot(attempt, html(vid), vid, HASH, 'before', trace=trace)
    attempt.begin()
    fields = {**POPUP_FIELDS, 'vacancy_id':vid, 'resume_hash':HASH, 'letter':'PRIVATE_LETTER'}
    proof = {'owner': owner, 'used': False, 'kind':'hh-react-popup',
             'url': 'https://spb.hh.ru/applicant/vacancy_response/popup', 'fields':fields}
    page._hh_request_approval = proof
    request = _request(fields, url=proof['url'])
    request.frame = page.main_frame
    request.post_data = request.post_data_buffer.decode()
    request.all_headers = AsyncMock(return_value={'cookie':'hhtoken=PRIVATE_COOKIE'})
    context.emit('request', request)
    response = SimpleNamespace(url=proof['url'], request=request, status=200,
        headers={'content-type': 'application/json'}, body=AsyncMock(return_value=b'{}'))
    # Response observers schedule asynchronous body tasks, so emit in the loop.
    post = attempt._hh_application_posts[0]
    post['status'] = 200
    post['_completed_monotonic'] = time.monotonic()
    after = SimpleNamespace(status=200, url=url, headers={'content-type': 'text/html'},
        text=AsyncMock(return_value=html(vid, [topic])), dispose=AsyncMock())
    isolated = SimpleNamespace(get=AsyncMock(return_value=after), dispose=AsyncMock())
    context.cookies = AsyncMock(return_value=[{'name':'hhtoken','value':'PRIVATE_COOKIE','domain':'.hh.ru','path':'/'}])
    context.request = SimpleNamespace(get=AsyncMock())
    client._cookie_binding = SimpleNamespace(context=context, closing=False, revoked=False)
    client._pw = SimpleNamespace(request=SimpleNamespace(new_context=AsyncMock(return_value=isolated)))
    return client, attempt, trace, isolated


def run_check(client, attempt):
    asyncio.run(delivery.check_delivery(client, attempt, HASH, 'PRIVATE_LETTER'))
    return attempt._hh_server_delivery


def dirty(client, attempt):
    # Live shape: second malformed application POST and many unknown actions.
    context, page = attempt.context, attempt.page
    context.emit('request', SimpleNamespace(method='POST', url='https://spb.hh.ru/other',
        frame=page.main_frame, headers={'content-type':'application/json'}, post_data='{malformed'))
    for _ in range(30):
        page._hh_action_monitor.mark_unknown(reason='unclassified_event')


@pytest.mark.parametrize('vid,topic', [('138433749',5657384020), ('138473842',5657461156)])
def test_both_live_reconstructions_confirm_without_relaxing_safety(tmp_path, vid, topic):
    client, attempt, trace, isolated = setup(tmp_path, vid, topic)
    dirty(client, attempt)
    asyncio.run(verification.reconcile_post_submit(client, attempt, HASH, 'PRIVATE_LETTER'))
    record = attempt._hh_server_delivery
    assert record.status == 'confirmed' and record.topic_id == topic and record.resume_verified is None
    assert len(attempt._hh_application_posts) == 2
    assert attempt._hh_application_posts[1]['owned'] is False
    assert attempt.page._hh_action_monitor.unknown and attempt.context._hh_action_watch.unknown
    assert client._hh_recovery_stop_reason == 'possible_external_action'
    assert attempt.uncertain and not verification.can_continue_uncertain(client, attempt)
    assert not getattr(attempt, '_hh_verified_receipt', None)
    attempt.repository.transition(attempt.url, attempt.owner, 'uncertain')
    assert attempt.repository.get(attempt.url)['status'] == 'uncertain'
    assert not attempt.repository.claim(attempt.url, '', '')
    result = delivery.result_with_delivery({'ok':False, 'uncertain':True}, attempt)
    assert delivery.fact(result) is record
    assert delivery.fact({**result}) is None
    assert delivery.fact({'server_delivery': {'status':'confirmed'}, 'uncertain':True}) is None
    with pytest.raises(FrozenInstanceError):
        record.status = 'unknown'
    event = [e for e in trace.events if e['stage']=='HH_SERVER_DELIVERY'][-1]
    assert event['before_ids'] == [] and event['after_ids'] == event['new_ids'] == [topic]
    assert event['reads'] == 1 and event['has_response_letter'] is True
    assert event['post_to_confirmation_seconds'] >= 0
    assert 'PRIVATE_' not in json.dumps(trace.events)
    isolated.get.assert_awaited_once()
    attempt.context.request.get.assert_not_awaited()


@pytest.mark.parametrize('case,status', [('two','ambiguous'), ('foreign','ambiguous'), ('invitation','ambiguous'),
    ('old','ambiguous'), ('future','ambiguous'), ('mismatch','mismatch'), ('missing','unknown'),
    ('late_before','unknown'), ('late_post','unknown'), ('empty','not_seen'), ('mapped','confirmed')])
def test_diff_conditions(tmp_path, monkeypatch, case, status):
    client, attempt, trace, isolated = setup(tmp_path)
    monkeypatch.setattr(delivery.asyncio, 'sleep', AsyncMock())
    args = {}
    ids = [5657384020]
    if case == 'two': ids.append(5657384021)
    if case == 'foreign': args['vacancy'] = '42'
    if case == 'invitation': args['kind'] = 'INVITATION'
    if case == 'old': args['created'] = int((time.time()-601)*1000)
    if case == 'future': args['created'] = int((time.time()+601)*1000)
    if case in {'mismatch','mapped'}: args['resume_hash'] = 'different' if case=='mismatch' else HASH
    if case == 'missing': del attempt._hh_delivery_before
    if case == 'late_before':
        del attempt._hh_delivery_before
        observation.snapshot(attempt, html('138433749'), '138433749', HASH, 'before')
    if case == 'late_post': attempt._hh_application_posts[0]['_completed_monotonic'] = time.monotonic()+100
    if case == 'empty': ids = []
    isolated.get.return_value.text.return_value = html('138433749', ids, **args)
    record = run_check(client, attempt)
    assert record.status == status
    if case == 'empty':
        assert record.reads == 3 and isolated.get.await_count == 3
        attempt.repository.transition(attempt.url, attempt.owner, 'uncertain')
        assert not attempt.repository.claim(attempt.url, '', '')
    if case == 'mapped': assert record.resume_verified is True
    if case == 'mismatch': assert record.resume_verified is False


@pytest.mark.parametrize('kind', ['403','challenge','login','mime','parse','transport'])
def test_bad_read_stops_observation_only(tmp_path, kind):
    client, attempt, trace, isolated = setup(tmp_path)
    dirty(client, attempt)
    if kind=='403': isolated.get.return_value.status = 403
    if kind=='challenge': isolated.get.return_value.text.return_value = '<div class="captcha">Verify you are human</div>'
    if kind=='login': isolated.get.return_value.text.return_value = '<script>{"userType":"anonymous"}</script>'
    if kind=='mime': isolated.get.return_value.headers = {'content-type':'application/json'}
    if kind=='parse': isolated.get.return_value.text.return_value = '<template id="HH-Lux-InitialState">{bad}</template>'
    if kind=='transport': isolated.get.side_effect = RuntimeError('PRIVATE_ERROR')
    assert run_check(client, attempt).status == 'unknown'
    isolated.get.assert_awaited_once()
    assert attempt.context._hh_action_watch.unknown and client._hh_recovery_stop_reason == 'possible_external_action'


def test_existing_two_topics_never_confirm(tmp_path, monkeypatch):
    client, attempt, _, isolated = setup(tmp_path, VID)
    monkeypatch.setattr(delivery.asyncio, 'sleep', AsyncMock())
    # Reconstruct an already captured before with the B3-10 two-topic fixture.
    attempt.acting = False
    post = attempt._hh_application_posts.pop()
    attempt.repository.get = lambda url: {'owner':attempt.owner, 'status':'preparing' if not attempt.acting else 'acting'}
    del attempt._hh_delivery_before
    observation.snapshot(attempt, TWO, VID, HASH, 'before')
    attempt.acting = True
    post['_started_monotonic'] = time.monotonic()
    post['_completed_monotonic'] = time.monotonic()
    attempt._hh_application_posts.append(post)
    isolated.get.return_value.text.return_value = TWO
    assert run_check(client, attempt).status == 'not_seen'


def test_preflight_map_explicit_id_and_used_ids_are_not_positional(tmp_path):
    client, attempt, _, isolated = setup(tmp_path)
    state = {'applicantVacancyResponseStatuses': {'138433749': {
        'resumes': {HASH: {'id':260248350, 'hash':HASH}}, 'usedResumeIds':[123]}}}
    attempt.acting = False
    delivery.capture_resume_hashes(attempt, '<template id="HH-Lux-InitialState">'+json.dumps(state)+'</template>', '138433749')
    attempt.acting = True
    assert run_check(client, attempt).resume_verified is True
    state['applicantVacancyResponseStatuses']['138433749']['resumes'][HASH].pop('id')
    assert observation.resume_hashes(state, '138433749') == {}


@pytest.mark.parametrize('state', [
    '{broken',
    json.dumps({'resumes': {'260248350': {'hash':'wrong'}}, 'applicantVacancyResponseStatuses': {
        '138433749': {'resumes': {HASH: {'id':260248350, 'hash':HASH}}}}}),
])
def test_broken_or_conflicting_preflight_map_is_unknown(tmp_path, state):
    client, attempt, _, _ = setup(tmp_path)
    attempt.acting = False
    delivery.capture_resume_hashes(attempt, '<template id="HH-Lux-InitialState">'+state+'</template>', '138433749')
    attempt.acting = True
    assert run_check(client, attempt).status=='unknown'


@pytest.mark.parametrize('point', ['evaluate','entry','get','dispose','snapshot','trace'])
def test_new_exceptions_cannot_change_old_safety(tmp_path, monkeypatch, point):
    client, attempt, _, isolated = setup(tmp_path)
    dirty(client, attempt)
    def fail(*args, **kwargs): raise RuntimeError('PRIVATE_ERROR')
    if point=='evaluate': monkeypatch.setattr(delivery, '_evaluate', fail)
    if point=='entry': monkeypatch.setattr(delivery, 'check_delivery', AsyncMock(side_effect=RuntimeError('PRIVATE_ERROR')))
    if point in {'get','dispose'}: getattr(isolated, point).side_effect = RuntimeError('PRIVATE_ERROR')
    if point=='snapshot': monkeypatch.setattr(delivery, 'record_snapshot', fail)
    if point=='trace':
        event = attempt._hh_reconciliation_trace.event
        def fail_delivery(stage, **fields):
            if stage=='HH_SERVER_DELIVERY': fail()
            return event(stage, **fields)
        monkeypatch.setattr(attempt._hh_reconciliation_trace, 'event', fail_delivery)
    asyncio.run(verification.reconcile_post_submit(client, attempt, HASH, 'PRIVATE_LETTER'))
    assert attempt._hh_server_delivery.status == 'unknown'
    assert attempt.uncertain and client._hh_recovery_uncertain
    assert attempt.context._hh_action_watch.unknown and attempt.page._hh_action_monitor.unknown
    assert not getattr(attempt, '_hh_verified_receipt', None)


@pytest.mark.parametrize('stage', ['reentry', 'get_exception', 'after_get', 'http', 'after_get_exception'])
def test_delivery_runs_after_every_reconciliation_exit(tmp_path, stage):
    client, attempt, _, isolated = setup(tmp_path)
    response = isolated.get.return_value
    if stage == 'reentry': attempt._hh_reconciliation_started = True
    elif stage == 'get_exception': attempt.context.request.get.side_effect = RuntimeError('PRIVATE_ERROR')
    else:
        response.url = attempt.url
        attempt.context.request.get.return_value = response
        if stage=='http': response.status = 403
        if stage=='after_get_exception': attempt.context.cookies.side_effect = [
            [{'name':'hhtoken','value':'PRIVATE_COOKIE','domain':'.hh.ru','path':'/'}], RuntimeError('PRIVATE_ERROR')]
    asyncio.run(verification.reconcile_post_submit(client, attempt, HASH, 'PRIVATE_LETTER'))
    assert attempt._hh_server_delivery.status == ('unknown' if stage in {'http','get_exception'} else 'confirmed')
    if stage=='reentry': isolated.get.assert_awaited_once()
    else: isolated.get.assert_not_awaited()


def test_delayed_visibility_uses_at_most_three_reads(tmp_path, monkeypatch):
    client, attempt, _, isolated = setup(tmp_path)
    monkeypatch.setattr(delivery.asyncio, 'sleep', AsyncMock())
    isolated.get.return_value.text.side_effect = [html('138433749'), html('138433749'), html('138433749', [5657384020])]
    assert run_check(client, attempt).status == 'confirmed'
    assert attempt._hh_server_delivery.reads == 3
    assert isolated.get.await_count == 3
    asyncio.run(delivery.check_delivery(client, attempt, HASH, 'PRIVATE_LETTER'))
    assert isolated.get.await_count == 3


@pytest.mark.parametrize('alter', ['proof','letter','hash','vacancy','status','owner','before_time'])
def test_missing_or_mismatched_attempt_evidence_cannot_confirm(tmp_path, alter):
    client, attempt, _, isolated = setup(tmp_path)
    post = attempt._hh_application_posts[0]
    if alter=='proof': del post['_approval']
    if alter in {'letter','hash','vacancy'}:
        key = {'letter':'letter','hash':'resume_hash','vacancy':'vacancy_id'}[alter]
        post['_approval']['fields'][key] = '999'
    if alter=='status': post['status'] = 500
    if alter=='owner': attempt.owner = 'foreign'
    if alter=='before_time': post['_started_monotonic'] = attempt._hh_delivery_before.monotonic - 1
    assert run_check(client, attempt).status == 'unknown'
