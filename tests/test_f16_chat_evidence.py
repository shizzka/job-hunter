"""Passive audit evidence and owner routing use synthetic state/transports only."""
import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock
import pytest

import analytics
import chat_context
import hh_chat_responder as cr
import notifier
from tests.test_chat_workflow_transactions import workflow


def sent_events(workflow):
    path = Path(workflow.paths.home_dir) / 'chat_journal.jsonl'
    return [row for row in map(json.loads, path.read_text().splitlines())
            if row['event'] == 'chat_reply_sent']


def test_sent_reply_records_candidate_and_positive_application_proof(workflow):
    with analytics.event_context(run_id='synthetic-run'):
        result = asyncio.run(workflow.run())
    assert result['sent']
    row, = sent_events(workflow)
    assert row['chat_id'] == 'chat' and row['message_id'] == 'm1'
    assert row['sent'] is True and row['vacancy']['title'] == 'Synthetic QA'
    evidence = row['audit_evidence']
    assert evidence['ownership_status'] == chat_context.OWN
    assert evidence['ownership_resume_id'] == evidence['binding_resume_id'] == '111'
    assert evidence['ownership_direction'] == evidence['binding_direction'] == evidence['facts_direction'] == 'qa'
    assert evidence['binding_profile_home'] == evidence['facts_profile_home'] == workflow.paths.home_dir
    assert evidence['resume_digest'] == hashlib.sha256(b'Synthetic resume').hexdigest()
    assert len(evidence['facts_digest']) == 64
    assert evidence['analytics_run_id'] == 'synthetic-run'
    assert row['run_id']
    assert 'Synthetic resume' not in json.dumps(evidence)


def test_preview_does_not_record_sent_evidence(workflow):
    assert asyncio.run(workflow.run(dry_run=True))['ok']
    assert not sent_events(workflow)


def test_evidence_failure_does_not_change_reply_result(workflow, monkeypatch):
    monkeypatch.setattr(cr, '_reply_audit_evidence', lambda *args: None)
    assert asyncio.run(workflow.run())['sent']
    row, = sent_events(workflow)
    assert row['audit_evidence'] is None


def test_journal_failure_does_not_change_completed_send(workflow, monkeypatch):
    from chat_observation import ChatJournal
    write = ChatJournal.write
    def fail_only_sent(self, event, **fields):
        if event == 'chat_reply_sent':
            raise OSError('Synthetic journal failure')
        return write(self, event, **fields)
    monkeypatch.setattr(ChatJournal, 'write', fail_only_sent)
    assert asyncio.run(workflow.run())['sent']
    assert workflow.repo.load()['chat']['replies_count'] == 1


def test_owner_target_ignores_active_profile_and_admins(monkeypatch):
    monkeypatch.setenv('NOTIFY_CHAT_ID', '101')
    monkeypatch.setenv('OWNER_CHAT_ID', '102')
    monkeypatch.setenv('TELEGRAM_PROXY', 'socks5://synthetic')
    monkeypatch.setattr(notifier.config, 'NOTIFY_CHAT_ID', 999)
    monkeypatch.setattr(notifier.config, 'TELEGRAM_BOT_TOKEN', 'client-token')
    monkeypatch.setattr(notifier.config, 'TELEGRAM_CONTROL_BOT_TOKEN', 'control-token')
    monkeypatch.setattr(notifier, '_active_profile', lambda: (_ for _ in ()).throw(AssertionError('client lookup')))
    monkeypatch.setattr(notifier, '_admin_chat_ids', lambda: (_ for _ in ()).throw(AssertionError('admin broadcast')))
    deliver = AsyncMock(return_value=True)
    monkeypatch.setattr(notifier, '_deliver', deliver)
    target = notifier.capture_audit_owner_target()
    monkeypatch.setenv('NOTIFY_CHAT_ID', '999')
    assert asyncio.run(notifier.notify_audit_owner('&lt;synthetic&gt;', target=target))
    args, kwargs = deliver.call_args
    assert args[0] == 'https://api.telegram.org/botcontrol-token/sendMessage'
    assert args[1]['chat_id'] == 101
    assert args[1]['text'] == '&lt;synthetic&gt;' and args[1]['parse_mode'] == 'HTML'
    assert kwargs['proxy_url'] == 'socks5://synthetic'


def test_owner_delivery_fails_closed_without_global_owner(monkeypatch):
    monkeypatch.delenv('NOTIFY_CHAT_ID', raising=False)
    monkeypatch.delenv('OWNER_CHAT_ID', raising=False)
    monkeypatch.setattr(notifier.config, 'NOTIFY_CHAT_ID', 999)
    monkeypatch.setattr(notifier.config, 'TELEGRAM_CONTROL_BOT_TOKEN', 'control-token')
    deliver = AsyncMock(return_value=True)
    monkeypatch.setattr(notifier, '_deliver', deliver)
    assert not asyncio.run(notifier.notify_audit_owner('Synthetic'))
    deliver.assert_not_awaited()


def test_owner_delivery_uses_owner_env_fallback_and_propagates_failure(monkeypatch):
    monkeypatch.delenv('NOTIFY_CHAT_ID', raising=False)
    monkeypatch.setenv('OWNER_CHAT_ID', '102')
    monkeypatch.setattr(notifier.config, 'TELEGRAM_CONTROL_BOT_TOKEN', 'control-token')
    deliver = AsyncMock(return_value=False)
    monkeypatch.setattr(notifier, '_deliver', deliver)
    assert not asyncio.run(notifier.notify_audit_owner('Synthetic'))
    assert deliver.call_args.args[1]['chat_id'] == 102


def test_post_send_owner_change_preserves_pre_click_proof(workflow, monkeypatch):
    async def send(*args, **kwargs):
        await kwargs['before_send']()
        (Path(workflow.paths.home_dir) / 'negotiation_snapshots.jsonl').unlink()
        return True
    monkeypatch.setattr(cr, 'send_message', send)
    assert asyncio.run(workflow.run())['sent']
    row, = sent_events(workflow)
    assert row['audit_evidence']['ownership_status'] == 'own'
    assert row['audit_evidence']['ownership_resume_id'] == '111'
    assert row['hh_resume_numeric_id'] is None


def test_unconfirmed_send_does_not_emit_sent_proof(workflow, monkeypatch):
    async def send(*args, **kwargs):
        await kwargs['before_send']()
        return False
    monkeypatch.setattr(cr, 'send_message', send)
    assert not asyncio.run(workflow.run())['sent']
    assert not sent_events(workflow)


def test_standalone_reply_enqueues_a08_audit_after_final_observation(workflow, monkeypatch):
    import run_auditor
    from state_store.private_journal import read_json_records
    queued = []
    def enqueue(run_id, **destination):
        rows = read_json_records(Path(destination['home']) / 'chat_journal.jsonl')
        assert rows[-1]['event'] == 'chat_reply_attempt'
        assert rows[-1]['reason'] == 'sent'
        queued.append((run_id, destination))
        return True
    monkeypatch.setattr(run_auditor, 'enqueue_completed_run', enqueue)
    # Audit telemetry is synthetic; the actual reply gates still see own binding.
    monkeypatch.setattr(cr, '_reply_audit_evidence', lambda *args: {'ownership_status': 'unknown'})
    assert asyncio.run(workflow.run())['sent']
    (run_id, destination), = queued
    # The workflow's minimal ownership-only definitions predate the full
    # direction schema required by the auditor loader.
    from tests.test_directions import saved
    saved(Path(destination['home']), 'qa', count=1)
    saved(Path(destination['home']), 'electric', count=1)
    report, = run_auditor.audit_runs(home=destination['home'], profile=destination['profile'], run_id=run_id)
    assert [r['rule_id'] for r in report['findings']] == ['A08']


@pytest.mark.parametrize('dry_run', [False, True])
def test_search_owned_reply_and_preview_do_not_enqueue_standalone_audit(workflow, monkeypatch, dry_run):
    import run_auditor
    from unittest.mock import Mock
    enqueue = Mock(return_value=True)
    monkeypatch.setattr(run_auditor, 'enqueue_completed_run', enqueue)
    with analytics.event_context(run_id='search-run'):
        result = asyncio.run(workflow.run(dry_run=dry_run))
    assert result['ok'] and (dry_run or result['sent'])
    enqueue.assert_not_called()


def test_standalone_chat_scan_enqueues_once_after_all_decisions(workflow, monkeypatch):
    import run_auditor
    from state_store.private_journal import read_json_records
    queued = []
    def enqueue(run_id, **destination):
        rows = read_json_records(Path(destination['home']) / 'chat_journal.jsonl')
        assert rows[-1]['event'] == 'chat_decision' and rows[-1]['reason'] == 'sent'
        assert cr.active_chat_journal.get() is None
        queued.append(run_id)
        return True
    monkeypatch.setattr(run_auditor, 'enqueue_completed_run', enqueue)
    result = asyncio.run(workflow.run_all(dry_run=False))
    assert result['answers_sent'] == 1 and queued == [result['run_id']]


def test_chat_audit_handoff_failure_preserves_successful_reply(workflow, monkeypatch):
    import run_auditor
    def fail(*args, **kwargs):
        raise OSError('synthetic handoff failure')
    monkeypatch.setattr(run_auditor, 'enqueue_completed_run', fail)
    assert asyncio.run(workflow.run())['sent']
