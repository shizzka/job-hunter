"""F16: read-only run checks, append-only findings and owner-only notices.

Inputs are always confined to one captured profile home. No browser, LLM or
chat ownership resolver is called. The CLI saves findings but never sends.
"""
from __future__ import annotations

import argparse
import asyncio
import fcntl
from datetime import datetime, timedelta
import hashlib
import html
import json
import logging
from pathlib import Path
from urllib.parse import urlsplit

from run_audit_rules import AuditContext, RULES, timestamp, vacancy_key
from state_store.json_store import file_lock
from state_store.locking import acquire_flock
from state_store.private_journal import append_json

log = logging.getLogger('run_auditor')
FINDINGS = 'audit_findings.jsonl'
JOURNAL = 'audit_journal.jsonl'
DEFAULTS = {'send_clean': False, 'daily_summary': True, 'employer_days': 7, 'llm_error_threshold': .5}
SEVERITY = {'critical': 0, 'error': 1, 'warning': 2, 'info': 3}


def _inside(home, path):
    path = Path(path)
    if not path.is_absolute():
        path = home / path
    if not path.resolve().is_relative_to(home):
        raise ValueError('Audit input outside profile home')
    return path


def _json(home, path, default):
    try:
        with _inside(home, path).open(encoding='utf-8') as stream:
            return json.load(stream)
    except FileNotFoundError:
        return default


def _records(home, path):
    """Do not silently call a corrupt or unreadable journal a clean run."""
    try:
        stream = _inside(home, path).open(encoding='utf-8')
    except FileNotFoundError:
        return []
    result = []
    with stream:
        acquire_flock(stream.fileno(), fcntl.LOCK_SH)
        for number, line in enumerate(stream, 1):
            if not line.strip():
                continue
            row = json.loads(line)
            if not isinstance(row, dict):
                raise ValueError(f'Invalid record at line {number}')
            result.append(row)
    return result


def load_settings(home):
    raw = _json(home, 'audit_settings.json', {})
    if not isinstance(raw, dict) or set(raw) - DEFAULTS.keys():
        raise ValueError('Invalid audit settings')
    settings = {**DEFAULTS, **raw}
    if (any(type(settings[k]) is not bool for k in ('send_clean', 'daily_summary'))
            or type(settings['employer_days']) is not int or not 0 <= settings['employer_days'] <= 3650
            or type(settings['llm_error_threshold']) not in (int, float)
            or not 0 <= settings['llm_error_threshold'] <= 1):
        raise ValueError('Invalid audit settings')
    return settings


def _flatten_card(item):
    vacancy = item.get('vacancy') or {}
    return {**item, **vacancy, **(item.get('evaluation') or {}), 'event': 'manual_card',
            'vacancy_id': str(vacancy.get('id') or ''),
            'run_id': item.get('run_id') or vacancy.get('_analytics_run_id') or '',
            'evaluation_present': bool(item.get('evaluation'))}


def _load(home):
    """Read the actual saved schemas; no use of the currently selected direction."""
    import directions
    events = _records(home, 'analytics_events.jsonl')
    history = _records(home, 'run_history.jsonl')
    runs = {}
    for row in events:
        if row.get('event') in {'search_started', 'search_finished', 'manual_started', 'manual_finished'} and row.get('run_id'):
            runs[row['run_id']] = {**runs.get(row['run_id'], {}), **row}
    for row in history:
        if row.get('run_id'):
            runs[row['run_id']] = {**runs.get(row['run_id'], {}), **row}
    # A final analytics record survives a failed final history append. An old
    # incomplete checkpoint must not overwrite that explicit completion.
    for row in events:
        if row.get('event') in {'search_finished', 'manual_finished'} and row.get('run_id'):
            runs[row['run_id']] = {**runs.get(row['run_id'], {}), **row,
                                  'status': row.get('status') or 'finished',
                                  'finished_at': row.get('finished_at') or row.get('created_at')}
    # Old manual runs may have receipts but predate lifecycle instrumentation.
    for row in events:
        if row.get('run_id') and not row.get('historical') and row.get('mode') != 'historical':
            runs.setdefault(row['run_id'], {'run_id': row['run_id'], 'created_at': row.get('created_at'),
                                            'direction': row.get('direction')})
    queue = _json(home, 'manual_apply_queue.json', {'items': {}})
    if not isinstance(queue, dict) or not isinstance(queue.get('items'), dict):
        raise ValueError('Invalid manual queue')
    cards = [_flatten_card(v) for v in queue['items'].values()]
    chats = _records(home, 'chat_journal.jsonl')
    proven_sends = {(c.get('run_id'), c.get('chat_id'), c.get('message_id')) for c in chats
                    if c.get('event') == 'chat_reply_sent' and c.get('sent') is True}
    threads = {c.get('snapshot_id'): c for c in chats if c.get('event') == 'chat_thread_snapshot'}
    for row in chats:
        # Older journals recorded successful replies as a chat_decision reason.
        # Preserve missing pre-send proof as unknown, never resolve it live.
        if (row.get('event') == 'chat_decision' and row.get('reason') == 'sent'
                and (row.get('run_id'), row.get('chat_id'), row.get('message_id')) not in proven_sends):
            row.update(event='chat_reply_sent', sent=True,
                       vacancy=threads.get(row.get('snapshot_ref'), {}).get('vacancy') or {})
        if (row.get('event') == 'chat_reply_sent' and row.get('sent') is True and row.get('run_id')
                and not (row.get('audit_evidence') or {}).get('analytics_run_id')):
            runs.setdefault(row['run_id'], {'run_id': row['run_id'], 'kind': 'chat',
                'created_at': row.get('observed_at'), 'finished_at': row.get('observed_at')})
    # State is supplementary, never proof of positive ownership at send time.
    state = _json(home, 'chat_responder_state.json', {})
    if not isinstance(state, dict):
        raise ValueError('Invalid chat state')
    snapshots = _records(home, 'negotiation_snapshots.jsonl')
    definitions = {d['name']: d for d in directions.list_directions(home)}
    return runs, events, cards, chats, snapshots, definitions


def _trace_belongs_to(home, path, run_id):
    """Discover ownership without validating unrelated retained artifacts.

    Current traces carry ownership in their first row. Legacy traces may only
    acquire it at APPLICATION_CONTEXT; tolerate bad rows during discovery, then
    read a matching trace strictly (including those bad rows).
    """
    with _inside(home, path).open(encoding='utf-8') as stream:
        for line in stream:
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict) and row.get('run_id'):
                return row['run_id'] == run_id
    return False


def _traces(home, run, events):
    refs = {}
    for row in events:
        if row.get('event') == 'debug_artifact' and row.get('artifact_type') == 'apply_trace' and row.get('artifact_ref'):
            refs[str(_inside(home, row['artifact_ref']))] = row
    # New traces carry a run_id themselves, even if debug_artifact append failed.
    for path in _inside(home, 'traces').glob('*/*/trace.jsonl'):
        path = _inside(home, path)
        if str(path) not in refs:
            if not _trace_belongs_to(home, path, run['run_id']):
                continue
            records = _records(home, path)
            context = next((r for r in records if r.get('run_id') == run['run_id']), None)
            if context:
                first = records[0] if records else {}
                refs[str(path)] = {**first, **context}
    traces = []
    for ref, row in refs.items():
        records = _records(home, ref)
        if not records:
            raise ValueError('Referenced apply trace unavailable')
        context = next((e for e in records if e.get('stage') == 'APPLICATION_CONTEXT'), {})
        # No summary parsing: the structured stages are authoritative. Its local
        # summary.txt is supplied as a human-readable detail reference.
        traces.append({**row, **context, 'events': records,
                       'summary_ref': str(Path(ref).with_name('summary.txt')),
                       'vacancy_id': row.get('vacancy_id') or records[0].get('vacancy_id'),
                       'source': row.get('source') or 'hh'})
    return traces


def _context(home, run, events, cards, chats, snapshots, definitions, settings):
    selected = [e for e in events if e.get('run_id') == run['run_id'] and not e.get('historical')]
    rows = []
    for card in cards:
        # Legacy cards have no run_id; an unambiguous run/vacancy decision is
        # the only allowed recovery, never the active direction or card age.
        if card.get('run_id') == run['run_id'] or not card.get('run_id') and any(
                vacancy_key(e) == vacancy_key(card) and e.get('event') == 'decision' for e in selected):
            rows.append(card)
    owned_chats = [c for c in chats if (c.get('audit_evidence') or {}).get('analytics_run_id') == run['run_id']
                   or c.get('run_id') == run['run_id']]
    for c in owned_chats:
        vacancy = c.get('vacancy') or {}
        c.update(vacancy_id=str(vacancy.get('id') or c.get('vacancy_id') or ''),
                 title=vacancy.get('title') or c.get('title'), company=vacancy.get('company') or c.get('company'),
                 url=vacancy.get('url') or c.get('url'))
    # Frozen start-of-run variants keep historical attribution stable on refresh.
    definitions = dict(definitions)
    for e in selected:
        snapshot = e.get('audit_direction')
        if snapshot and snapshot.get('name'):
            definitions[snapshot['name']] = snapshot
    if (snapshot := run.get('audit_direction')) and snapshot.get('name'):
        definitions[snapshot['name']] = snapshot
    return AuditContext(run, selected, events, definitions, _traces(home, run, selected), rows,
                        owned_chats, snapshots, settings, str(home))


def _fingerprint(row):
    # Stable rule/run/vacancy + exact contradictory field values. Neither audit
    # time nor presentation labels are identities.
    payload = {k: row.get(k) for k in ('rule_id', 'run_id', 'direction', 'source', 'vacancy_id', 'evidence')}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def check_run(ctx):
    findings, errors = {}, []
    for rule in RULES:
        try:
            for row in rule.check(ctx, rule):
                row['finding_id'] = _fingerprint(row)
                findings[row['finding_id']] = row
        except Exception as exc:
            log.warning('Audit rule %s failed: error_kind=%s', rule.id, type(exc).__name__)
            errors.append({'rule_id': rule.id, 'error_kind': type(exc).__name__})
    return {'run_id': ctx.run['run_id'], 'run': ctx.run, 'findings': list(findings.values()), 'errors': errors}


def _save(home, report):
    _inside(home, FINDINGS)
    _inside(home, JOURNAL)
    with file_lock(_inside(home, 'audit_transaction')):
        saved = {r.get('finding_id') for r in _records(home, FINDINGS)}
        for row in report['findings']:
            if row['finding_id'] not in saved:
                append_json(home / FINDINGS, {**row, 'audited_at': datetime.now().astimezone().isoformat()})
                saved.add(row['finding_id'])
        record = {'event': 'audit_completed', 'run_id': report['run_id'],
                  'finished_at': report['run'].get('finished_at') or report['run'].get('created_at'),
                  'findings': len(report['findings']), 'errors': report['errors']}
        previous = next((r for r in reversed(_records(home, JOURNAL))
                         if r.get('event') == 'audit_completed' and r.get('run_id') == report['run_id']), None)
        if record != previous:
            append_json(home / JOURNAL, record)


def audit_runs(*, home, profile='default', run_id=None, since=None, persist=True):
    home = Path(home).resolve()
    settings = load_settings(home)
    runs, events, cards, chats, snapshots, definitions = _load(home)
    cutoff = timestamp(since) if since else None
    if since and not cutoff:
        raise ValueError('Invalid ISO date')
    selected = [r for r in runs.values() if (not run_id or r['run_id'] == run_id) and (
        not cutoff or (at := timestamp(r.get('finished_at') or r.get('created_at') or r.get('started_at'))) and at >= cutoff)]
    if not run_id and not since:
        selected = selected[-1:]
    if run_id and not selected:
        raise ValueError('Run not found')
    reports = []
    for run in selected:
        report = check_run(_context(home, run, events, cards, chats, snapshots, definitions, settings))
        report.update(profile=profile, settings=settings, home=str(home))
        if run.get('status') == 'incomplete':
            report['errors'].append({'error_kind': 'IncompleteRun'})
        if run.get('kind') != 'chat' and not any(e.get('run_id') == run['run_id'] for e in events):
            report['errors'].append({'error_kind': 'RunEventsUnavailable'})
        if persist:
            _save(home, report)
        reports.append(report)
    return reports


def _short(value, limit=70):
    text = ' '.join(str(value or '').split())
    return text[:limit] + ('…' if len(text) > limit else '')


def format_report(report, *, full=False, findings=None):
    run = report['run']
    profile = report.get('profile', 'default')
    label = profile + '/' + (run.get('direction_title') or run.get('direction') or 'направление не указано')
    if profile.endswith('-dev'):
        label += ' [dev]'
    at = timestamp(run.get('finished_at') or run.get('created_at'))
    when = at.strftime('%d.%m %H:%M') if at else 'время не указано'
    rows = report['findings'] if findings is None else findings
    errors = report.get('errors') or []
    prefix = f'Прогон {when}, {label}'
    if not rows and not errors:
        return '✅ ' + html.escape(prefix) + ': нарушений нет'
    lines = [('⚠️ ' if errors else '🔎 ') + html.escape(prefix) + f': замечаний {len(rows)}']
    if errors:
        lines.append('⚠️ Аудитор не смог проверить: часть данных недоступна.')
    ordered = sorted(rows, key=lambda r: (SEVERITY[r['severity']], r['rule_id'], r['vacancy_id']))
    icons = {'critical': '🛑', 'error': '❗', 'warning': '⚠️', 'info': 'ℹ️'}
    # Severity order groups findings without extra headings consuming ten lines.
    for row in ordered[:max(0, 8 - len(lines))] if not full else ordered:
        title = _short(row.get('title') or 'Проверка прогона')
        company = _short(row.get('company'), 45)
        caption = html.escape(title + (' / ' + company if company else ''))
        url = str(row.get('url') or '')
        if not url and row.get('source') == 'hh' and str(row.get('vacancy_id') or '').isdigit():
            url = 'https://hh.ru/vacancy/' + row['vacancy_id']
        if urlsplit(url).scheme in {'https', 'http'} and urlsplit(url).netloc:
            caption = f'<a href="{html.escape(url, quote=True)}">{caption}</a>'
        lines.append(f"{icons[row['severity']]} {caption}: {html.escape(row['message'])}")
        if full:
            lines.append(html.escape(json.dumps({'rule_id': row['rule_id'], 'finding_id': row['finding_id'],
                'run_id': row['run_id'], 'vacancy_id': row['vacancy_id'], 'evidence': row['evidence']}, ensure_ascii=False, sort_keys=True)))
    if not full and len(ordered) > max(0, 8 - (2 if errors else 1)):
        lines.append('Остальные замечания — в полном отчёте.')
    # Profile/run strings go into an HTML code block, not an executable command.
    lines.append('Подробнее: <code>./run.sh --profile ' + html.escape(profile) + ' audit --run ' + html.escape(report['run_id']) + '</code>')
    return '\n'.join(lines)


def _claim_notice(home, key, finding_ids, now):
    """Short disk-only transaction. Never hold a flock across an await."""
    with file_lock(_inside(home, 'audit_transaction')):
        rows = _records(home, JOURNAL)
        delivered = {i for r in rows if r.get('event') == 'notice_delivered' for i in r.get('finding_ids', [])}
        if finding_ids:
            latest = {r['notice_key']: r for r in rows if r.get('notice_key')}
            active = {i for r in latest.values() if r.get('event') == 'notice_claim'
                      and (at := timestamp(r.get('at'))) and now - at < timedelta(minutes=5)
                      for i in r.get('finding_ids', [])}
            available = sorted(set(finding_ids) - delivered - active)
            if not available:
                return None
            if available != list(finding_ids):
                key += ':' + hashlib.sha256('|'.join(available).encode()).hexdigest()
            finding_ids = available
        prior = next((r for r in reversed(rows) if r.get('notice_key') == key), None)
        if prior and (prior.get('event') == 'notice_delivered' or prior.get('event') == 'notice_claim'
                      and (at := timestamp(prior.get('at'))) and now - at < timedelta(minutes=5)):
            return None
        append_json(home / JOURNAL, {'event': 'notice_claim', 'notice_key': key,
                    'finding_ids': finding_ids, 'at': now.isoformat()})
        return key, list(finding_ids)


async def _notice(home, key, text, finding_ids=(), *, target=None, now=None):
    import notifier
    now = now or datetime.now().astimezone()
    claim = _claim_notice(home, key, finding_ids, now)
    if claim is None:
        return False
    key, finding_ids = claim
    try:
        content = text(finding_ids) if callable(text) else text
        ok = await notifier.notify_audit_owner(content, target=target)
    except Exception as exc:
        log.warning('Audit notice failed: error_kind=%s', type(exc).__name__)
        ok = False
    append_json(_inside(home, JOURNAL), {'event': 'notice_delivered' if ok else 'notice_failed',
        'notice_key': key, 'finding_ids': list(finding_ids), 'at': now.isoformat()})
    return ok


async def audit_daily_summary(*, home, profile, target=None, now=None):
    """Previous complete local day; delivered once on the next scheduled tick/run."""
    home = Path(home).resolve()
    if not load_settings(home)['daily_summary']:
        return
    now = now or datetime.now().astimezone()
    day = now.date() - timedelta(days=1)
    latest = {}
    for row in _records(home, JOURNAL):
        if row.get('event') == 'audit_completed':
            latest[row['run_id']] = row
    runs = [r for r in latest.values() if (at := timestamp(r.get('finished_at'))) and at.date() == day]
    history = {}
    for row in _records(home, 'run_history.jsonl'):
        if row.get('run_id'):
            history[row['run_id']] = row
    expected = {r['run_id'] for r in history.values() if
        (at := timestamp(r.get('finished_at') or r.get('created_at') or r.get('started_at'))) and at.date() == day}
    if (runs and expected <= {r['run_id'] for r in runs}
            and all(not r['findings'] and not r['errors'] for r in runs)):
        label = profile + (' [dev]' if profile.endswith('-dev') else '')
        text = f'✅ {html.escape(label)}: за сутки {day:%d.%m} прогонов {len(runs)}, нарушений 0'
        await _notice(home, 'daily:' + day.isoformat(), text, target=target, now=now)


async def report_audit_error(run_id, *, home=None, profile=None, target=None):
    """Last-resort owner notice. A broken disk must not prevent the notice."""
    import config
    import notifier
    home = Path(home or config.JOB_HUNTER_HOME).resolve()
    profile = profile or 'default'
    target = notifier.capture_audit_owner_target() if target is None else target
    text = f'⚠️ Прогон {html.escape(str(run_id))}, {html.escape(profile)}' + (' [dev]' if profile.endswith('-dev') else '') + ': аудитор не смог проверить.'
    try:
        await _notice(home, 'audit_error:' + str(run_id), text, target=target)
    except Exception:
        await notifier.notify_audit_owner(text, target=target)


def _enqueue(kind, *, run_id=None, home, profile, target=None):
    import audit_worker
    import notifier
    return audit_worker.submit({'kind': kind, 'run_id': run_id, 'destination': {
        'home': str(home), 'profile': profile,
        'target': notifier.capture_audit_owner_target() if target is None else target}})


def enqueue_completed_run(run_id, **destination):
    return _enqueue('run', run_id=run_id, **destination)


def enqueue_daily_summary(**destination):
    return _enqueue('daily', **destination)


async def audit_completed_run(run_id, *, home=None, profile=None, target=None, offload=True):
    import config
    import notifier
    home = Path(home or config.JOB_HUNTER_HOME).resolve()
    profile = profile or 'default'
    target = notifier.capture_audit_owner_target() if target is None else target
    try:
        reports = (await asyncio.to_thread(audit_runs, home=home, profile=profile, run_id=run_id)
                   if offload else audit_runs(home=home, profile=profile, run_id=run_id))
        for report in reports:
            delivered = {i for r in _records(home, JOURNAL) if r.get('event') == 'notice_delivered' for i in r.get('finding_ids', [])}
            pending = [r for r in report['findings'] if r['finding_id'] not in delivered]
            if pending or report['errors'] or report['settings']['send_clean']:
                ids = sorted(r['finding_id'] for r in pending)
                suffix = hashlib.sha256('|'.join(ids).encode()).hexdigest() if ids else 'error' if report['errors'] else 'clean'
                render = lambda claimed: format_report(report, findings=[r for r in pending if r['finding_id'] in claimed])
                await _notice(home, run_id + ':' + suffix, render, ids, target=target)
        await audit_daily_summary(home=home, profile=profile, target=target)
    except Exception as exc:
        log.warning('Run audit failed: error_kind=%s', type(exc).__name__)
        try:
            await report_audit_error(run_id, home=home, profile=profile, target=target)
        except Exception as report_exc:
            log.warning('Audit error notice failed: error_kind=%s', type(report_exc).__name__)


async def main(argv=None):
    import config
    import profile as profile_mod
    parser = argparse.ArgumentParser(description='Проверка сохранённых прогонов')
    selection = parser.add_mutually_exclusive_group()
    selection.add_argument('--run')
    selection.add_argument('--since')
    args = parser.parse_args(argv)
    try:
        profile = getattr(getattr(profile_mod, '_active_profile', None), 'name', 'default')
        reports = audit_runs(home=config.JOB_HUNTER_HOME, profile=profile, run_id=args.run, since=args.since)
        if not reports:
            print('Прогонов за указанный период нет.')
        for report in reports:
            # CLI is plain text; HTML is only for owner delivery.
            import re
            print(html.unescape(re.sub('<[^>]+>', '', format_report(report, full=True))))
        return 1 if any(report['errors'] for report in reports) else 0
    except Exception as exc:
        log.warning('Audit CLI failed: error_kind=%s', type(exc).__name__)
        print('⚠️ Аудитор не смог проверить: данные недоступны или указан неверный прогон/период.')
        return 1
