"""One positive/negative contract per auditor rule, using saved-schema fixtures."""
from copy import deepcopy

import pytest

from run_audit_rules import AuditContext, RULES


def record(**fields):
    return {'run_id': 'run', 'source': 'hh', 'vacancy_id': '42', 'title': 'Техник',
            'company': 'Пример', 'url': 'https://hh.ru/vacancy/42', 'direction': 'qa',
            'created_at': '2026-10-21T10:00:00+03:00', **fields}


def context(events=(), **kwargs):
    ctx = AuditContext({'run_id': 'run', 'direction': 'qa'}, events=list(events), all_events=list(events),
        directions={'qa': {'name': 'qa', 'variants': [{'hh_resume_hash': 'qa-hash', 'hh_resume_numeric_id': 100,
                    'text_sha256': 'qa-digest'}]},
                    'electric': {'name': 'electric', 'variants': [{'hh_resume_hash': 'electric-hash',
                    'hh_resume_numeric_id': 200, 'text_sha256': 'electric-digest'}]}}, home='/synthetic/home')
    for key, value in kwargs.items():
        setattr(ctx, key, value)
    return ctx


def check(rule_id, ctx):
    rule = next(r for r in RULES if r.id == rule_id)
    rows = list(rule.check(ctx, rule))
    for row in rows:
        assert row['rule_id'] == rule_id and row['severity'] and row['message']
        assert row['run_id'] == 'run' and 'evidence' in row
        assert all(k in row for k in ('vacancy_id', 'title', 'company', 'source'))
    return rows


@pytest.mark.parametrize('field,bad,good', [
    ('resume_id', 'electric-hash', 'qa-hash'), ('selected_resume_id', 'electric-hash', 'qa-hash'),
    ('hh_resume_numeric_id', 200, 100), ('matcher_resume_sha256', 'electric-digest', 'qa-digest'),
    ('cover_letter_resume_sha256', 'electric-digest', 'qa-digest'),
    ('_cover_letter_selection', {'direction': 'electric'}, {'direction': 'qa'}),
    ('_cover_letter_selection', {'direction': 'qa', 'hh_resume_hash': 'electric-hash'},
                              {'direction': 'qa', 'hh_resume_hash': 'qa-hash'}),
    ('_evaluation_selection', {'direction': 'electric'}, {'direction': 'qa'}),
    ('evaluation', {'direction': 'electric'}, {'direction': 'qa'}),
    ('direction', 'electric', 'qa'),
])
def test_a01_direction_mix_positive_and_negative(field, bad, good):
    row = record(event='decision', **{field: bad})
    assert check('A01', context([row]))
    row[field] = good
    assert not check('A01', context([row]))
    if field not in {'evaluation', '_cover_letter_selection', '_evaluation_selection', 'direction'}:
        row[field] = 'unknown-legacy-id'
        assert not check('A01', context([row]))


@pytest.mark.parametrize('fields', [
    {'formats': ['on_site'], 'fly_in_fly_out': True},
    {'formats': ['on_site'], 'fly_in_fly_out': False},
    {'formats': ['hybrid'], 'fly_in_fly_out': False, 'city': 'Другой город'},
])
def test_a02_work_conditions_positive_negative_and_exact_b26(monkeypatch, fields):
    import work_conditions
    direction = {'name': 'qa', 'work_conditions': {'allowed_formats': ['remote', 'hybrid'],
        'allow_fly_in_fly_out': False, 'allowed_cities': ['Город Пример']}}
    row = record(event='decision', decision='manual_hh_confirm', outcome='manual', _hh_work_conditions=fields)
    ctx = context([row])
    ctx.directions['qa'].update(direction)
    calls = []
    original = work_conditions.check
    def spy(v, details='', *, direction=None):
        calls.append(direction)
        return original(v, details, direction=direction)
    monkeypatch.setattr(work_conditions, 'check', spy)
    assert check('A02', ctx) and calls == [ctx.directions['qa']]
    row['_hh_work_conditions'] = {'formats': ['remote'], 'fly_in_fly_out': False}
    assert not check('A02', ctx)
    row['_hh_work_conditions'] = {'formats': [], 'fly_in_fly_out': False}
    assert not check('A02', ctx)  # Unknown is review, never an invented violation.
    row['_hh_work_conditions'] = fields
    row.update(decision='skipped_work_conditions', outcome='skipped')
    assert not check('A02', ctx)


@pytest.mark.parametrize('score,status,present', [(None, 'not_evaluated', False), (0, 'not_evaluated', True),
                                                 (0, 'scored', False), (0, None, None)])
def test_a03_card_score_positive_negative(score, status, present):
    row = record(event='decision', decision='manual_hh_confirm', outcome='manual', score=score,
                 evaluation_status=status, evaluation_present=present)
    ctx = context([row])
    assert check('A03', ctx)
    row.update(score=0, evaluation_status='scored', evaluation_present=True)
    assert not check('A03', ctx)  # A genuinely evaluated zero is not this defect.
    row.update(score=None, decision='manual_work_conditions', evaluation_status='not_evaluated')
    assert not check('A03', ctx)  # Explicitly unscored "на разбор" from B26.


def test_a04_duplicate_company_within_run_and_window_per_direction():
    first = record(event='application_result', outcome='sent', application_id='one')
    second = record(event='application_result', outcome='sent', application_id='two', vacancy_id='43')
    ctx = context([first, second, record(event='decision', decision='applied_auto', vacancy_id='43')])
    assert len(check('A04', ctx)) == 1  # Receipt and terminal decision are one action.
    second['direction'] = 'electric'
    assert not check('A04', ctx)
    second['direction'] = 'qa'
    first.update(run_id='previous', created_at='2026-10-19T10:00:00+03:00')
    ctx.events = [second]
    assert check('A04', ctx)
    ctx.settings['employer_days'] = 1
    assert not check('A04', ctx)
    ctx.settings['employer_days'] = 0
    first['run_id'] = 'run'
    assert check('A04', ctx)  # Zero disables history window only.


def test_a04_unidentified_employer_and_dry_run_do_not_invent_duplicates():
    ctx = context([record(event='decision', decision='dry_run_match', mode='dry-run'),
                   record(event='application_result', outcome='sent', application_id='one', company='')])
    assert not check('A04', ctx)


def test_a04_legacy_receipts_recover_employer_from_same_terminal_decision():
    prior = record(event='application_result', outcome='sent', run_id='previous', vacancy_id='40', company='',
                   application_id='previous', created_at='2026-10-20T10:00:00+03:00')
    decision = {**prior, 'event': 'decision', 'decision': 'applied_auto', 'company': 'Пример'}
    current = record(event='application_result', outcome='sent', application_id='current')
    ctx = context([current], all_events=[prior, decision, current])
    assert check('A04', ctx)


@pytest.mark.parametrize('stop', [record(event='hh_breaker_stop'),
    record(event='decision', decision='guard_stop'),
    record(event='hh_ui_isolation', stopped=True)])
def test_a05_attempt_after_stop_positive_and_negative(stop):
    attempt = record(event='application_attempt')
    assert check('A05', context([stop, attempt]))
    assert not check('A05', context([attempt, stop]))
    assert not check('A05', context([stop, {**attempt, 'source': 'habr'}]))
    assert not check('A05', context([record(event='decision', decision='guard_stop', note='hh:recovered_ui'), attempt]))


def trace(*events, **fields):
    return record(trace_id='trace', application_id='one', events=list(events), **fields)


@pytest.mark.parametrize('verification', [{}, {'ok': False, 'resume_match': False},
                                         {'ok': True, 'letter_match': False}])
def test_a06_submission_needs_own_successful_final_gate(verification):
    result = record(event='application_result', outcome='sent', application_id='one', trace_id='trace')
    t = trace({'stage': 'PRE_SUBMIT_VERIFY', **verification}, {'stage': 'SUBMIT_CLICK', 'ok': True})
    ctx = context([result], traces=[t])
    assert check('A06', ctx)
    t['events'][0] = {'stage': 'PRE_SUBMIT_VERIFY', 'ok': True, 'resume_match': True, 'letter_match': True}
    assert not check('A06', ctx)
    t['events'] = [{'stage': 'PRE_SUBMIT_VERIFY', 'ok': False}, {'stage': 'TRACE_FAILED'}]
    ctx.events = [record(event='application_result', outcome='failed')]
    assert not check('A06', ctx)  # Safely stopped before submit is healthy.


def test_a06_no_cross_attempt_verification_or_post_submit_proof():
    result = record(event='application_result', outcome='sent', application_id='two', trace_id='second')
    first = trace({'stage': 'PRE_SUBMIT_VERIFY', 'ok': True, 'letter_match': True})
    second = trace({'stage': 'SUBMIT_CLICK', 'ok': True}, {'stage': 'PRE_SUBMIT_VERIFY', 'ok': True})
    second.update(trace_id='second', application_id='two')
    assert check('A06', context([result], traces=[first, second]))
    assert check('A06', context([result]))


def test_a07_delivery_receipt_mismatch_both_directions_and_negative():
    result = record(event='application_result', outcome='sent', application_id='one', trace_id='trace')
    t = trace({'stage': 'HH_SERVER_DELIVERY', 'status': 'unknown'})
    ctx = context([result], traces=[t])
    assert check('A07', ctx)[0]['evidence']['mismatch'] == 'sent_without_confirmation'
    t['events'][0]['status'] = 'confirmed'
    assert not check('A07', ctx)
    ctx.events = []
    assert check('A07', ctx)[0]['evidence']['mismatch'] == 'confirmation_without_result'
    t['events'][0]['status'] = 'unknown'
    assert not check('A07', ctx)
    ctx.events = [{**result, 'server_delivery': {'status': 'confirmed'}}]
    assert not check('A07', ctx)


def test_a07_native_verified_receipt_without_result_is_not_silently_lost():
    t = trace({'stage': 'POST_SUBMIT_RECONCILIATION', 'step': 'verified', 'ok': True})
    ctx = context(traces=[t])
    assert check('A07', ctx)[0]['evidence']['mismatch'] == 'confirmation_without_result'
    ctx.events = [record(event='application_result', outcome='sent', application_id='one', trace_id='trace')]
    assert not check('A07', ctx)


def test_a08_reply_requires_positive_ownership_and_same_direction_facts():
    evidence = {'ownership_status': 'own', 'ownership_resume_id': '100', 'binding_resume_id': '100',
                'ownership_direction': 'qa', 'binding_direction': 'qa', 'facts_direction': 'qa',
                'binding_profile_home': '/synthetic/home', 'facts_profile_home': '/synthetic/home'}
    reply = record(event='chat_reply_sent', sent=True, chat_id='5', message_id='9', audit_evidence=evidence)
    ctx = context(chats=[reply])
    assert not check('A08', ctx)
    for key, bad in [('ownership_status', 'unknown'), ('ownership_resume_id', ''),
                     ('facts_direction', 'electric'), ('facts_profile_home', '/other')]:
        changed = deepcopy(reply)
        changed['audit_evidence'][key] = bad
        assert check('A08', context(chats=[changed]))
    reply['audit_evidence'] = None
    assert check('A08', ctx)
    reply['sent'] = False
    assert not check('A08', ctx)


def test_a09_llm_error_ratio_above_threshold_informational():
    calls = [record(event='llm_call', outcome='error', provider='one') for _ in range(3)]
    calls += [record(event='llm_call', outcome='response', provider='one')]
    ctx = context(calls)
    assert check('A09', ctx)[0]['severity'] == 'info'
    ctx.settings['llm_error_threshold'] = .75
    assert not check('A09', ctx)
    assert not check('A09', context())


def test_a10_stage_failures_and_unexpected_ui_with_stable_fingerprint():
    ctx = context([record(event='stage_failed', stage='matcher', reason_code='LLM_RATE_LIMIT'),
                   record(event='unexpected_ui', fingerprint='known-page')])
    rows = check('A10', ctx)
    assert len(rows) == 2 and all(r['severity'] == 'info' for r in rows)
    assert all(r['evidence']['fingerprint'] for r in rows)
    assert rows == check('A10', ctx)
    assert not check('A10', context([record(event='search_finished', ok=True)]))


def test_a10_manual_final_exception_without_stage_event_is_not_clean():
    ctx = context()
    ctx.run.update(kind='manual-apply', status='finished', ok=False, error_kind='ValueError')
    assert check('A10', ctx)[0]['evidence']['error_kind'] == 'ValueError'


def test_explicit_conflicting_application_ids_cannot_join_even_with_same_trace_id():
    result = record(event='application_result', outcome='sent', application_id='two', trace_id='trace')
    t = trace({'stage': 'PRE_SUBMIT_VERIFY', 'ok': True},
              {'stage': 'HH_SERVER_DELIVERY', 'status': 'confirmed'})
    ctx = context([result], traces=[t])
    assert not ctx.trace_for(result)
    assert check('A06', ctx) and check('A07', ctx)
