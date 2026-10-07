"""A-01 adversarial desired invariants; all state and transports are synthetic."""
import asyncio
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import agent
import analytics
import apply_orchestrator
import config
import manual_apply_queue
import notifier
import seen
from state_store.native_apply import NativeApplyRepository, run_native_attempt
from state_store.private_journal import read_json_records
from tests.test_observability import configure_synthetic_search


@pytest.fixture(autouse=True)
def isolated_a01(tmp_path, monkeypatch):
    def deny(*args, **kwargs):
        raise AssertionError('No network in uncertainty regression')
    monkeypatch.setattr(socket.socket, 'connect', deny)
    monkeypatch.setattr(socket.socket, 'connect_ex', deny)
    for key, name in {'JOB_HUNTER_HOME':'', 'RUN_HISTORY_FILE':'runs.jsonl',
        'RUNTIME_STATUS_FILE':'runtime.json', 'SEEN_VACANCIES_FILE':'seen.json',
        'HH_STATE_DIR':'hh', 'HH_GUARD_STATE_FILE':'guard.json',
        'ANALYTICS_EVENTS_FILE':'events.jsonl', 'ANALYTICS_STATE_FILE':'analytics.json',
        'HH_COOKIES_FILE':'hh-cookies.json', 'HABR_COOKIES_FILE':'habr-cookies.json',
        'GEEKJOB_COOKIES_FILE':'geekjob-cookies.json'}.items():
        monkeypatch.setattr(config, key, str(tmp_path / name))
    monkeypatch.setattr(config, 'ANALYTICS_ENABLED', True)
    return tmp_path


@pytest.mark.parametrize('source', ['hh', 'habr'])
@pytest.mark.parametrize('result', [
    {'ok':False, 'uncertain':True},
    {'ok':True, 'uncertain':True},
])
def test_native_explicit_uncertainty_is_durable_before_success(source, result, isolated_a01):
    repository = NativeApplyRepository(isolated_a01 / 'cookies.json', source)
    url = 'https://hh.ru/vacancy/111' if source == 'hh' else 'https://career.habr.com/vacancies/111'
    client = SimpleNamespace()
    async def operation():
        return dict(result)
    returned = asyncio.run(run_native_attempt(client, repository, url, operation))
    assert returned['ok'] is False and returned['uncertain'] is True
    assert repository.get(url)['status'] == 'uncertain'
    assert not repository.claim(url, '', '')


def test_owned_zero_dispatch_does_not_erase_separately_reported_uncertainty(isolated_a01):
    repository = NativeApplyRepository(isolated_a01 / 'cookies.json', 'hh')
    url = 'https://hh.ru/vacancy/111'
    client = SimpleNamespace()
    async def operation():
        client._external_attempt.begin()
        client._external_attempt.confirm_no_action()
        return {'ok':False, 'uncertain':True, 'message':'Independent possible action'}
    returned = asyncio.run(run_native_attempt(client, repository, url, operation))
    assert returned['uncertain'] and not returned['ok']
    assert repository.get(url)['status'] == 'uncertain'
    assert not repository.claim(url, '', '')


@pytest.mark.parametrize('source,lower_result', [
    ('hh', {'ok':False, 'uncertain':True}),
    ('habr', {'ok':False, 'uncertain':True}),
    ('geekjob', {'ok':False, 'submission_status':'uncertain'}),
    ('geekjob', {'ok':False, 'submission_status':'acting'}),
    ('geekjob', {'ok':False, 'submission_status':'preparing'}),
    ('hh', {'ok':True, 'uncertain':True, 'already_applied':True}),
])
def test_dispatch_analytics_reports_uncertain_before_success(source, lower_result, monkeypatch):
    monkeypatch.setattr(config, 'HH_PRIMARY_RESUME_ID', 'synthetic-resume')
    monkeypatch.setattr(apply_orchestrator, 'create_hh_apply_trace', lambda *args, **kwargs: None)
    monkeypatch.setattr(apply_orchestrator.company_blacklist, 'is_blocked', lambda company: False)
    monkeypatch.setattr(apply_orchestrator, '_dispatch_apply', AsyncMock(return_value=dict(lower_result)))
    vacancy = {'id':'111', 'source':source, 'title':'QA', 'company':'Synthetic',
               'url':'https://hh.ru/vacancy/111'}
    result = asyncio.run(apply_orchestrator.dispatch_apply(vacancy, 'Approved synthetic cover'))
    event = next(event for event in read_json_records(config.ANALYTICS_EVENTS_FILE)
                 if event['event'] == 'application_result')
    assert event['outcome'] == 'uncertain'
    assert not result['ok'] and result['uncertain']


@pytest.mark.parametrize('source,lower_result', [
    ('hh', {'ok':False, 'uncertain':True, 'message':'Вакансия в архиве'}),
    ('hh', {'ok':False, 'uncertain':True, 'message':'Отклик не отправлен'}),
    ('hh', {'ok':False, 'uncertain':True, 'hh_unexpected_ui':True, 'hh_recovered':True, 'fingerprint':'a'*64}),
    ('hh', {'ok':False, 'uncertain':True, 'requires_questions':True, 'message':'Вопросы, пропускаем'}),
    ('habr', {'ok':False, 'uncertain':True, 'message':'unknown Habr receipt'}),
    ('geekjob', {'ok':False, 'submission_status':'uncertain', 'message':'unknown GeekJob receipt'}),
    ('geekjob', {'ok':False, 'submission_status':'acting', 'message':'unknown GeekJob receipt'}),
    ('geekjob', {'ok':False, 'submission_status':'preparing', 'message':'active GeekJob owner'}),
    ('hh', {'ok':True, 'uncertain':True, 'already_applied':True, 'message':'Conflicting unknown receipt'}),
])
def test_search_uncertainty_precedes_heuristics_and_failure(source, lower_result, isolated_a01, monkeypatch):
    configure_synthetic_search(monkeypatch, isolated_a01)
    monkeypatch.setattr(config, 'HH_ENABLED', source == 'hh')
    monkeypatch.setattr(config, 'HABR_ENABLED', source == 'habr')
    monkeypatch.setattr(config, 'GEEKJOB_ENABLED', source == 'geekjob')
    fake = SimpleNamespace(_page=None, start=AsyncMock(), stop=AsyncMock(), stop_browser=AsyncMock(),
                           is_logged_in=AsyncMock(return_value=True),
                           is_auto_apply_ready=AsyncMock(return_value=(True, '')))
    monkeypatch.setattr(agent, 'HabrCareerClient', lambda: fake)
    monkeypatch.setattr(agent, 'GeekJobClient', lambda: fake)
    vacancy = {'id':'4', 'source':source, 'source_label':source, 'title':'QA 4',
               'company':'Synthetic', 'url':'https://hh.ru/vacancy/4'}
    async def collect(*args, source_stats=None, **kwargs):
        return [dict(vacancy)]
    monkeypatch.setattr(agent.search_pipeline, 'collect_all', collect)
    dispatch = AsyncMock(return_value=dict(lower_result))
    monkeypatch.setattr(agent.apply_orchestrator, 'dispatch_apply', dispatch)
    result = asyncio.run(agent.do_search())
    assert seen._load()['4']['action'] == 'apply_uncertain'
    assert result['applied'] == result['funnel']['applied'] == result['funnel']['failed'] == 0
    assert result['source_stats'][source]['uncertain'] == 1
    decisions = [e['decision'] for e in read_json_records(config.ANALYTICS_EVENTS_FILE) if e['event']=='decision']
    assert decisions == ['apply_uncertain']
    assert agent.notify_needs_manual.await_count == 1
    text = str(agent.notify_needs_manual.await_args.kwargs['note']).casefold()
    assert 'провер' in text and 'не отправ' not in text and 'не уш' not in text
    dispatch.assert_awaited_once()


@pytest.mark.parametrize('lower_result', [
    {'ok':False, 'uncertain':True, 'message':'Вакансия закрыта, результат неизвестен'},
    {'ok':False, 'uncertain':True, 'message':'Отклик не отправлен'},
    {'ok':True, 'uncertain':True, 'already_applied':True, 'message':'Conflicting unknown result'},
])
def test_manual_uncertain_is_sticky_and_cannot_be_retried(lower_result, isolated_a01, monkeypatch):
    monkeypatch.setattr(manual_apply_queue, '_queue_path', lambda profile_name=None: isolated_a01/'queue.json')
    monkeypatch.setattr(config, 'HH_APPLICATION_MODE', 'auto')
    candidate = manual_apply_queue.create_candidate(
        {'id':'111', 'source':'hh', 'title':'QA', 'company':'Synthetic', 'url':'https://hh.ru/vacancy/111'},
        {'score':55, 'reason':'synthetic'}, details='Ordinary description')
    fake = SimpleNamespace(start=AsyncMock(), stop=AsyncMock(), is_logged_in=AsyncMock(return_value=True))
    monkeypatch.setattr(agent, 'HHClient', lambda: fake)
    monkeypatch.setattr(agent.hh_guard, 'can_auto_apply', lambda:(True,''))
    monkeypatch.setattr(agent, 'generate_cover_letter', AsyncMock(return_value='Approved synthetic cover'))
    dispatch = AsyncMock(return_value=dict(lower_result))
    monkeypatch.setattr(agent.apply_orchestrator, 'dispatch_apply', dispatch)
    monkeypatch.setattr(agent, 'notify_needs_manual', AsyncMock())
    result = asyncio.run(agent.do_manual_apply_token(candidate['token']))
    assert not result['ok'] and result['uncertain']
    assert 'не отправ' not in result['message'].casefold()
    assert manual_apply_queue.get_candidate(candidate['token'])['status'] == 'uncertain'
    assert seen._load()['111']['action'] == 'apply_uncertain'
    assert not manual_apply_queue.claim_candidate(candidate['token'])
    assert not asyncio.run(agent.do_manual_apply_token(candidate['token']))['ok']
    dispatch.assert_awaited_once()
    decisions = [e['decision'] for e in read_json_records(config.ANALYTICS_EVENTS_FILE) if e['event']=='decision']
    assert decisions == ['apply_uncertain']


def test_uncertainty_observability_has_separate_manual_outcome():
    assert analytics.decision_outcome('apply_uncertain') == ('manual', 'apply_uncertain')
    assert analytics._map_historical_action('apply_uncertain')[0] == 'apply_uncertain'
    summary = seen.stats_from_data({'111':{'action':'apply_uncertain'}})
    assert summary['manual'] == 1 and summary['applied'] == 0


def test_summary_never_counts_uncertain_as_definitely_not_sent(monkeypatch):
    send = AsyncMock()
    monkeypatch.setattr(notifier, 'send_message', send)
    monkeypatch.setattr(notifier, '_telegram_flag', lambda *args: True)
    asyncio.run(notifier.notify_summary(2, 0, 2, {'hh':{'manual':2, 'uncertain':1, 'keyword_pass':2}}))
    text = send.await_args.args[0]
    assert 'Не отправлено автоматически: 2' not in text
    assert 'неопредел' in text.casefold() or 'не подтвержд' in text.casefold()
    assert '1' in text


@pytest.mark.parametrize('downgrade', ['applied', 'apply_failed:unknown', 'skipped_archived', 'already_applied'])
def test_seen_uncertainty_cannot_be_downgraded(downgrade):
    vacancy = {'id':'111', 'source':'hh', 'title':'QA', 'company':'Synthetic'}
    seen.mark_seen('111', vacancy, 'apply_uncertain')
    with pytest.raises(RuntimeError, match='manual verification'):
        seen.mark_seen('111', vacancy, downgrade)
    assert seen._load()['111']['action'] == 'apply_uncertain'


def test_completed_run_displays_confirmed_count_and_uncertainty_separately():
    import telegram_bot_ui as ui
    vacancy = {'id':'111', 'source':'hh', 'title':'QA', 'company':'Synthetic'}
    with analytics.observe_search('a01-observability', 'search') as observation:
        observation.result = {'found':1, 'applied':0, 'source_stats':{'hh':{'manual':1, 'uncertain':1, 'new':1}}}
        analytics.register_candidates([vacancy])
        analytics.count_stage('apply_attempt', vacancy)
        analytics.record_decision(run_id=observation.run_id, vacancy=vacancy, decision='apply_uncertain')
        observation.ok = True
        record = observation.entry()
    assert record['funnel']['uncertain'] == 1 and record['funnel']['failed'] == 0
    assert record['funnel']['manual'] == 1
    text = ui.format_run_summary(record)
    assert '0 откликов' not in text.casefold()
    assert 'сверк' in text and 'подтверждено' in text
    summary = analytics.summarize(all_time=True)
    assert summary['uncertain'] == summary['manual'] == 1
    assert summary['by_source']['hh']['uncertain'] == 1
    assert summary['reason_breakdown'] == {'apply_uncertain':1}


def test_recovery_counts_and_fingerprints_survive_run_history_and_analytics():
    recovery = {'incidents':5, 'fingerprints':['a'*64, 'b'*64], 'distinct':2,
                'stopped':True, 'reason':'incident_limit'}
    with analytics.observe_search('hh-recovery-metadata', 'search') as observation:
        observation.result = {'found':0, 'applied':0, 'hh_recovery':recovery,
                              'source_stats':{'hh':{'stop_reason':'5 incidents, 2 fingerprints'}}}
        observation.ok = True
        record = observation.entry()
        incomplete = observation.entry(incomplete=True)
    assert record['hh_recovery'] == incomplete['hh_recovery'] == recovery
    analytics.record_search_finished(run_id='hh-recovery-metadata', mode='search', result=record)
    event = read_json_records(config.ANALYTICS_EVENTS_FILE)[-1]
    assert event['hh_recovery'] == recovery
    assert event['source_stats']['hh']['stop_reason'] == '5 incidents, 2 fingerprints'


@pytest.mark.parametrize('source', ['hh', 'future_source'])
def test_source_stop_reason_is_reported_even_without_candidates(source, monkeypatch):
    send = AsyncMock()
    monkeypatch.setattr(notifier, 'send_message', send)
    monkeypatch.setattr(notifier, '_telegram_flag', lambda *args: True)
    reason = '5 incidents; 2 fingerprints; <synthetic>'
    asyncio.run(notifier.notify_summary(0, 0, 0, {source:{'stop_reason':reason}}))
    text = send.await_args.args[0]
    assert '5 incidents; 2 fingerprints' in text
    assert '<synthetic>' not in text and '&lt;synthetic&gt;' in text


@pytest.mark.parametrize('failure,lower_result,expected_outcome', [
    ('capture', {'ok':False, 'uncertain':True}, 'uncertain'),
    ('finish', {'ok':False, 'uncertain':True}, 'uncertain'),
    ('finish', {'ok':True}, 'sent'),
    ('capture', {'ok':False}, 'failed'),
    ('finish', {'ok':False}, 'failed'),
])
def test_post_result_trace_error_cannot_replace_apply_receipt(failure, lower_result, expected_outcome, monkeypatch):
    trace = SimpleNamespace(last_stage='SUBMIT_ATTEMPTED', trace_id='synthetic-trace', trace_dir='/tmp/synthetic-trace',
        event=lambda *args, **kwargs: None,
        capture=AsyncMock(side_effect=OSError('synthetic trace disk error') if failure == 'capture' else None))
    def finish(**kwargs):
        if failure == 'finish':
            raise OSError('synthetic trace finalization error')
    trace.finish = finish
    monkeypatch.setattr(config, 'HH_PRIMARY_RESUME_ID', 'synthetic-resume')
    monkeypatch.setattr(apply_orchestrator.company_blacklist, 'is_blocked', lambda company: False)
    lower = AsyncMock(return_value=dict(lower_result))
    monkeypatch.setattr(apply_orchestrator, '_dispatch_apply', lower)
    client = SimpleNamespace(_page=object())
    vacancy = {'id':'111', 'source':'hh', 'title':'QA', 'company':'Synthetic', 'url':'https://hh.ru/vacancy/111'}
    result = asyncio.run(apply_orchestrator.dispatch_apply(vacancy, 'Approved synthetic cover', hh_client=client, trace=trace))
    assert result['ok'] == lower_result['ok']
    assert result.get('uncertain', False) == lower_result.get('uncertain', False)
    events = [e for e in read_json_records(config.ANALYTICS_EVENTS_FILE) if e['event']=='application_result']
    assert [e['outcome'] for e in events] == [expected_outcome]
    lower.assert_awaited_once()


@pytest.mark.parametrize('original_error', [OSError('synthetic native error'), apply_orchestrator.HHUnexpectedUI('apply', 'a'*64)])
def test_trace_error_does_not_mask_native_guard_stop(original_error, monkeypatch):
    trace = SimpleNamespace(last_stage='SUBMIT_ATTEMPTED', event=lambda *args, **kwargs:None,
                            capture=AsyncMock(side_effect=RuntimeError('synthetic capture error')))
    def finish(**kwargs):
        raise RuntimeError('synthetic trace finalization error')
    trace.finish = finish
    monkeypatch.setattr(config, 'HH_PRIMARY_RESUME_ID', 'synthetic-resume')
    monkeypatch.setattr(apply_orchestrator.company_blacklist, 'is_blocked', lambda company:False)
    monkeypatch.setattr(apply_orchestrator, '_dispatch_apply', AsyncMock(side_effect=original_error))
    vacancy = {'id':'111', 'source':'hh', 'title':'QA', 'company':'Synthetic', 'url':'https://hh.ru/vacancy/111'}
    with pytest.raises(type(original_error)) as exc:
        asyncio.run(apply_orchestrator.dispatch_apply(vacancy, 'Approved synthetic cover',
                      hh_client=SimpleNamespace(_page=object()), trace=trace))
    assert exc.value is original_error


def test_manual_proven_failure_does_not_become_uncertain_after_dispatch(isolated_a01, monkeypatch):
    monkeypatch.setattr(manual_apply_queue, '_queue_path', lambda profile_name=None: isolated_a01/'queue.json')
    monkeypatch.setattr(config, 'HH_APPLICATION_MODE', 'auto')
    candidate = manual_apply_queue.create_candidate(
        {'id':'111', 'source':'hh', 'title':'QA', 'company':'Synthetic', 'url':'https://hh.ru/vacancy/111'},
        {'score':55, 'reason':'synthetic'}, details='Ordinary description')
    fake = SimpleNamespace(start=AsyncMock(), stop=AsyncMock(), is_logged_in=AsyncMock(return_value=True))
    monkeypatch.setattr(agent, 'HHClient', lambda:fake)
    monkeypatch.setattr(agent.hh_guard, 'can_auto_apply', lambda:(True,''))
    monkeypatch.setattr(agent, 'generate_cover_letter', AsyncMock(return_value='Approved synthetic cover'))
    monkeypatch.setattr(agent, 'notify_needs_manual', AsyncMock())
    monkeypatch.setattr(agent.apply_orchestrator, 'dispatch_apply',
                        AsyncMock(return_value={'ok':False, 'uncertain':False, 'message':'Exact resume ID not verified'}))
    result = asyncio.run(agent.do_manual_apply_token(candidate['token']))
    assert not result['ok'] and not result.get('uncertain')
    assert manual_apply_queue.get_candidate(candidate['token'])['status'] == 'failed'
    assert '111' not in seen._load()


def test_uncertain_stops_hh_only_without_touching_next_hh_vacancy(isolated_a01, monkeypatch):
    configure_synthetic_search(monkeypatch, isolated_a01)
    monkeypatch.setattr(config, 'HABR_ENABLED', True)
    fake = SimpleNamespace(_page=None, stop=AsyncMock(), stop_browser=AsyncMock(),
                           is_logged_in=AsyncMock(return_value=True))
    monkeypatch.setattr(agent, 'HabrCareerClient', lambda:fake)
    monkeypatch.setattr(agent, 'task_complete', AsyncMock())
    vacancies = [{'id':str(i), 'source':'hh' if i in {4,5} else 'habr', 'title':f'QA {i}',
                  'company':'Synthetic', 'url':f'https://hh.ru/vacancy/{i}'} for i in [4,5,6]]
    async def collect(*args, **kwargs):
        return vacancies
    monkeypatch.setattr(agent.search_pipeline, 'collect_all', collect)
    calls=[]
    async def dispatch(v, *args, **kwargs):
        calls.append(v['id'])
        return {'ok':False, 'uncertain':True} if v['id']=='4' else {'ok':True}
    monkeypatch.setattr(agent.apply_orchestrator, 'dispatch_apply', dispatch)
    details_calls=[]
    async def details(v, *args, **kwargs):
        details_calls.append(v['id'])
        return 'Ordinary description'
    monkeypatch.setattr(agent.apply_orchestrator, 'fetch_vacancy_details', details)
    result=asyncio.run(agent.do_search())
    assert calls == details_calls == ['4','6']
    assert seen._load()['4']['action']=='apply_uncertain'
    assert '5' not in seen._load()
    assert result['applied']==1 and result['funnel']['failed']==0
    assert result['source_stats']['hh']['uncertain']==1
    assert result['source_stats']['habr']['applied']==1


@pytest.mark.parametrize('lower_result,failure,expected_outcome', [
    ({'ok':False, 'uncertain':True}, 'capture', 'uncertain'),
    ({'ok':True}, 'finish', 'sent'),
])
def test_trace_cancellation_propagates_without_erasing_receipt(lower_result, failure, expected_outcome, monkeypatch):
    trace = SimpleNamespace(last_stage='SUBMIT_ATTEMPTED', event=lambda *args, **kwargs:None,
                            capture=AsyncMock(side_effect=asyncio.CancelledError() if failure=='capture' else None))
    def finish(**kwargs):
        if failure=='finish':
            raise asyncio.CancelledError()
    trace.finish=finish
    monkeypatch.setattr(config, 'HH_PRIMARY_RESUME_ID', 'synthetic-resume')
    monkeypatch.setattr(apply_orchestrator.company_blacklist, 'is_blocked', lambda company:False)
    lower=AsyncMock(return_value=dict(lower_result))
    monkeypatch.setattr(apply_orchestrator, '_dispatch_apply', lower)
    vacancy={'id':'111', 'source':'hh', 'title':'QA', 'company':'Synthetic', 'url':'https://hh.ru/vacancy/111'}
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(apply_orchestrator.dispatch_apply(vacancy, 'Approved synthetic cover',
                      hh_client=SimpleNamespace(_page=object()), trace=trace))
    events=[e for e in read_json_records(config.ANALYTICS_EVENTS_FILE) if e['event']=='application_result']
    assert [e['outcome'] for e in events]==[expected_outcome]
    lower.assert_awaited_once()
