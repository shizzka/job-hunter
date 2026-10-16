"""Private, best-effort HH telemetry. No observation supplies action evidence."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from datetime import datetime
from html import unescape
from html.parser import HTMLParser
from urllib.parse import urlsplit

import config
from hh.response_path import RESPONSE_PATH, canonical_path


class _InitialState(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.active = self.found = self.closed = False
        self.parts = []
        self.source = ''
        self.start = None

    def feed(self, data):
        self.source += data
        super().feed(data)

    def source_index(self):
        line, column = self.getpos()
        return sum(len(part) + 1 for part in self.source.split('\n')[:line - 1]) + column

    def handle_starttag(self, tag, attrs):
        if tag == 'template' and dict(attrs).get('id') == 'HH-Lux-InitialState' and not self.found:
            self.active = self.found = True
            self.start = self.source_index() + len(self.get_starttag_text())

    def handle_endtag(self, tag):
        if tag == 'template' and self.active:
            # Slice source verbatim: JSON strings can themselves contain HTML
            # tags, comments and entities, which handle_data would alter.
            self.parts = [self.source[self.start:self.source_index()]]
            self.active = False
            self.closed = True


def _number(value):
    if type(value) is int and value >= 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit():
        return int(value)
    return None


def initial_state(html):
    parser = _InitialState()
    parser.feed(html)
    parser.close()
    if not parser.found:
        raise ValueError('template_missing')
    if not parser.closed:
        raise ValueError('template_unclosed')
    raw = ''.join(parser.parts)
    try:
        return json.loads(raw)
    except ValueError:
        try:
            return json.loads(unescape(raw))
        except ValueError:
            raise ValueError('invalid_json') from None


def resume_hashes(state, vacancy_id):
    """Only explicit numeric identity plus hash; usedResumeIds has no hash."""
    containers = [state.get('resumes', {})]
    status = state.get('applicantVacancyResponseStatuses', {}).get(str(vacancy_id), {})
    containers.append(status.get('resumes', {}))
    hashes = {}
    for container in containers:
        items = container.items() if isinstance(container, dict) else enumerate(container) if isinstance(container, list) else []
        for key, value in items:
            if not isinstance(value, dict) or not isinstance(value.get('hash'), str) or not value['hash']:
                continue
            identity = _number(value.get('id'))
            if identity is None and isinstance(container, dict):
                identity = _number(key)
            if identity is not None:
                key = str(identity)
                if key in hashes and hashes[key] != value['hash']:
                    raise ValueError('resume_identity_conflict')
                hashes[key] = value['hash']
    return hashes


def negotiations_from_html(html, vacancy_id):
    """Return an explicit absence; hashes are for in-memory comparison only."""
    try:
        try:
            state = initial_state(html)
        except ValueError as exc:
            return {'found': False, 'reason': str(exc)}
        statuses = state.get('applicantVacancyResponseStatuses', {})
        if not isinstance(statuses, dict) or str(vacancy_id) not in statuses:
            return {'found': False, 'reason': 'vacancy_missing'}
        negotiations = statuses[str(vacancy_id)].get('negotiations')
        if (not isinstance(negotiations, dict) or _number(negotiations.get('total')) is None
                or not isinstance(negotiations.get('topicList'), list)):
            return {'found': False, 'reason': 'negotiations_missing'}
        topics = []
        for item in negotiations['topicList']:
            if not isinstance(item, dict) or _number(item.get('id')) is None:
                return {'found': False, 'reason': 'invalid_topic'}
            topic = {key: _number(item.get(key)) for key in ('id', 'vacancyId', 'resumeId', 'creationTimeMillis')}
            if topic['creationTimeMillis'] is None and isinstance(item.get('creationTime'), str):
                with contextlib.suppress(ValueError, OverflowError):
                    date = datetime.fromisoformat(item['creationTime'])
                    if date.tzinfo is not None:
                        topic['creationTimeMillis'] = int(date.timestamp() * 1000)
            topic['initialTopicType'] = (item.get('initialTopicType') if item.get('initialTopicType') in {
                'RESPONSE_BY_APPLICANT', 'INVITATION', 'INVITATION_BY_EMPLOYER', 'RESPONSE', 'DISCARD'} else 'other')
            topic.update({key: item.get(key) if type(item.get(key)) is bool else None
                          for key in ('archived', 'discarded', 'hasResponseLetter')})
            topics.append(topic)
        hashes = resume_hashes(state, vacancy_id)
        return {'found': True, 'total': _number(negotiations['total']), 'topicList': topics, 'resume_hashes': hashes}
    except Exception:
        return {'found': False, 'reason': 'invalid_state'}


def event(trace, stage, **fields):
    # Do not let even trace persistence failures escape into the action path.
    with contextlib.suppress(Exception):
        if trace is not None:
            previous = getattr(trace, 'last_stage', None)
            try:
                trace.event(stage, **fields)
            finally:
                # Failure attribution must still name the original action stage.
                if previous is not None:
                    trace.last_stage = previous


def snapshot_error(attempt, phase, error_kind, *, trace=None, read_number=None):
    with contextlib.suppress(Exception):
        from hh.delivery import record_snapshot_error
        record_snapshot_error(attempt, phase, error_kind)
    event(trace if trace is not None else getattr(attempt, '_hh_reconciliation_trace', None),
          'HH_NEGOTIATIONS_SNAPSHOT', phase=phase, found=False, error_kind=error_kind,
          read_number=read_number, read_completed_monotonic=time.monotonic(), read_wall_time=time.time())


def snapshot(attempt, html, vacancy_id, resume_hash, phase, *, trace=None, read_number=None,
             response=None, target_url=None):
    try:
        trace = trace if trace is not None else getattr(attempt, '_hh_reconciliation_trace', None)
        read_time = time.monotonic()
        wall_time = time.time()
        result = negotiations_from_html(html, vacancy_id)
        from hh.delivery import record_snapshot
        try:
            record_snapshot(attempt, result, vacancy_id, phase, read_time, wall_time,
                            response=response, html=html, target_url=target_url)
        except Exception:
            if attempt is not None:
                attempt._hh_delivery_error = 'snapshot_exception'
        if phase == 'before':
            attempt._hh_negotiations_before = result
        before = getattr(attempt, '_hh_negotiations_before', {'found': False})
        before_ids = sorted({topic['id'] for topic in before.get('topicList', [])}) if before['found'] else None
        after_ids = sorted({topic['id'] for topic in result.get('topicList', [])}) if result['found'] else None
        new_ids = sorted(set(after_ids) - set(before_ids)) if before_ids is not None and after_ids is not None else None
        topics = [{**topic, 'resume_hash_matches': (
            result['resume_hashes'][str(topic['resumeId'])] == resume_hash
            if resume_hash and str(topic['resumeId']) in result['resume_hashes'] else None)}
            for topic in result.get('topicList', [])]
        event(trace, 'HH_NEGOTIATIONS_SNAPSHOT', phase=phase, read_number=read_number,
              vacancy_id=_number(vacancy_id), found=result['found'], reason=result.get('reason'),
              read_completed_monotonic=read_time, read_wall_time=wall_time,
              total=result.get('total'), topics=topics,
              before_ids=before_ids, after_ids=after_ids if phase != 'before' else None,
              new_ids=new_ids if phase != 'before' else None,
              new_topics=[topic for topic in topics if new_ids is not None and topic['id'] in new_ids])
    except Exception as exc:
        snapshot_error(attempt, phase, type(exc).__name__, trace=trace, read_number=read_number)


# Only protocol enums and numbers may survive. Unknown string values can be PII.
_CODES = {'OK', 'SUCCESS', 'ERROR', 'SPAM', 'ALREADY_APPLIED', 'alreadyApplied',
          'success', 'error', 'INCOMPLETE_RESUME', 'RESUME_NOT_FOUND', 'VACANCY_NOT_FOUND',
          'VACANCY_CLOSED', 'FORBIDDEN', 'BAD_REQUEST', 'INVALID_REQUEST', 'NOT_FOUND'}


def _code(value):
    if type(value) is int:
        return value
    return value if isinstance(value, str) and value in _CODES else 'other'


def sanitize_receipt(body, content_type):
    mime = content_type.split(';', 1)[0].strip().lower()
    result = {'content_type': mime if mime in {'application/json', 'text/json', 'text/html', 'text/plain'} else 'other',
              'size_bytes': len(body), 'body_kind': 'empty' if not body.strip() else 'not_json'}
    if not body.strip():
        return result
    try:
        data = json.loads(body)
    except (ValueError, UnicodeError):
        return result
    result.update(body_kind='json', SPAM=False, alreadyApplied=False, error_codes=[])
    if not isinstance(data, dict):
        return result
    for key in ('topic_id', 'chat_id'):
        if key in data:
            result[key] = _number(data[key])
    if 'responseStatus' in data:
        result['responseStatus'] = _code(data['responseStatus'])
    # Read only protocol containers, never recurse into arbitrary payload fields.
    def errors(value):
        if isinstance(value, list):
            for item in value:
                errors(item)
        elif isinstance(value, dict):
            for flag in ('SPAM', 'alreadyApplied'):
                result[flag] |= flag in value and value[flag] is not False and value[flag] is not None
            for key in ('code', 'error', 'type', 'value'):
                if key in value and not isinstance(value[key], (dict, list)):
                    errors(value[key])
        else:
            code = _code(value)
            result['error_codes'].append(code)
            result['SPAM'] |= code == 'SPAM'
            result['alreadyApplied'] |= code in {'alreadyApplied', 'ALREADY_APPLIED'}
    for flag in ('SPAM', 'alreadyApplied'):
        result[flag] = flag in data and data[flag] is not False and data[flag] is not None
    result['SPAM'] |= data.get('responseStatus') == 'SPAM'
    result['alreadyApplied'] |= data.get('responseStatus') in ('alreadyApplied', 'ALREADY_APPLIED')
    for key in ('errors', 'error', 'code', 'errorCode'):
        if key in data:
            errors(data[key])
    return result


def schedule_receipt(attempt, post, response):
    """Keep the existing synchronous HTTP status observer independent."""
    try:
        if (post['owned'] is not True or post.get('_receipt_scheduled')
                or response.request.method.upper() != 'POST'
                or canonical_path(urlsplit(response.url).path) not in {RESPONSE_PATH, RESPONSE_PATH + '/popup'}):
            return
        post['_receipt_scheduled'] = True
        async def read():
            fields = {'body_kind': 'unreadable', 'size_bytes': None, 'content_type': 'other'}
            try:
                fields['content_type'] = sanitize_receipt(b'', response.headers.get('content-type', ''))['content_type']
                body = await asyncio.wait_for(response.body(), timeout=5)
                fields = sanitize_receipt(body, response.headers.get('content-type', ''))
            except Exception as exc:
                fields['error_kind'] = type(exc).__name__
            event(getattr(attempt, '_hh_reconciliation_trace', None), 'HH_POST_RECEIPT',
                  status=post['status'], post_completed_monotonic=post.get('_completed_monotonic'),
                  **fields)
        task = asyncio.create_task(read())
        tasks = getattr(attempt, '_hh_receipt_tasks', None)
        if tasks is None:
            tasks = attempt._hh_receipt_tasks = []
        tasks.append(task)
    except Exception as exc:
        snapshot_error(attempt, 'post_receipt', type(exc).__name__)


async def finish_receipts(attempt):
    try:
        tasks = getattr(attempt, '_hh_receipt_tasks', [])
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
    except Exception as exc:
        snapshot_error(attempt, 'post_receipt', type(exc).__name__)


async def after_observation(session, attempt, resume_hash, *, trace=None):
    """One inert GET in an isolated cookie jar; never publish verification."""
    if getattr(attempt, '_hh_after_observation_started', False):
        return
    attempt._hh_after_observation_started = True
    await isolated_observation(session, attempt, resume_hash, trace=trace, read_number=1)


async def isolated_observation(session, attempt, resume_hash, *, trace=None, read_number):
    """One inert GET; copy cookies, never mutate the browser or its stop flags."""
    response = isolated = None
    try:
        context, page = attempt.context, attempt.page
        binding = getattr(session, '_cookie_binding', None)
        def owned():
            return (attempt.client is session and session._external_attempt is attempt
                    and session._context is context and session._page is page and page.context is context
                    and attempt.repository.source == 'hh' and not attempt.abandoned
                    and binding is not None and session._cookie_binding is binding
                    and binding.context is context and not binding.closing and not binding.revoked
                    and attempt.repository.get(attempt.url).get('owner') == attempt.owner)
        if not owned():
            snapshot_error(attempt, 'after_observation', 'ownership_unproven', trace=trace)
            return
        target = urlsplit(attempt.url)
        vacancy_id = target.path.removeprefix('/vacancy/')
        host = target.hostname or ''
        # Vacancies live on regional subdomains too (spb.hh.ru); read the same host
        # directly, because redirects are disabled for this isolated request.
        if (target.scheme != 'https' or not (host == 'hh.ru' or host.endswith('.hh.ru'))
                or target.port not in (None, 443)
                or target.username is not None or target.password is not None
                or not target.path.startswith('/vacancy/') or _number(vacancy_id) is None):
            snapshot_error(attempt, 'after_observation', 'vacancy_url_unproven', trace=trace)
            return
        url = f'https://{host}/vacancy/' + vacancy_id
        cookies = await context.cookies([url])
        if not owned():
            snapshot_error(attempt, 'after_observation', 'ownership_changed', trace=trace)
            return
        options = {'storage_state': {'cookies': cookies, 'origins': []}}
        proxy = os.environ.get('HH_PROXY', config.BROWSER_PROXY)
        if proxy:
            options['proxy'] = {'server': proxy}
        isolated = await session._pw.request.new_context(**options)
        if not owned():
            snapshot_error(attempt, 'after_observation', 'ownership_changed', trace=trace)
            return
        from hh.delivery import note_read
        note_read(attempt)
        response = await isolated.get(url, max_redirects=0, max_retries=0, timeout=15000,
                                      headers={'Cache-Control': 'no-cache'})
        if not owned():
            snapshot_error(attempt, 'after_observation', 'ownership_changed', trace=trace)
            return
        html = await response.text()
        if not owned():
            snapshot_error(attempt, 'after_observation', 'ownership_changed', trace=trace)
            return
        snapshot(attempt, html, vacancy_id, resume_hash, 'after_observation',
                 trace=trace, read_number=read_number, response=response, target_url=url)
    except Exception as exc:
        snapshot_error(attempt, 'after_observation', type(exc).__name__, trace=trace, read_number=read_number)
    finally:
        for resource in (response, isolated):
            if resource is not None:
                try:
                    await resource.dispose()
                except Exception as exc:
                    snapshot_error(attempt, 'after_observation', type(exc).__name__, trace=trace)
