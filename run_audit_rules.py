"""Pure, independent checks of saved run evidence. No IO or runtime config reads."""
from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta
import hashlib
import json

import work_conditions
from apply_audit import INVALIDATES_VERIFICATION


def timestamp(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value.replace('Z', '+00:00')).astimezone()
    except ValueError:
        return None


def vacancy_key(row):
    source = row.get('source') or 'hh'
    return source, str(row.get('vacancy_id') or row.get('id') or '').removeprefix(source + ':')


def is_sent(row):
    return (not row.get('dry_run') and row.get('mode') not in {'dry-run', 'historical'}
            and not row.get('historical') and (
                row.get('event') == 'application_result' and row.get('outcome') == 'sent'
                or row.get('event') == 'decision' and row.get('decision') == 'applied_auto'))


def is_card(row):
    # B26 deliberately sends an unscored "на разбор" card; that is not a match.
    if row.get('card_kind') in {'review', 'uncertain'} or row.get('decision') in {
            'manual_work_conditions', 'apply_uncertain', 'guard_stop'}:
        return False
    return (row.get('card_kind') in {'suitable', 'manual'} or row.get('event') == 'manual_card'
            or row.get('event') == 'decision' and (
                row.get('outcome') in {'manual', 'dry_run_match'}
                or str(row.get('decision', '')).startswith('manual_')
                or row.get('decision') in {'dry_run_match', 'questions_required'}))


@dataclass
class AuditContext:
    run: dict
    events: list[dict] = field(default_factory=list)
    all_events: list[dict] = field(default_factory=list)
    directions: dict[str, dict] = field(default_factory=dict)
    traces: list[dict] = field(default_factory=list)
    cards: list[dict] = field(default_factory=list)
    chats: list[dict] = field(default_factory=list)
    snapshots: list[dict] = field(default_factory=list)
    settings: dict = field(default_factory=dict)
    home: str = ''

    def direction(self, row):
        return ((row.get('_direction_selection') or {}).get('direction')
                or self.run.get('direction') or row.get('direction') or '')

    def identity(self, row):
        """Enrich labels only from this run and this source, never change evidence."""
        key = vacancy_key(row)
        same = [e for e in self.events + self.cards if key[1] and vacancy_key(e) == key]
        return {name: row.get(name) or next((e[name] for e in reversed(same) if e.get(name)), '')
                for name in ('title', 'company', 'url')}

    def trace_for(self, row):
        matches = [t for t in self.traces if vacancy_key(t) == vacancy_key(row)]
        if row.get('application_id'):
            exact = [t for t in matches if t.get('application_id') == row['application_id']]
            if exact:
                return exact
            matches = [t for t in matches if not t.get('application_id')]
        if row.get('trace_id'):
            return [t for t in matches if t.get('trace_id') == row['trace_id']]
        # Legacy joins are safe only when there is exactly one attempt/trace.
        attempts = [e for e in self.events if e.get('event') == 'application_attempt' and vacancy_key(e) == vacancy_key(row)]
        return matches if len(matches) == 1 and len(attempts) <= 1 else []

    def applications(self, *, history=False):
        events = self.all_events if history else self.events
        decisions = defaultdict(list)
        for e in events:
            if e.get('event') == 'decision' and is_sent(e):
                decisions[(e.get('run_id'), *vacancy_key(e))].append(e)
        receipts = [e for e in events if is_sent(e) and e.get('event') == 'application_result']
        covered = {(e.get('run_id'), *vacancy_key(e)) for e in receipts}
        receipts += [e for e in events if is_sent(e) and e.get('event') == 'decision'
                     and (e.get('run_id'), *vacancy_key(e)) not in covered]
        unique = {}
        for e in receipts:
            # Legacy workflow receipts omitted labels that their terminal
            # decision saved. Join within the same run/source/vacancy only.
            matches = decisions[(e.get('run_id'), *vacancy_key(e))]
            e = dict(e)
            for name in ('title', 'company', 'url', 'direction', 'employer_id'):
                if not e.get(name):
                    e[name] = next((d[name] for d in reversed(matches) if d.get(name)), '')
            key = (e.get('run_id'), e.get('application_id') or vacancy_key(e))
            unique[key] = e
        return list(unique.values())


@dataclass(frozen=True)
class Rule:
    id: str
    severity: str
    message: str
    check: object


def finding(rule, ctx, row=None, **evidence):
    row = row or {}
    labels = ctx.identity(row)
    return {'rule_id': rule.id, 'severity': rule.severity, 'message': rule.message,
            'run_id': ctx.run['run_id'], 'direction': ctx.direction(row),
            'vacancy_id': vacancy_key(row)[1], 'source': vacancy_key(row)[0], **labels,
            'evidence': evidence}


def direction_mix(ctx, rule):
    owners = defaultdict(set)
    for name, direction in ctx.directions.items():
        for variant in direction.get('variants', []):
            for key in ('hh_resume_hash', 'hh_resume_numeric_id', 'text_sha256'):
                if variant.get(key):
                    owners[str(variant[key])].add(name)
    rows = ctx.events + ctx.cards
    for trace in ctx.traces:
        rows += [{**trace, **e} for e in trace['events'] if e.get('stage') in {
            'APPLICATION_CONTEXT', 'DISPATCH_APPLY', 'RESUME_SELECTED'}]
    for row in rows:
        if not (row.get('event') in {'decision', 'application_attempt', 'application_result', 'manual_card'}
                or row.get('stage') in {'APPLICATION_CONTEXT', 'DISPATCH_APPLY', 'RESUME_SELECTED'}):
            continue
        expected = ctx.direction(row)
        if not expected:
            continue
        fields = {k: row.get(k) for k in ('resume_id', 'requested_resume_id', 'selected_resume_id',
            'hh_resume_hash', 'hh_resume_numeric_id', 'variant_text_sha256', 'matcher_resume_sha256',
            'cover_letter_resume_sha256') if row.get(k)}
        for label, data in (('evaluation', row.get('_evaluation_selection') or row.get('evaluation') or {}),
                            ('cover_letter', row.get('_cover_letter_selection') or {})):
            for key in ('hh_resume_hash', 'hh_resume_numeric_id', 'variant_text_sha256'):
                if data.get(key):
                    fields[label + '_' + key] = data[key]
        for stage, value in (row.get('_resume_input_versions') or {}).items():
            if value.get('sha256'):
                fields[stage + '_resume_sha256'] = value['sha256']
        contradictions = {key: {'value': val, 'owners': sorted(owners[str(val)])}
                          for key, val in fields.items() if owners[str(val)] and expected not in owners[str(val)]}
        selection = row.get('_direction_selection') or {}
        for label, data in (('record', row), ('evaluation', row.get('_evaluation_selection') or row.get('evaluation') or {}),
                            ('cover_letter', row.get('_cover_letter_selection') or {})):
            if data.get('direction') and data['direction'] != expected:
                contradictions[label + '_direction'] = data['direction']
        if selection.get('direction') and row.get('direction') and row['direction'] != selection['direction']:
            contradictions['vacancy_direction'] = selection['direction']
        if contradictions:
            yield finding(rule, ctx, row, expected_direction=expected, fields=contradictions)


def work_condition_violation(ctx, rule):
    for row in ctx.events + ctx.cards:
        if not (is_sent(row) or is_card(row)):
            continue
        direction = row.get('audit_direction') or ctx.run.get('audit_direction')
        if not direction or direction.get('name') != ctx.direction(row):
            direction = ctx.directions.get(ctx.direction(row))
        if not direction:
            continue
        # The exact B26 policy; never infer format/region from query or job title.
        result = work_conditions.check(row, row.get('details') or '', direction=direction)
        if result['status'] == 'reject' or is_sent(row) and result['status'] != 'allow':
            yield finding(rule, ctx, row, conditions=row.get('_hh_work_conditions'),
                          policy=direction.get('work_conditions', work_conditions.defaults(direction)), check=result)


def missing_score(ctx, rule):
    for row in ctx.events + ctx.cards:
        if not is_card(row):
            continue
        status, score = row.get('evaluation_status'), row.get('score')
        # A real evaluated zero is a score. A legacy default zero has no proof.
        evaluated = status == 'scored' and row.get('evaluation_present', True) is not False
        evaluated |= bool(row.get('matcher_resume_sha256') and status not in {'not_evaluated', 'deferred_unscored'})
        if score is None or score == 0 and not evaluated:
            yield finding(rule, ctx, row, score=score, evaluation_status=status,
                          evaluation_present=row.get('evaluation_present'))


def duplicate_employer(ctx, rule):
    days = ctx.settings.get('employer_days', 7)
    groups = defaultdict(list)
    snapshots = defaultdict(set)
    for s in ctx.snapshots:
        if s.get('employerId'):
            snapshots[str(s.get('vacancyId'))].add(str(s['employerId']))
    def company(row):
        ids = snapshots.get(vacancy_key(row)[1], set()) if vacancy_key(row)[0] == 'hh' else set()
        name = ' '.join(str(row.get('company') or ctx.identity(row)['company']).casefold().split())
        return ('id:' + str(row['employer_id']) if row.get('employer_id') else
                'id:' + next(iter(ids)) if len(ids) == 1 else 'name:' + name if name else '')
    # Resolve label-only and ID-bearing records together when unambiguous.
    rows = ctx.applications(history=True)
    aliases = defaultdict(set)
    for row in rows:
        name = ' '.join(str(row.get('company') or '').casefold().split())
        identity = company(row)
        if name and identity.startswith('id:'):
            aliases[name].add(identity)
    for row in rows:
        identity = company(row)
        if identity.startswith('name:') and len(aliases[identity[5:]]) == 1:
            identity = next(iter(aliases[identity[5:]]))
        if identity:
            groups[(row.get('direction') or (ctx.run.get('direction') if row.get('run_id') == ctx.run['run_id'] else ''), identity)].append(row)
    for (direction, employer), rows in groups.items():
        for current in rows:
            if current.get('run_id') != ctx.run['run_id']:
                continue
            now = timestamp(current.get('recorded_at_utc') or current.get('created_at'))
            previous = []
            for other in rows:
                if other is current:
                    continue
                at = timestamp(other.get('recorded_at_utc') or other.get('created_at'))
                if other.get('run_id') == current.get('run_id'):
                    # Emit once on the later receipt even without timestamps.
                    if rows.index(other) < rows.index(current):
                        previous.append(other)
                elif days and now and at and now - timedelta(days=days) <= at <= now:
                    previous.append(other)
            if previous:
                yield finding(rule, ctx, current, employer=employer, days=days,
                              previous=[{k: p.get(k) for k in ('run_id', 'vacancy_id', 'created_at', 'application_id')} for p in previous])


def after_stop(ctx, rule):
    stopped = {}
    for row in ctx.events:
        source = row.get('source') or 'hh'
        stop = (row.get('event') == 'hh_breaker_stop'
                or row.get('event') == 'hh_ui_isolation' and row.get('stopped') is True
                or row.get('decision') == 'guard_stop' and row.get('note') != 'hh:recovered_ui'
                or row.get('decision') == 'not_processed' and row.get('note') == 'hh_recovery_breaker')
        if stop:
            stopped.setdefault(source, row)
        if row.get('event') == 'application_attempt' and source in stopped:
            yield finding(rule, ctx, row, stop={k: stopped[source].get(k) for k in
                          ('event', 'event_id', 'created_at', 'decision', 'note', 'reason')}, application_id=row.get('application_id'))


def submit_unverified(ctx, rule):
    sent = ctx.applications()
    for trace in ctx.traces:
        check = {}
        submitted = False
        for row in trace['events']:
            if row.get('stage') == 'PRE_SUBMIT_VERIFY':
                check = row
            elif row.get('stage') in INVALIDATES_VERIFICATION:
                check = {}
            elif row.get('stage') == 'SUBMIT_CLICK':
                if row.get('ok') is True and (check.get('ok') is not True or check.get('resume_match') is False
                                              or check.get('letter_match') is False):
                    yield finding(rule, ctx, trace, trace_id=trace.get('trace_id'), verification=check, submit=row)
                submitted |= row.get('ok') is True
                # Every dispatch needs fresh verification, including retries.
                check = {}
        relevant = [e for e in sent if trace in ctx.trace_for(e)]
        if relevant and not submitted:
            if check.get('ok') is not True or check.get('resume_match') is False or check.get('letter_match') is False:
                yield finding(rule, ctx, relevant[-1], trace_id=trace.get('trace_id'), verification=check)
    for row in sent:
        if vacancy_key(row)[0] == 'hh' and not ctx.trace_for(row):
            yield finding(rule, ctx, row, application_id=row.get('application_id'), verification=None)


def delivery_mismatch(ctx, rule):
    results = [e for e in ctx.events if e.get('event') == 'application_result' and vacancy_key(e)[0] == 'hh']
    for result in results:
        if result.get('outcome') != 'sent':
            continue
        traces = ctx.trace_for(result)
        confirmed = ((result.get('server_delivery') or {}).get('status') == 'confirmed'
                     or result.get('submission_status') == 'success_confirmed'
                     or any(e.get('stage') == 'HH_SERVER_DELIVERY' and e.get('status') == 'confirmed'
                            or e.get('stage') == 'POST_SUBMIT_RECONCILIATION' and e.get('step') == 'verified' and e.get('ok') is True
                            for t in traces for e in t['events']))
        if not confirmed:
            yield finding(rule, ctx, result, application_id=result.get('application_id'),
                          mismatch='sent_without_confirmation', server_delivery=result.get('server_delivery'))
    for trace in ctx.traces:
        delivery = [e for e in trace['events'] if e.get('stage') == 'HH_SERVER_DELIVERY']
        native = [e for e in trace['events'] if e.get('stage') == 'POST_SUBMIT_RECONCILIATION'
                  and e.get('step') == 'verified' and e.get('ok') is True]
        confirmation = (delivery[-1] if delivery and delivery[-1].get('status') == 'confirmed'
                        else native[-1] if native else None)
        if confirmation and not any(trace in ctx.trace_for(e) for e in results):
            yield finding(rule, ctx, trace, mismatch='confirmation_without_result', trace_id=trace.get('trace_id'), delivery=confirmation)


def chat_context_violation(ctx, rule):
    for row in ctx.chats:
        if row.get('event') != 'chat_reply_sent' or row.get('sent') is not True:
            continue
        proof = row.get('audit_evidence') or {}
        reason = []
        if proof.get('ownership_status') != 'own' or not proof.get('ownership_resume_id'):
            reason.append('ownership_unproven')
        if proof.get('ownership_resume_id') != proof.get('binding_resume_id'):
            reason.append('resume_mismatch')
        expected = proof.get('ownership_direction')
        if expected is not None and any(proof.get(k) != expected for k in ('binding_direction', 'facts_direction')):
            reason.append('direction_mismatch')
        if proof.get('binding_profile_home') != proof.get('facts_profile_home') or (
                proof.get('facts_profile_home') and proof['facts_profile_home'] != ctx.home):
            reason.append('profile_mismatch')
        # A later current snapshot cannot manufacture positive pre-send proof.
        if reason:
            yield finding(rule, ctx, row, chat_id=row.get('chat_id'), message_id=row.get('message_id'),
                          reasons=reason, audit_evidence=proof)


def llm_failure_rate(ctx, rule):
    groups = defaultdict(list)
    for row in ctx.events:
        if row.get('event') == 'llm_call':
            groups[row.get('provider') or 'unknown'].append(row)
    for provider, calls in groups.items():
        errors = sum(c.get('outcome') == 'error' for c in calls)
        if errors / len(calls) > ctx.settings.get('llm_error_threshold', .5):
            yield finding(rule, ctx, provider=provider, calls=len(calls), errors=errors,
                          threshold=ctx.settings.get('llm_error_threshold', .5))


def run_failure(ctx, rule):
    rows = [e for e in ctx.events if e.get('event') in {'stage_failed', 'unexpected_ui'}]
    rows += ctx.run.get('stage_failures') or []
    if ctx.run.get('error_kind') and not any(r.get('error_kind') == ctx.run['error_kind'] for r in rows):
        rows.append({'stage': ctx.run.get('failure_stage') or 'run',
                     'error_kind': ctx.run['error_kind']})
    seen = set()
    for row in rows:
        evidence = {k: row.get(k) for k in ('stage', 'source', 'error_kind', 'reason_code', 'fingerprint', 'failure_id')}
        signature = json.dumps(evidence, sort_keys=True)
        if signature in seen:
            continue
        seen.add(signature)
        evidence['fingerprint'] = evidence.get('fingerprint') or hashlib.sha256(signature.encode()).hexdigest()[:16]
        yield finding(rule, ctx, row, **evidence)


RULES = (
    Rule('A01', 'critical', 'Резюме, оценка или письмо взяты из другого направления.', direction_mix),
    Rule('A02', 'error', 'Вакансия нарушает условия работы направления.', work_condition_violation),
    Rule('A03', 'warning', 'Вакансия показана как подходящая без проведённой оценки.', missing_score),
    Rule('A04', 'warning', 'Работодателю отправлено несколько откликов по одному направлению.', duplicate_employer),
    Rule('A05', 'critical', 'Попытка отклика произошла после остановки отправки.', after_stop),
    Rule('A06', 'critical', 'Нет успешной проверки резюме и письма перед отправкой.', submit_unverified),
    Rule('A07', 'error', 'Запись об отправке расходится с подтверждением получения.', delivery_mismatch),
    Rule('A08', 'critical', 'Ответ в переписке отправлен без подтверждённой принадлежности или с чужими данными.', chat_context_violation),
    Rule('A09', 'info', 'Большая часть обращений к поставщику ИИ завершилась ошибкой.', llm_failure_rate),
    Rule('A10', 'info', 'Во время прогона были сбои или незнакомая страница.', run_failure),
)
