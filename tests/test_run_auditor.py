"""Synthetic profile homes, mocked owner transport; no HH/Telegram requests."""
import asyncio
from datetime import datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import analytics
import config
import notifier
import run_auditor as audit
from run_audit_rules import RULES, AuditContext
from state_store.private_journal import append_json, read_json_records
from tests.test_run_audit_rules import record


@pytest.fixture
def home(tmp_path):
    finished = datetime.now().astimezone().isoformat()
    append_json(tmp_path / 'run_history.jsonl', {'run_id': 'run', 'direction': 'qa', 'direction_title': 'Тестирование',
        'status': 'finished', 'finished_at': finished, 'ok': True})
    append_json(tmp_path / 'analytics_events.jsonl', record(event='search_finished', ok=True, finished_at=finished))
    return tmp_path


@pytest.fixture
def owner(monkeypatch):
    send = AsyncMock(return_value=True)
    monkeypatch.setattr(notifier, 'notify_audit_owner', send)
    monkeypatch.setattr(notifier, 'capture_audit_owner_target', lambda: ('control', (7,), ''))
    return send


def bad_card(home):
    append_json(home / 'analytics_events.jsonl', record(event='decision', decision='manual_hh_confirm',
        outcome='manual', score=0, evaluation_status='not_evaluated', evaluation_present=False))


def test_quiet_clean_run_no_finding_no_owner_notice(home, owner):
    asyncio.run(audit.audit_completed_run('run', home=home, profile='client'))
    owner.assert_not_awaited()
    assert not (home / audit.FINDINGS).exists()
    saved = read_json_records(home / audit.JOURNAL)
    assert saved[-1]['findings'] == 0 and saved[-1]['errors'] == []


def test_clean_setting_dev_marker_and_once_per_run(home, owner):
    (home / 'audit_settings.json').write_text(json.dumps({'send_clean': True}))
    for _ in range(2):
        asyncio.run(audit.audit_completed_run('run', home=home, profile='client-dev'))
    owner.assert_awaited_once()
    text = owner.await_args.args[0]
    assert 'нарушений нет' in text and '[dev]' in text and 'client-dev' in text
    assert owner.await_args.kwargs['target'] == ('control', (7,), '')


def test_append_only_findings_and_delivery_deduplication(home, owner):
    bad_card(home)
    asyncio.run(audit.audit_completed_run('run', home=home, profile='client'))
    before = (home / audit.FINDINGS).read_bytes()
    asyncio.run(audit.audit_completed_run('run', home=home, profile='client'))
    assert (home / audit.FINDINGS).read_bytes() == before
    owner.assert_awaited_once()
    rows = read_json_records(home / audit.FINDINGS)
    assert len(rows) == 1 and rows[0]['rule_id'] == 'A03'
    assert rows[0]['run_id'] == 'run' and rows[0]['vacancy_id'] == '42'
    assert (home / audit.FINDINGS).stat().st_mode & 0o777 == 0o600
    assert 'https://hh.ru/vacancy/42' in owner.await_args.args[0]


def test_cli_audit_does_not_consume_owner_notice(home, owner):
    bad_card(home)
    report = audit.audit_runs(home=home, run_id='run')[0]
    assert report['findings']
    asyncio.run(audit.audit_completed_run('run', home=home, profile='client'))
    owner.assert_awaited_once()


def test_failed_notice_can_retry_without_duplicate_finding(home, owner):
    bad_card(home)
    owner.side_effect = [False, True]
    for _ in range(3):
        asyncio.run(audit.audit_completed_run('run', home=home, profile='client'))
    assert owner.await_count == 2
    assert len(read_json_records(home / audit.FINDINGS)) == 1


def test_concurrent_audits_claim_one_notice(home, owner):
    bad_card(home)
    async def run():
        await asyncio.gather(*(audit.audit_completed_run('run', home=home, profile='client') for _ in range(4)))
    asyncio.run(run())
    owner.assert_awaited_once()
    assert len(read_json_records(home / audit.FINDINGS)) == 1


def test_overlapping_reports_do_not_repeat_claimed_finding(home, owner):
    bad_card(home)
    old = audit.audit_runs(home=home, run_id='run')[0]['findings'][0]
    assert audit._claim_notice(home, 'in-flight', [old['finding_id']], datetime.now().astimezone())
    append_json(home / 'analytics_events.jsonl', record(event='unexpected_ui', fingerprint='new-page'))
    asyncio.run(audit.audit_completed_run('run', home=home, profile='client'))
    owner.assert_awaited_once()
    text = owner.await_args.args[0]
    assert 'незнакомая страница' in text
    assert 'без проведённой оценки' not in text


def test_new_findings_after_previous_audit_notify_only_new(home, owner):
    bad_card(home)
    asyncio.run(audit.audit_completed_run('run', home=home, profile='client'))
    append_json(home / 'analytics_events.jsonl', record(event='unexpected_ui', fingerprint='synthetic-page'))
    asyncio.run(audit.audit_completed_run('run', home=home, profile='client'))
    assert owner.await_count == 2
    assert 'Подходящ' not in owner.await_args.args[0]
    assert len(read_json_records(home / audit.FINDINGS)) == 2


@pytest.mark.parametrize('fault', ['corrupt', 'rule', 'store', 'missing', 'bad_settings'])
def test_auditor_errors_never_escape_or_report_clean(home, owner, monkeypatch, fault, caplog):
    if fault == 'corrupt':
        (home / 'analytics_events.jsonl').write_text('{broken\n')
    elif fault == 'rule':
        def fail(ctx, rule):
            raise RuntimeError('PRIVATE_CONTENT')
        monkeypatch.setattr(audit, 'RULES', [SimpleNamespace(id='broken', check=fail)])
    elif fault == 'store':
        monkeypatch.setattr(audit, '_save', lambda *a: (_ for _ in ()).throw(OSError('PRIVATE_CONTENT')))
    elif fault == 'missing':
        (home / 'analytics_events.jsonl').unlink()
    else:
        (home / 'audit_settings.json').write_text('{"employer_days": -1}')
    asyncio.run(audit.audit_completed_run('run', home=home, profile='client-dev'))
    assert owner.await_count == 1
    text = owner.await_args.args[0].lower()
    assert 'аудитор не смог проверить' in text and 'нарушений нет' not in text
    assert '[dev]' in text and 'PRIVATE_CONTENT' not in caplog.text


def test_profile_inputs_and_trace_refs_cannot_escape_home(home, tmp_path, owner):
    outside = tmp_path / 'other-profile'
    outside.mkdir()
    (outside / 'trace.jsonl').write_text('SECRET')
    append_json(home / 'analytics_events.jsonl', record(event='debug_artifact', artifact_type='apply_trace',
                                                       artifact_ref=str(outside / 'trace.jsonl')))
    # outside lives below fixture home in this test; use a true sibling path.
    rows = read_json_records(home / 'analytics_events.jsonl')
    rows[-1]['artifact_ref'] = str(home.parent / 'unread-other-profile' / 'trace.jsonl')
    (home / 'analytics_events.jsonl').write_text(''.join(json.dumps(r) + '\n' for r in rows))
    asyncio.run(audit.audit_completed_run('run', home=home, profile='client'))
    assert 'не смог проверить' in owner.await_args.args[0]
    assert 'SECRET' not in owner.await_args.args[0]


def test_owner_report_is_bounded_grouped_escaped_and_links(home):
    report = audit.audit_runs(home=home, persist=False)[0]
    report['findings'] = [{
        'rule_id': f'A{i}', 'severity': severity, 'message': 'Замечание', 'finding_id': str(i),
        'run_id': 'run', 'vacancy_id': str(i), 'source': 'hh', 'title': '<script>',
        'company': '& Пример', 'url': 'javascript:alert(1)', 'evidence': {},
    } for i, severity in enumerate(['info', 'warning', 'error', 'critical'] * 6)]
    text = audit.format_report(report)
    assert len(text.splitlines()) <= 10 and 'audit --run run' in text
    assert '<script>' not in text and 'javascript:' not in text
    assert text.index('🛑') < text.index('❗')


def test_previous_day_clean_rollup_once_and_not_for_problem_day(home, owner):
    # One completed audit per unique run (checkpoints/re-audits are not extra runs).
    audit.audit_runs(home=home, run_id='run')
    now = datetime.now().astimezone() + timedelta(days=1)
    for _ in range(2):
        asyncio.run(audit.audit_daily_summary(home=home, profile='client-dev', now=now))
    owner.assert_awaited_once()
    assert 'прогонов 1, нарушений 0' in owner.await_args.args[0] and '[dev]' in owner.await_args.args[0]
    owner.reset_mock()
    bad_card(home)
    audit.audit_runs(home=home, run_id='run')
    # Use fresh delivery journal to ensure findings, rather than prior dedup, suppress clean daily line.
    rows = [r for r in read_json_records(home / audit.JOURNAL) if r['event'] == 'audit_completed']
    (home / audit.JOURNAL).write_text(''.join(json.dumps(r) + '\n' for r in rows))
    asyncio.run(audit.audit_daily_summary(home=home, profile='client', now=now))
    owner.assert_not_awaited()


def test_cli_selectors_output_and_no_notification(home, owner, monkeypatch, capsys):
    bad_card(home)
    monkeypatch.setattr(config, 'JOB_HUNTER_HOME', str(home))
    for argv in (['--run', 'run'], ['--since', '2026-10-01T00:00:00+03:00'], []):
        assert asyncio.run(audit.main(argv)) == 0
        text = capsys.readouterr().out
        assert 'A03' in text and 'Техник' in text and 'run' in text
    assert asyncio.run(audit.main(['--since', '2027-01-01'])) == 0
    assert 'Прогонов за указанный период нет' in capsys.readouterr().out
    assert asyncio.run(audit.main(['--run', 'missing'])) == 1
    assert 'не смог проверить' in capsys.readouterr().out
    owner.assert_not_awaited()


def test_cli_partial_rule_failure_has_nonzero_status(home, monkeypatch, capsys):
    monkeypatch.setattr(config, 'JOB_HUNTER_HOME', str(home))
    def fail(ctx, rule):
        raise ValueError('private')
    monkeypatch.setattr(audit, 'RULES', [SimpleNamespace(id='broken', check=fail)])
    assert asyncio.run(audit.main(['--run', 'run'])) == 1
    assert 'не смог проверить' in capsys.readouterr().out


def test_loader_joins_queue_and_chat_evidence_to_exact_run(home):
    append_json(home / 'analytics_events.jsonl', record(event='decision', decision='manual_hh_confirm', score=80))
    (home / 'manual_apply_queue.json').write_text(json.dumps({'items': {
        'right': {'run_id': 'run', 'vacancy': {'id': '42', 'source': 'hh'}, 'evaluation': {'score': 0, 'evaluation_status': 'not_evaluated'}},
        'other': {'run_id': 'other', 'vacancy': {'id': '42', 'source': 'hh'}, 'evaluation': {}},
    }}))
    append_json(home / 'chat_journal.jsonl', {'run_id': 'chat-local-id', 'event': 'chat_reply_sent', 'sent': True,
        'chat_id': '1', 'audit_evidence': {'analytics_run_id': 'run', 'ownership_status': 'unknown'}})
    report = audit.audit_runs(home=home, run_id='run')[0]
    assert [r['rule_id'] for r in report['findings']] == ['A03', 'A08']


def test_final_event_survives_missing_final_history_append(home):
    (home / 'run_history.jsonl').write_text(json.dumps({'run_id': 'run', 'status': 'incomplete', 'finished_at': None}) + '\n')
    report = audit.audit_runs(home=home, run_id='run')[0]
    assert report['run']['status'] == 'finished' and report['run']['finished_at']
    assert not report['errors']


def test_legacy_chat_sent_decision_cannot_be_reported_clean(home):
    append_json(home / 'chat_journal.jsonl', {'run_id': 'chat-run', 'event': 'chat_decision',
        'observed_at': datetime.now().astimezone().isoformat(), 'chat_id': '5', 'reason': 'sent'})
    report = audit.audit_runs(home=home, run_id='chat-run')[0]
    assert [r['rule_id'] for r in report['findings']] == ['A08']


def test_daily_never_calls_unaudited_runs_clean(home, owner):
    audit.audit_runs(home=home, run_id='run')
    append_json(home / 'run_history.jsonl', {'run_id': 'unverified', 'status': 'finished',
        'finished_at': datetime.now().astimezone().isoformat()})
    asyncio.run(audit.audit_daily_summary(home=home, profile='client', now=datetime.now().astimezone() + timedelta(days=1)))
    owner.assert_not_awaited()


def test_decision_saves_exact_conditions_evaluation_and_letter_binding(home, monkeypatch):
    monkeypatch.setattr(config, 'JOB_HUNTER_HOME', str(home))
    monkeypatch.setattr(config, 'ANALYTICS_EVENTS_FILE', str(home / 'events.jsonl'))
    direction = {'name': 'qa', 'work_conditions': {'allowed_formats': ['remote'], 'allow_fly_in_fly_out': False}}
    monkeypatch.setattr(config, 'ACTIVE_DIRECTION', direction, raising=False)
    # no variant selection in runtime_metadata when vacancy ID absent
    vacancy = {'source': 'hh', '_hh_work_conditions': {'formats': ['on_site'], 'fly_in_fly_out': False},
        '_cover_letter_selection': {'direction': 'electric', 'sha256': 'letter-digest', 'profile_home': 'PRIVATE'}}
    analytics.record_decision(run_id='run', vacancy=vacancy, decision='manual_hh_confirm')
    row = read_json_records(home / 'events.jsonl')[0]
    assert row['_hh_work_conditions'] == vacancy['_hh_work_conditions']
    assert row['evaluation_present'] is False
    assert row['_cover_letter_selection']['direction'] == 'electric'
    assert 'PRIVATE' not in json.dumps(row)
    assert row['audit_direction']['work_conditions'] == direction['work_conditions']


def test_trace_discovery_uses_own_run_context_even_without_reference(home):
    path = home / 'traces' / '2026-10-21' / 'hh_42_100000' / 'trace.jsonl'
    append_json(path, record(stage='TRACE_STARTED', trace_id='trace'))
    append_json(path, record(stage='HH_SERVER_DELIVERY', trace_id='trace', status='confirmed'))
    report = audit.audit_runs(home=home, run_id='run')[0]
    assert [f['rule_id'] for f in report['findings']] == ['A07']
