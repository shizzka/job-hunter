"""Independent adversarial HH isolation checks: only fake transports and temporary state."""
import asyncio
import socket
from types import SimpleNamespace
from unittest.mock import AsyncMock
import pytest
import agent
import analytics
import config
import seen
import notifier
from state_store.private_journal import read_json_records
import hh_resume_pipeline as pipeline
from hh.ui import HHUnexpectedUI
from tests.test_observability import configure_synthetic_search
ORIGINAL_COLLECT_ALL = agent.search_pipeline.collect_all
ORIGINAL_COLLECT_HH = agent.search_pipeline.collect_hh_vacancies

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    original = socket.socket.connect
    def reject(sock, address):
        if sock.family in (socket.AF_INET, socket.AF_INET6):
            raise AssertionError('INDEPENDENT D: external network forbidden')
        return original(sock, address)
    monkeypatch.setattr(socket.socket, 'connect', reject)
    monkeypatch.setattr(socket.socket, 'connect_ex', reject)

@pytest.fixture
def synthetic(tmp_path, monkeypatch):
    for key, name in {
        'JOB_HUNTER_HOME':'', 'RUN_HISTORY_FILE':'runs.jsonl', 'RUNTIME_STATUS_FILE':'runtime.json',
        'SEEN_VACANCIES_FILE':'seen.json', 'HH_STATE_DIR':'hh', 'HH_GUARD_STATE_FILE':'guard.json',
        'ANALYTICS_EVENTS_FILE':'events.jsonl', 'ANALYTICS_STATE_FILE':'analytics.json',
        'HH_RESUME_PIPELINE_FILE':'pipeline.json', 'HH_COOKIES_FILE':'synthetic-cookies.json',
        'HABR_COOKIES_FILE':'synthetic-habr-cookies.json',
    }.items():
        monkeypatch.setattr(config,key,str(tmp_path/name))
    configure_synthetic_search(monkeypatch,tmp_path)
    monkeypatch.setattr(agent.search_pipeline.filters,'check_vacancy',lambda v:None)
    monkeypatch.setattr(agent,'evaluate_vacancy',AsyncMock(return_value={
        'score':90,'reason':'Synthetic','should_apply':True,'red_flags':[]}))
    fake_habr=SimpleNamespace(start=AsyncMock(),stop=AsyncMock(),stop_browser=AsyncMock(),
                             is_logged_in=AsyncMock(return_value=True))
    monkeypatch.setattr(config,'HABR_ENABLED',True)
    monkeypatch.setattr(agent,'HabrCareerClient',lambda:fake_habr)
    return tmp_path

def vac(vid,source='hh'):
    return {'id':str(vid),'source':source,'title':'Manual QA','company':'Synthetic '+str(vid),
            'url':'https://example.invalid/vacancy/'+str(vid)}

def incident(fp='a', recovered=True):
    exc=HHUnexpectedUI('apply',fp*64)
    if recovered is not None:
        exc.hh_recovered=recovered
    return exc

def use_sequence(monkeypatch, sequence, *, start_id=4):
    vacancies=[vac(i+start_id) for i in range(len(sequence))]+[vac('habr:other','habr')]
    async def collect(*a,**k):return [dict(v) for v in vacancies]
    monkeypatch.setattr(agent.search_pipeline,'collect_all',collect)
    visited=[]
    async def apply(v,*a,**k):
        visited.append((v['source'],v['id']))
        item=sequence[int(v['id'])-start_id] if v['source']=='hh' else None
        if item is not None:raise item
        return {'ok':True}
    monkeypatch.setattr(agent.apply_orchestrator,'dispatch_apply',apply)
    return visited

@pytest.mark.parametrize('sequence,expected_incidents,expected_visited,distinct,stopped',[
    ([incident('a'), None, incident('a'), None, incident('a'), None, incident('a'), None],4,8,1,False),
    ([incident('a'), None, incident('a'), None, incident('a'), None, incident('a'), None,incident('a'),None],5,9,1,True),
    ([incident('a'),None,incident('b'),None,incident('c'),None],3,5,3,True),
    ([incident('a'),incident('b'),incident('a'),incident('b'),None],4,5,2,False),
    ([incident('a'),incident('b'),incident('a'),incident('b'),incident('a'),None],5,5,2,True),
])
def test_breaker_ignores_intervening_success_and_counts_distinct_once(synthetic,monkeypatch,
        sequence,expected_incidents,expected_visited,distinct,stopped):
    visited=use_sequence(monkeypatch,sequence)
    result=asyncio.run(agent.do_search())
    hh=result['hh_recovery']
    assert len([v for v in visited if v[0]=='hh'])==expected_visited
    assert hh['incidents']==expected_incidents
    assert hh['distinct']==distinct
    assert len(hh['fingerprints'])==distinct
    assert hh['stopped'] is stopped
    assert ('habr','habr:other') in visited
    assert result['funnel']['failed']==0
    assert 'apply_failed' not in result['reason_breakdown']
    for index in range(expected_visited,len(sequence)):
        assert str(index+4) not in seen.all_entries()

@pytest.mark.parametrize('marker',[False,None,'true',1])
def test_unproven_recovery_immediately_stops_only_hh(synthetic,monkeypatch,marker):
    visited=use_sequence(monkeypatch,[incident('a',marker),None])
    result=asyncio.run(agent.do_search())
    assert visited==[('hh','4'),('habr','habr:other')]
    assert result['hh_recovery']['stopped']
    assert result['hh_recovery']['incidents']==0
    assert '5' not in seen.all_entries()
    assert result['funnel']['failed']==0

@pytest.mark.parametrize('recovered',[True,False])
def test_stopped_hh_never_runs_chat_piggyback(synthetic,monkeypatch,recovered):
    import hh_chat_responder
    monkeypatch.setattr(config,'HH_CHAT_RESPONDER_ENABLED',True)
    chat=AsyncMock(return_value={})
    monkeypatch.setattr(hh_chat_responder,'process_all',chat)
    sequence=[incident('a',recovered)]*5+[None] if recovered else [incident('a',False),None]
    use_sequence(monkeypatch,sequence)
    result=asyncio.run(agent.do_search())
    assert result['hh_recovery']['stopped']
    chat.assert_not_awaited()


def test_new_run_does_not_inherit_breaker_state(synthetic,monkeypatch):
    use_sequence(monkeypatch,[incident('a')]*5+[None])
    first=asyncio.run(agent.do_search())
    assert first['hh_recovery']['stopped']
    visited=use_sequence(monkeypatch,[None],start_id=11)
    second=asyncio.run(agent.do_search())
    assert second['hh_recovery']['incidents']==0
    assert second['hh_recovery']['distinct']==0
    assert second['hh_recovery']['stopped'] is False
    assert visited==[('hh','11'),('habr','habr:other')]

@pytest.mark.parametrize('reason',['apply_uncertain','guard_stop','manual_hh_guard_stop'])
@pytest.mark.parametrize('status',['Приглашение','Отказ','Просмотрен'])
def test_resume_terminal_safety_reason_is_sticky_across_status_sync(synthetic,monkeypatch,reason,status):
    from datetime import datetime,timedelta
    monkeypatch.setattr(config,'HH_RESUME_PIPELINE_ENABLED',True)
    monkeypatch.setattr(config,'HH_PRIMARY_RESUME_ID','one')
    monkeypatch.setattr(config,'HH_PRIMARY_RESUME_TITLE','Manual QA')
    monkeypatch.setattr(config,'HH_SECONDARY_RESUME_ID','two')
    monkeypatch.setattr(config,'HH_SECONDARY_RESUME_TITLE','Manual QA second')
    monkeypatch.setattr(config,'HH_TERTIARY_RESUME_ID','')
    monkeypatch.setattr(config,'HH_TERTIARY_RESUME_TITLE','')
    monkeypatch.setattr(config,'HH_RESUME_RETRY_ON_SILENCE',True)
    monkeypatch.setattr(pipeline,'enabled',lambda:True)
    pipeline.record_successful_apply(vac('4'),{'name':'normal','id':'one'})
    pipeline.mark_terminal('4',reason)
    pipeline.sync_negotiation_statuses([{**vac('4'),'status':status}])
    assert pipeline.all_entries()['4']['completed_reason']==reason
    monkeypatch.setattr(pipeline,'_now',lambda:datetime.now()+timedelta(days=10))
    assert pipeline.get_retry_candidates()==[]

@pytest.mark.parametrize('seen_action',['apply_uncertain','manual_hh_guard_stop'])
def test_seen_safety_marker_blocks_legacy_retry_in_automatic_search(synthetic,monkeypatch,seen_action):
    from datetime import datetime,timedelta
    monkeypatch.setattr(config,'HH_RESUME_PIPELINE_ENABLED',True)
    monkeypatch.setattr(config,'HH_PRIMARY_RESUME_ID','one')
    monkeypatch.setattr(config,'HH_PRIMARY_RESUME_TITLE','Manual QA')
    monkeypatch.setattr(config,'HH_SECONDARY_RESUME_ID','two')
    monkeypatch.setattr(config,'HH_SECONDARY_RESUME_TITLE','Manual QA second')
    monkeypatch.setattr(config,'HH_TERTIARY_RESUME_ID','')
    monkeypatch.setattr(config,'HH_TERTIARY_RESUME_TITLE','')
    monkeypatch.setattr(config,'HH_RESUME_RETRY_MAX_PER_COMPANY',0)
    monkeypatch.setattr(pipeline,'enabled',lambda:True)
    pipeline.record_successful_apply(vac('4'),{'name':'normal','id':'one'})
    pipeline.sync_negotiation_statuses([{**vac('4'),'status':'Отказ'}])
    seen.mark_seen('4',vac('4'),seen_action)
    monkeypatch.setattr(pipeline,'_now',lambda:datetime.now()+timedelta(days=10))
    fake_hh=SimpleNamespace(start=AsyncMock(),stop=AsyncMock(),_page=None,
            is_logged_in=AsyncMock(return_value=True), get_negotiation_statuses=AsyncMock(return_value=[]),
            get_resume_ids=AsyncMock(return_value=[]))
    monkeypatch.setattr(agent,'HHClient',lambda:fake_hh)
    async def collect(*a,hh_retry_vacancies=None,**k):
        return list(hh_retry_vacancies or [])+[vac('habr:other','habr')]
    monkeypatch.setattr(agent.search_pipeline,'collect_all',collect)
    visited=[]
    async def apply(v,*a,**k):visited.append((v['source'],v['id']));return {'ok':True}
    monkeypatch.setattr(agent.apply_orchestrator,'dispatch_apply',apply)
    result=asyncio.run(agent.do_search())
    assert visited==[('habr','habr:other')]
    assert seen.all_entries()['4']['action']==seen_action

@pytest.mark.parametrize('where',['startup','collection'])
def test_ui_failure_before_vacancy_stops_hh_and_continues_other_sources(synthetic,monkeypatch,where):
    import search_pipeline
    if where=='startup':
        fake_hh=SimpleNamespace(start=AsyncMock(),stop=AsyncMock(),_page=None,
                is_logged_in=AsyncMock(side_effect=incident('a',True)),
                get_negotiation_statuses=AsyncMock(return_value=[]))
        monkeypatch.setattr(agent,'HHClient',lambda:fake_hh)
    else:
        async def fail_hh(*a,**k):raise incident('a',True)
        monkeypatch.setattr(search_pipeline,'collect_hh_vacancies',fail_hh)
    monkeypatch.setattr(search_pipeline,'collect_all',ORIGINAL_COLLECT_ALL)
    async def hh_collector(*a,**k):return [vac('4')]
    if where=='startup':monkeypatch.setattr(search_pipeline,'collect_hh_vacancies',hh_collector)
    for name in ['collect_superjob_vacancies','collect_geekjob_vacancies']:
        monkeypatch.setattr(search_pipeline,name,AsyncMock(return_value=[]))
    monkeypatch.setattr(search_pipeline,'collect_habr_vacancies',AsyncMock(return_value=[vac('habr:other','habr')]))
    visited=[]
    async def apply(v,*a,**k):visited.append((v['source'],v['id']));return {'ok':True}
    monkeypatch.setattr(agent.apply_orchestrator,'dispatch_apply',apply)
    result=asyncio.run(agent.do_search())
    assert visited==[('habr','habr:other')]
    assert result['hh_recovery']['stopped']
    assert result['hh_recovery']['incidents']==0
    assert '4' not in seen.all_entries()

@pytest.mark.parametrize('apply_result',[
    {'ok':False,'anti_bot_kind':'captcha','message':'Synthetic captcha'},
    {'ok':False,'anti_bot_kind':'ddos_guard','message':'Synthetic anti-bot'},
])
def test_antibot_result_stops_all_further_hh_browser_work(synthetic,monkeypatch,apply_result):
    vacancies=[vac('4'),vac('5'),vac('habr:other','habr')]
    async def collect(*a,**k):return vacancies
    monkeypatch.setattr(agent.search_pipeline,'collect_all',collect)
    visited=[]
    async def details(v,*a,**k):visited.append(('details',v['source'],v['id']));return 'Ordinary'
    async def apply(v,*a,**k):
        visited.append(('apply',v['source'],v['id']))
        return dict(apply_result) if v['source']=='hh' else {'ok':True}
    monkeypatch.setattr(agent.apply_orchestrator,'fetch_vacancy_details',details)
    monkeypatch.setattr(agent.apply_orchestrator,'dispatch_apply',apply)
    monkeypatch.setattr(agent.hh_guard,'record_antibot',lambda **k:{})
    monkeypatch.setattr(agent.hh_guard,'format_block_note',lambda s:'Synthetic anti-bot: stop HH')
    result=asyncio.run(agent.do_search())
    assert ('details','hh','5') not in visited
    assert ('apply','hh','5') not in visited
    assert ('apply','habr','habr:other') in visited
    assert result['hh_recovery']['stopped']
    assert result['hh_recovery']['incidents']==0
    assert '5' not in seen.all_entries()
    assert result['funnel']['failed']==0


def test_lost_auth_prevents_deferred_hh_vacancy_resurrection(synthetic,monkeypatch):
    from state_store.matcher_deferred import MatcherDeferredQueue
    queue=MatcherDeferredQueue(synthetic,cooldown_seconds=1,clock=lambda:0)
    queue.defer(vac('4'),'Cached description','transient_matcher_error')
    fake_hh=SimpleNamespace(start=AsyncMock(),stop=AsyncMock(),_page=None,
            is_logged_in=AsyncMock(return_value=False),get_negotiation_statuses=AsyncMock(return_value=[]))
    monkeypatch.setattr(agent,'HHClient',lambda:fake_hh)
    async def collect(*a,**k):return [vac('habr:other','habr')]
    monkeypatch.setattr(agent.search_pipeline,'collect_all',collect)
    visited=[]
    async def apply(v,*a,**k):visited.append((v['source'],v['id']));return {'ok':True}
    monkeypatch.setattr(agent.apply_orchestrator,'dispatch_apply',apply)
    result=asyncio.run(agent.do_search())
    assert visited==[('habr','habr:other')]
    assert result['hh_recovery']['stopped']
    assert '4' not in seen.all_entries()

@pytest.mark.parametrize('seen_action',['apply_uncertain','manual_hh_guard_stop'])
def test_collector_repeated_blocked_vacancy_never_touches_browser_or_overwrites_seen(synthetic,monkeypatch,seen_action):
    seen.mark_seen('4',vac('4'),seen_action)
    visited=use_sequence(monkeypatch,[None])
    details=AsyncMock(return_value='Ordinary')
    monkeypatch.setattr(agent.apply_orchestrator,'fetch_vacancy_details',details)
    result=asyncio.run(agent.do_search())
    assert visited==[('habr','habr:other')]
    assert [call.args[0]['source'] for call in details.await_args_list]==['habr']
    assert seen.all_entries()['4']['action']==seen_action
    assert result['funnel']['failed']==0
    assert not result['hh_recovery']['stopped']

@pytest.mark.parametrize('sequence',[[incident('a')]*5+[None],[incident('a'),incident('b'),incident('c'),None]])
def test_breaker_reason_counts_and_fingerprints_reach_history_analytics_and_telegram(synthetic,monkeypatch,sequence):
    use_sequence(monkeypatch,sequence)
    result=asyncio.run(agent.do_search())
    hh=result['hh_recovery']
    history=read_json_records(config.RUN_HISTORY_FILE)[-1]
    assert history['hh_recovery']==hh
    events=read_json_records(config.ANALYTICS_EVENTS_FILE)
    isolates=[event for event in events if event['event']=='hh_ui_isolation']
    assert isolates[-1]['stopped'] is True
    assert isolates[-1]['reason']==hh['reason']
    assert isolates[-1]['incidents']==hh['incidents']
    assert isolates[-1]['distinct']==hh['distinct']
    assert isolates[-1]['fingerprints']==hh['fingerprints']
    note=agent.notify_needs_manual.await_args_list[hh['incidents']-1].kwargs['note']
    assert hh['reason'] in note
    assert str(hh['incidents']) in note and str(hh['distinct']) in note
    summary=agent.notify_summary.await_args.args[3]
    assert summary['hh']['stop_reason']==hh['reason']
    assert 'остановлен' in notifier._format_source_stats(summary)
    assert hh['reason'] in notifier._format_source_stats(summary)
    assert result['funnel']['failed']==0

@pytest.mark.parametrize('where',['startup','collection'])
def test_empty_run_preserves_ui_stop_and_notifies_telegram(synthetic,monkeypatch,where):
    monkeypatch.setattr(config,'HABR_ENABLED',False)
    monkeypatch.setattr(agent.search_pipeline,'collect_all',ORIGINAL_COLLECT_ALL)
    for name in ['collect_superjob_vacancies','collect_habr_vacancies','collect_geekjob_vacancies']:
        monkeypatch.setattr(agent.search_pipeline,name,AsyncMock(return_value=[]))
    if where=='startup':
        fake_hh=SimpleNamespace(start=AsyncMock(),stop=AsyncMock(),_page=None,
            is_logged_in=AsyncMock(side_effect=incident('a',True)),
            get_negotiation_statuses=AsyncMock(return_value=[]))
        monkeypatch.setattr(agent,'HHClient',lambda:fake_hh)
        monkeypatch.setattr(agent.search_pipeline,'collect_hh_vacancies',AsyncMock(return_value=[]))
    else:
        monkeypatch.setattr(agent.search_pipeline,'collect_hh_vacancies',AsyncMock(side_effect=incident('a',True)))
    result=asyncio.run(agent.do_search())
    assert result['hh_recovery']['stopped']
    assert result['hh_recovery']['reason']
    assert read_json_records(config.RUN_HISTORY_FILE)[-1]['hh_recovery']['stopped']
    # Existing notification mechanism is mocked; delivery itself remains offline.
    assert agent.notify_summary.await_count or agent.notify_needs_manual.await_count
    assert not seen.all_entries()

@pytest.mark.parametrize('case',['auth_lost','captcha'])
def test_collection_hard_stop_prevents_cached_deferred_hh_from_running(synthetic,monkeypatch,case):
    from state_store.matcher_deferred import MatcherDeferredQueue
    queue=MatcherDeferredQueue(synthetic,cooldown_seconds=1,clock=lambda:0)
    queue.defer(vac('4'),'Cached description','transient_matcher_error')
    fake_hh=SimpleNamespace(start=AsyncMock(),stop=AsyncMock(),_page=None,
        is_logged_in=AsyncMock(side_effect=[True,False] if case=='auth_lost' else [True,True]),
        get_negotiation_statuses=AsyncMock(return_value=[]),
        search_vacancies=AsyncMock(return_value=[vac('4')]),
        consume_antibot_signal=lambda:{'kind':'captcha','message':'Synthetic challenge','stage':'search'})
    monkeypatch.setattr(agent,'HHClient',lambda:fake_hh)
    monkeypatch.setattr(agent.hh_guard,'can_collect',lambda:(True,''))
    monkeypatch.setattr(agent.hh_guard,'record_antibot',lambda **k:{})
    monkeypatch.setattr(agent.hh_guard,'format_block_note',lambda s:'Synthetic captcha: stop HH')
    monkeypatch.setattr(agent.search_pipeline,'_notify_hh_auth_required_once',AsyncMock())
    monkeypatch.setattr(agent.search_pipeline,'office_log',AsyncMock())
    monkeypatch.setattr(agent.search_pipeline,'collect_all',ORIGINAL_COLLECT_ALL)
    monkeypatch.setattr(agent.search_pipeline,'collect_hh_vacancies',ORIGINAL_COLLECT_HH)
    for name in ['collect_superjob_vacancies','collect_geekjob_vacancies']:
        monkeypatch.setattr(agent.search_pipeline,name,AsyncMock(return_value=[]))
    monkeypatch.setattr(agent.search_pipeline,'collect_habr_vacancies',AsyncMock(return_value=[vac('habr:other','habr')]))
    visited=[]
    async def details(v,*a,**k):visited.append(('details',v['source'],v['id']));return 'Ordinary'
    async def apply(v,*a,**k):visited.append(('apply',v['source'],v['id']));return {'ok':True}
    monkeypatch.setattr(agent.apply_orchestrator,'fetch_vacancy_details',details)
    monkeypatch.setattr(agent.apply_orchestrator,'dispatch_apply',apply)
    result=asyncio.run(agent.do_search())
    assert ('details','hh','4') not in visited
    assert ('apply','hh','4') not in visited
    assert ('apply','habr','habr:other') in visited
    assert result['hh_recovery']['stopped']
    assert '4' not in seen.all_entries()


def test_known_uncertain_ui_does_not_attempt_recovery(synthetic,monkeypatch):
    exc=incident('a',None)
    exc.hh_uncertain=True
    recovery=AsyncMock(return_value=True)
    fake=SimpleNamespace(start=AsyncMock(),stop=AsyncMock(),_page=None,
        is_logged_in=AsyncMock(return_value=True),get_negotiation_statuses=AsyncMock(return_value=[]),
        recover_unexpected_ui=recovery)
    monkeypatch.setattr(agent,'HHClient',lambda:fake)
    visited=use_sequence(monkeypatch,[exc,None])
    result=asyncio.run(agent.do_search())
    assert result['hh_recovery']['stopped']
    assert result['hh_recovery']['incidents']==0
    assert seen.all_entries()['4']['action']=='apply_uncertain'
    assert visited==[('hh','4'),('habr','habr:other')]
    recovery.assert_not_awaited()
