"""Owner outcome and accounting use typed delivery, preserving safety signals."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import agent
import analytics
import apply_orchestrator
import config
from hh import delivery
from outcome import application_outcome, apply_result_is_uncertain
from state_store.private_journal import read_json_records
from tests.test_hh_server_delivery import HASH, dirty, html, run_check, setup


@pytest.mark.parametrize('vid,topic', [('130000002',5600000002), ('130000003',5600000003)])
def test_live_cases_owner_sent_and_durable_uncertain(tmp_path, vid, topic):
    client, attempt, _, _ = setup(tmp_path, vid, topic)
    dirty(client, attempt)
    assert run_check(client, attempt).status=='confirmed'
    attempt.repository.transition(attempt.url, attempt.owner, 'uncertain')
    result = apply_orchestrator.apply_result_with_current_uncertainty(
        {'source':'hh','url':attempt.url}, client, {'ok':False,'uncertain':True,'hh_hard_stop':True},
        owned_attempt=attempt)
    assert result['server_delivery']['topic_id']==topic and application_outcome(result)=='sent'
    assert result['uncertain'] and result['hh_hard_stop'] and not result['ok']
    assert client._hh_recovery_stop_reason=='possible_external_action'
    assert attempt.repository.get(attempt.url)['status']=='uncertain'
    assert not attempt.repository.claim(attempt.url, '', '')


@pytest.mark.parametrize('kind', ['confirmed', 'not_seen', 'ambiguous', 'mismatch', 'unknown', 'forged'])
def test_owner_projection_analytics_notification_and_safety(tmp_path, monkeypatch, kind):
    client, attempt, _, isolated = setup(tmp_path)
    monkeypatch.setattr(delivery.asyncio, 'sleep', AsyncMock())
    dirty(client, attempt)
    if kind == 'not_seen': isolated.get.return_value.text.return_value = html('130000002')
    if kind == 'ambiguous': isolated.get.return_value.text.return_value = html('130000002', [1,2])
    if kind == 'mismatch': isolated.get.return_value.text.return_value = html('130000002', [1], resume_hash='wrong')
    if kind == 'unknown': del attempt._hh_delivery_before
    if kind != 'forged': assert run_check(client, attempt).status == kind
    attempt.repository.transition(attempt.url, attempt.owner, 'uncertain')
    vacancy = {'source':'hh','id':'130000002','url':attempt.url}
    raw = {'ok':False,'uncertain':True,'hh_hard_stop':True,'server_delivery':{'status':'confirmed'}}
    result = apply_orchestrator.apply_result_with_current_uncertainty(vacancy, client, raw, owned_attempt=attempt)
    assert application_outcome(result) == ('sent' if kind=='confirmed' else 'uncertain')
    assert apply_result_is_uncertain(result) and not result['ok'] and result['hh_hard_stop']
    assert attempt.repository.get(attempt.url)['status'] == 'uncertain'
    assert not attempt.repository.claim(attempt.url, '', '')
    monkeypatch.setattr(agent, 'notify_application', AsyncMock())
    monkeypatch.setattr(agent, 'notify_needs_manual', AsyncMock())
    asyncio.run(agent._notify_hh_delivery_or_uncertain(vacancy, 90, 'reason', result, result['message']))
    if kind=='confirmed':
        assert result['cover_letter_status']=='submitted_with_application'
        assert 'подтверждено сервером hh' in result['message']
        agent.notify_application.assert_awaited_once()
        agent.notify_needs_manual.assert_not_awaited()
        assert application_outcome({**result}) == 'uncertain'
    else:
        agent.notify_needs_manual.assert_awaited_once()
        if kind != 'forged': assert kind in result['message']
    if kind=='not_seen': assert 'не доказывает недоставку' in result['message']
    if kind=='forged': assert 'server_delivery' not in result
    with analytics.observe_search('delivery-projection', 'search') as observed:
        returned = asyncio.run(analytics.tracked_call('apply', observed.run_id, vacancy, AsyncMock(return_value=result)))
        assert returned is result
        assert observed.counters['hh']['applied'] == int(kind=='confirmed')
    monkeypatch.setattr(config, 'ANALYTICS_ENABLED', True)
    monkeypatch.setattr(config, 'ANALYTICS_EVENTS_FILE', str(tmp_path / 'events.jsonl'))
    result['question_answers'] = [{'question': 'Synthetic question', 'answer': 'Synthetic answer'}]
    agent._record_hh_questionnaire_analytics(run_id='delivery-projection', vacancy=vacancy,
                                           apply_result=result)
    event, = [row for row in read_json_records(config.ANALYTICS_EVENTS_FILE)
              if row.get('event') == 'questionnaire']
    assert event['success'] is (kind == 'confirmed')


@pytest.mark.parametrize('phase', ['before_get', 'after_get'])
def test_real_browser_dirty_receipt_stays_stopped_but_counts_sent(tmp_path, monkeypatch, phase):
    import tests.test_e2e_hh_apply_happy_path as gate_module
    import hh.apply as hh_apply
    from hh_client import HHClient
    from tests.test_hh_post_submit_verification import APPLIED_HTML, VACANCY_URL
    dispatch = gate_module.REAL_DISPATCH
    async def with_questionnaire(*args, **kwargs):
        result = await dispatch(*args, **kwargs)
        result['question_answers'] = [{'question': 'Synthetic question', 'answer': 'Synthetic answer'}]
        return result
    monkeypatch.setattr(gate_module, 'REAL_DISPATCH', with_questionnaire)
    monkeypatch.setattr(gate_module, 'VACANCY_HTML', gate_module.VACANCY_HTML + html('2'))
    after_html = APPLIED_HTML + html('2', [5600000002])
    original_init = HHClient.__init__
    isolated_get = AsyncMock(return_value=SimpleNamespace(status=200, url=VACANCY_URL,
        headers={'content-type':'text/html'}, text=AsyncMock(return_value=after_html), dispose=AsyncMock()))
    isolated = SimpleNamespace(get=isolated_get, dispose=AsyncMock())
    factory = AsyncMock(return_value=isolated)
    def initialize(self, *args, **kwargs):
        original_init(self, *args, **kwargs)
        self._pw = SimpleNamespace(request=SimpleNamespace(new_context=factory))
    monkeypatch.setattr(HHClient, '__init__', initialize)
    if phase=='before_get':
        original_wait = hh_apply.wait_popup_receipt_navigation
        async def dirty_navigation(session, boundary):
            await original_wait(session, boundary)
            attempt = session._external_attempt
            # Browser request plus independent unknown callbacks, as in live traces.
            await attempt.page.evaluate("""() => fetch('/other', {method:'POST',
                headers:{'content-type':'application/json'},body:'{malformed'}).catch(()=>{})""")
            for _ in range(30): attempt.page._hh_action_monitor.mark_unknown(reason='unclassified_event')
            raise RuntimeError('Synthetic stale popup')
        monkeypatch.setattr(hh_apply, 'wait_popup_receipt_navigation', dirty_navigation)
    effect = ("await fetch('/other',{method:'POST',headers:{'content-type':'application/json'},body:'{malformed'}).catch(()=>{});"
              if phase=='after_get' else '')
    gate = asyncio.run(gate_module.run_hh_gate(tmp_path, monkeypatch, noise=False,
        continue_after_apply=True, confirmation_html=after_html, post_submit_effect=effect))
    attempt, = gate.native_attempts
    result = gate.dispatch_results[0]
    assert result['server_delivery']['status']=='confirmed'
    assert application_outcome(result)=='sent' and result['uncertain'] and not result['ok']
    assert result['cover_letter_status']=='submitted_with_application'
    assert gate.result['applied']==1 and gate.result['funnel']['applied']==1
    assert gate.result['funnel']['uncertain']==0
    assert gate.result['hh_recovery']['stopped']
    assert gate.result['application_outcome']=='sent'
    assert gate.entries['2']['action']=='apply_uncertain'  # safety ledger remains sticky
    assert not [e for e in gate.decisions if e['decision']=='apply_uncertain']
    assert any(e['decision']=='applied_auto' for e in gate.decisions)
    assert attempt.context._hh_action_watch.unknown and attempt.page._hh_action_monitor.unknown
    assert attempt.repository.get(VACANCY_URL)['status']=='uncertain'
    assert not attempt.repository.claim(VACANCY_URL, '', '')
    assert gate.evidence['dispatched']==['2'] and gate.evidence['hard_stops']>=1
    assert not gate.notices
    if phase=='before_get': isolated_get.assert_awaited_once()
    else: isolated_get.assert_not_awaited()
    events = read_json_records(config.ANALYTICS_EVENTS_FILE)
    assert [e['outcome'] for e in events if e.get('event')=='application_result']==['sent']
    questionnaire, = [e for e in events if e.get('event') == 'questionnaire']
    assert questionnaire['success'] is True
    assert questionnaire['vacancy_id'] == '2'


def test_manual_queue_remains_uncertain_while_owner_is_told_sent(tmp_path, monkeypatch):
    import manual_apply_queue
    import seen
    for key, name in {'JOB_HUNTER_HOME':'','SEEN_VACANCIES_FILE':'seen.json',
        'HH_RESUME_PIPELINE_FILE':'pipeline.json','ANALYTICS_EVENTS_FILE':'events.jsonl',
        'ANALYTICS_STATE_FILE':'analytics.json','HH_STATE_DIR':'hh'}.items():
        monkeypatch.setattr(config, key, str(tmp_path/name))
    monkeypatch.setattr(config, 'ANALYTICS_ENABLED', True)
    monkeypatch.setattr(config, 'HH_APPLICATION_MODE', 'auto')
    monkeypatch.setattr(manual_apply_queue, '_queue_path', lambda profile_name=None: tmp_path/'queue.json')
    client, attempt, _, _ = setup(tmp_path)
    dirty(client, attempt)
    assert run_check(client, attempt).status=='confirmed'
    attempt.repository.transition(attempt.url, attempt.owner, 'uncertain')
    client.start, client.stop = AsyncMock(), AsyncMock()
    client.is_logged_in = AsyncMock(return_value=True)
    monkeypatch.setattr(agent, 'HHClient', lambda: client)
    monkeypatch.setattr(agent.hh_guard, 'can_auto_apply', lambda:(True,''))
    monkeypatch.setattr(agent, 'generate_cover_letter', AsyncMock(return_value='PRIVATE_LETTER'))
    monkeypatch.setattr(agent, 'notify_application', AsyncMock())
    monkeypatch.setattr(agent, 'notify_needs_manual', AsyncMock())
    vacancy = {'id':'130000002','source':'hh','url':attempt.url,'title':'QA','company':'Synthetic'}
    candidate = manual_apply_queue.create_candidate(vacancy, {'score':55,'reason':'synthetic'}, details='Synthetic')
    raw = delivery.result_with_delivery({'ok':False,'uncertain':True,'hh_hard_stop':True,
        'question_answers': [{'question': 'Synthetic question', 'answer': 'Synthetic answer'}]}, attempt)
    dispatch = AsyncMock(return_value=raw)
    monkeypatch.setattr(apply_orchestrator, 'dispatch_apply', dispatch)
    result = asyncio.run(agent.do_manual_apply_token(candidate['token']))
    assert application_outcome(result)=='sent' and result['uncertain'] and not result['ok']
    assert 'подтверждено сервером hh' in result['message']
    assert manual_apply_queue.get_candidate(candidate['token'])['status']=='uncertain'
    assert not manual_apply_queue.claim_candidate(candidate['token'])
    assert seen.all_entries()['130000002']['action']=='apply_uncertain'
    agent.notify_application.assert_awaited_once()
    agent.notify_needs_manual.assert_not_awaited()
    decisions = [e['decision'] for e in read_json_records(config.ANALYTICS_EVENTS_FILE) if e.get('event')=='decision']
    assert decisions==['applied_auto']
    questionnaire, = [e for e in read_json_records(config.ANALYTICS_EVENTS_FILE)
                      if e.get('event') == 'questionnaire']
    assert questionnaire['success'] is True
    assert questionnaire['vacancy_id'] == '130000002'


def test_delivery_presentation_exception_preserves_old_outcome(tmp_path, monkeypatch):
    client, attempt, _, _ = setup(tmp_path)
    dirty(client, attempt)
    assert run_check(client, attempt).status=='confirmed'
    def fail(*args): raise RuntimeError('PRIVATE_ERROR')
    monkeypatch.setattr(apply_orchestrator, 'application_result_for_owner', fail)
    vacancy = {'source':'hh','url':attempt.url}
    result = apply_orchestrator.apply_result_with_current_uncertainty(vacancy, client,
        {'ok':False,'uncertain':True,'hh_hard_stop':True}, owned_attempt=attempt)
    assert result['server_delivery']['status']=='unknown'
    assert application_outcome(result)=='uncertain'
    assert result['uncertain'] and result['hh_hard_stop'] and not result['ok']
    assert attempt.context._hh_action_watch.unknown and client._hh_recovery_stop_reason=='possible_external_action'


def test_telegram_manual_command_separates_delivery_from_exit_status():
    from telegram_bot_ui import format_command_result
    text = format_command_result('manual AI apply', {'ok':False,
        'stdout':'Отклик отправлен, подтверждено сервером hh.','stderr':''})
    assert 'отправлено, подтверждено сервером hh' in text
    assert 'Выполнение команды остановлено' in text
    assert 'нужна ручная сверка' not in text
    forged = format_command_result('manual AI apply', {'ok':False,'stdout':'','stderr':'',
        'server_delivery':{'status':'confirmed'}})
    assert 'подтверждено сервером hh' not in forged
