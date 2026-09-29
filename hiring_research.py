"""Cohorts of confirmed applications; observation time is not employer action time."""
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import re

import analytics

MOSCOW = ZoneInfo('Europe/Moscow')


def _time(value):
    try:
        dt = datetime.fromisoformat(value)
        return dt.replace(tzinfo=MOSCOW) if dt.tzinfo is None else dt
    except (TypeError, ValueError):
        return None


def _key(event):
    source = event.get('source') or 'hh'
    vid = str(event.get('vacancy_id') or '')
    if vid.startswith(source + ':'):
        vid = vid[len(source) + 1:]
    if not vid:
        match = re.search(r'/vacancy/(\d+)', event.get('url', ''))
        vid = match.group(1) if match else ''
    return (source, vid) if vid else None


def _bucket(seconds):
    if seconds < 300:
        return '<5 мин'
    if seconds < 10800:
        return '5 мин–3 ч'
    if seconds < 86400:
        return '3–24 ч'
    return '≥24 ч'


def summarize(events_file, *, now=None, days=30):
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=MOSCOW)
    start = (now.astimezone(MOSCOW).replace(hour=0, minute=0, second=0, microsecond=0)
             - timedelta(days=days - 1))
    rows = []
    for event in analytics._iter_events(events_file):
        if not isinstance(event, dict):
            continue
        dt = _time(event.get('observed_at_utc') or event.get('created_at') or event.get('recorded_at_utc'))
        key = _key(event)
        if dt and key and dt <= now:
            rows.append((dt, key, event))
    rows.sort(key=lambda row: row[0])
    verifications = Counter()
    for dt, key, event in rows:
        if dt >= start and event.get('event') == 'relevance_verification':
            verifications[event.get('verdict', 'unknown')] += 1
    applications = {}
    repeats = set()
    recent_application_keys = set()
    for dt, key, event in rows:
        if event.get('event') != 'decision' or event.get('decision') != 'applied_auto' or event.get('dry_run'):
            continue
        # Old backfill cannot reliably establish application time.
        if event.get('historical') or 'backfill' in str(event.get('run_id', '')):
            continue
        if dt >= start:
            recent_application_keys.add(key)
        if key in applications:
            previous = applications[key]
            if event.get('run_id') != previous['event'].get('run_id'):
                repeats.add(key)
            continue
        applications[key] = {'at': dt, 'event': event, 'status': 'unknown', 'viewed_at': None,
                             'last_poll': None, 'rejected_at': None, 'reject_lower': None,
                             'screening': 'unknown', 'positive': False}
    for dt, key, event in rows:
        app = applications.get(key)
        if not app or dt < app['at']:
            continue
        kind = event.get('event')
        if kind == 'screening_observation':
            category = event.get('screening_type', 'unknown')
            if category != 'unknown':
                app['screening'] = category
        if kind not in {'negotiation_status', 'negotiation_observation'}:
            continue
        bucket = event.get('status_bucket', 'unknown')
        if event.get('status_detail_bucket') == 'pending_viewed' and not app['viewed_at']:
            app['viewed_at'] = dt
        if bucket == 'positive':
            app['positive'] = True
        if bucket == 'rejected' and app['rejected_at'] is None:
            app['rejected_at'] = dt
            # An unknown-status poll cannot establish that rejection had not happened.
            lower = app['last_poll'] or app['at']
            app['reject_lower'] = max(app['at'], min(lower, dt))
            app['viewed_before_rejection'] = bool(app['viewed_at'] and app['viewed_at'] < dt)
        if bucket != 'unknown':
            app['status'] = bucket
            app['last_poll'] = dt
    cohort = [app for key, app in applications.items() if app['at'] >= start and key not in repeats]
    report = {'verifications': verifications, 'days': days, 'total': len(cohort), 'excluded_repeats': len(repeats & recent_application_keys),
              'outcomes': Counter(), 'rejection_delays': Counter(), 'rejection_views': Counter(),
              'groups': {name: defaultdict(Counter) for name in ('mode', 'cover', 'resume', 'screening')},
              'recent': 0, 'start': start.isoformat()}
    for app in cohort:
        event = app['event']
        report['outcomes'][app['status']] += 1
        report['recent'] += (now - app['at']).total_seconds() < 7 * 86400
        if app['rejected_at']:
            upper = (app['rejected_at'] - app['at']).total_seconds()
            lower = (app['reject_lower'] - app['at']).total_seconds()
            label = _bucket(upper) if _bucket(lower) == _bucket(upper) else 'интервал пересекает границы'
            report['rejection_delays'][label] += 1
            report['rejection_views']['просмотр замечен' if app['viewed_before_rejection'] else 'просмотр не наблюдался'] += 1
        mode = event.get('submission_mode') or ('manual' if str(event.get('note', '')).startswith('manual_ai') else 'unknown')
        # Absence of a historical hash is not evidence of an empty letter.
        delivery = event.get('cover_letter_status', 'unknown')
        cover = {
            'confirmed': 'доставка подтверждена',
            'submitted_with_application': 'заполнено в форме отклика',
            'unconfirmed': 'доставка не подтверждена',
            'not_requested': 'письмо не запрашивалось',
        }.get(delivery, 'доставка неизвестна')
        resume = event.get('resume_variant') or event.get('resume_id') or event.get('requested_resume_id') or 'HH-резюме неизвестно'
        resume_id = event.get('resume_id') or event.get('requested_resume_id')
        if resume_id and resume_id != resume:
            resume += ' / ' + resume_id
        local_version = event.get('cover_letter_resume_sha256')
        resume += ' · локальная ' + (local_version if local_version else 'версия неизвестна')
        for dimension, label in [('mode', mode), ('cover', cover), ('resume', resume), ('screening', app['screening'])]:
            group = report['groups'][dimension][str(label)]
            group['total'] += 1
            group['positive'] += app['positive']
            group[app['status']] += 1 if app['status'] != 'positive' else 0
    return report


def render(report):
    total = report['total']
    states = report['outcomes']
    lines = [f"🔬 Отказы и конверсии · {report['days']} календарных дней (МСК)",
             f"Подтверждённых откликов: {total}",
             f"Текущий статус: положительный {states['positive']}, отказ {states['rejected']}, ожидание {states['pending']}, неизвестно {states['unknown']}.",
             f"Моложе 7 дней: {report['recent']}. Результаты ещё меняются.",
             f"Повторные отклики с неоднозначной связью исключены: {report['excluded_repeats']}.",
             '\nВремя до отказа (интервал наблюдения):']
    lines.extend(f"• {key}: {value}" for key, value in report['rejection_delays'].items())
    if not report['rejection_delays']:
        lines.append('Пока нет данных.')
    lines.extend(f"• {key}: {value}" for key, value in report['rejection_views'].items())
    checks = report.get('verifications', {})
    if checks:
        lines.append('\nТеневая проверка (решения не меняет): ' + ', '.join(f'{label}: {checks.get(key, 0)}' for key, label in [('fit', 'подходит'), ('review', 'спорно'), ('reject', 'не подходит'), ('unknown', 'ошибка/нет данных')]))
    lines.append('Отсутствие замеченного просмотра не означает, что резюме не читали. Скорость отказа не доказывает автоматический отсев.')
    yield '\n'.join(lines)
    titles = {'mode': 'Режим отправки', 'cover': 'Доставка сопроводительного', 'resume': 'Запрошенное резюме HH / версия текста для LLM', 'screening': 'Тип скрининга'}
    for dimension, title in titles.items():
        lines = [title + ' · положительный ответ / отклики']
        for label, counts in sorted(report['groups'][dimension].items(), key=lambda row: -row[1]['total'])[:12]:
            n, positive = counts['total'], counts['positive']
            display_label = label[:100]
            if dimension == 'resume':
                name, _, version = label.rpartition(' · локальная ')
                display_label = name[:70] + ' · локальная ' + (version[:12] if len(version) == 64 else version)
            lines.append(f"• {display_label}: {positive}/{n} ({positive/n:.0%}); отказов {counts['rejected']}, ждут {counts['pending']}, неизвестно {counts['unknown']}")
        if len(lines) == 1:
            lines.append('Пока нет данных.')
        lines.append('Положительный ответ = наблюдался статус приглашения/собеседования/тестового/оффера. Сравнение описательное, не доказательство причинности.')
        yield '\n'.join(lines)


def record_screening(vacancy, messages):
    """Store evidence categories, not message bodies; absence of AI is not proof of a human."""
    from chat_screening import AI_ASSISTANT_AVATAR_URLS
    category, evidence = 'unknown', 'no_verified_marker'
    for message in messages:
        if message.get('is_me'):
            continue
        if (message.get('avatar_src') or '').split('?', 1)[0] in AI_ASSISTANT_AVATAR_URLS:
            category, evidence = 'hh_system_ai', 'known_hh_avatar'
            break
        if message.get('is_ai') or message.get('is_ai_suspect'):
            category, evidence = 'suspicious_automation', 'message_heuristic'
    key = _key({'source': 'hh', 'vacancy_id': vacancy.get('id'), 'url': vacancy.get('url', '')})
    if key:
        analytics._append_event({'event': 'screening_observation', 'source': key[0], 'vacancy_id': key[1],
                                 'screening_type': category, 'evidence': evidence, 'classifier_version': 'screening-v1'})
