"""Attempt-bound server delivery facts. Never authorize retry or session reuse."""
from __future__ import annotations

import asyncio
import re
import time
from dataclasses import dataclass
from urllib.parse import urlsplit

from hh import observation
from hh.response_path import RESPONSE_PATH, canonical_path


@dataclass(frozen=True)
class _Topic:
    id: int
    vacancy_id: int | None
    resume_id: int | None
    created: int | None
    kind: str
    letter: bool | None


@dataclass(frozen=True)
class _Snapshot:
    attempt: object
    owner: str
    vacancy_id: int
    monotonic: float
    wall_time: float
    reason: str | None
    topics: tuple[_Topic, ...]
    hashes: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class _Delivery:
    attempt: object
    owner: str
    status: str
    reason: str
    topic_id: int | None = None
    before_ids: tuple[int, ...] | None = None
    after_ids: tuple[int, ...] | None = None
    new_ids: tuple[int, ...] | None = None
    resume_verified: bool | None = None
    has_response_letter: bool | None = None
    reads: int = 0
    post_to_confirmation_seconds: float | None = None


class _DeliveryResult(dict):
    """JSON-compatible public fields, with a non-serialized typed capability."""
    _delivery = None


def fact(result):
    """A copied/merged/handwritten public dict is never delivery evidence."""
    try:
        record = result._delivery if isinstance(result, _DeliveryResult) else None
        if (isinstance(record, _Delivery) and record.attempt._hh_server_delivery is record
                and record.owner == record.attempt.owner):
            return record
    except Exception:
        pass
    return None


def result_with_delivery(result, attempt):
    """The ONE projection from an attempt's typed fact into public result fields."""
    clean = {key: value for key, value in result.items() if key != 'server_delivery'}
    try:
        record = getattr(attempt, '_hh_server_delivery', None)
        if not isinstance(record, _Delivery) or record.attempt is not attempt or record.owner != attempt.owner:
            return clean
        projected = _DeliveryResult(clean)
        projected._delivery = record
        projected['server_delivery'] = _fields(record)
        return projected
    except Exception:
        return clean


def _fields(record):
    return {name: list(value) if isinstance(value, tuple) else value
            for name, value in vars(record).items() if name not in {'attempt', 'owner'}}


def _publish(attempt, status, reason, *, trace=None, **fields):
    fields.setdefault('reads', getattr(attempt, '_hh_delivery_reads', 0))
    record = _Delivery(attempt, attempt.owner, status, reason, **fields)
    attempt._hh_server_delivery = record
    trace = trace if trace is not None else getattr(attempt, '_hh_reconciliation_trace', None)
    if trace is not None:
        previous = getattr(trace, 'last_stage', None)
        try:
            trace.event('HH_SERVER_DELIVERY', **_fields(record))
        except Exception:
            record = _Delivery(attempt, attempt.owner, 'unknown', 'delivery_trace_exception',
                               reads=fields['reads'])
            attempt._hh_server_delivery = record
        finally:
            if previous is not None:
                trace.last_stage = previous
    return record


def server_read_reason(response, html, target_url):
    from hh.verification import _AppliedHTML
    from hh.ui import CHALLENGE_TEXT
    if response is None:
        return 'server_read_unproven'
    if response.status != 200:
        return 'http_' + str(int(response.status))
    if response.url != target_url:
        return 'response_url_changed'
    if response.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'text/html':
        return 'non_html'
    parser = _AppliedHTML()
    parser.feed(html)
    parser.close()
    text = ' '.join(parser.text).casefold()
    if (any(marker in text for marker in CHALLENGE_TEXT)
            or re.search(r'"userType"\s*:\s*"anonymous"|"luxPageName"\s*:\s*"ForbiddenPage"', html, re.I)
            or re.search(r'(?:data-qa|id|class)=[\"\'][^\"\']*(?:captcha|account-login|login-form)', html, re.I)
            or re.search(r'<form\b[^>]*action=[\"\'][^\"\']*/account/login', html, re.I)):
        return 'server_auth_or_challenge'
    return None


def record_snapshot(attempt, result, vacancy_id, phase, monotonic, wall_time, *, response, html, target_url):
    if attempt is None or not hasattr(attempt, 'repository'):
        return
    reason = None if result['found'] else result.get('reason', 'parse_failed')
    if phase == 'before':
        # begin() is synchronous: it moves preparing -> acting and then sets
        # acting=True before returning. This synchronous capture cannot race it.
        # Never replace an earlier baseline or accept a post-begin baseline.
        state = attempt.repository.get(attempt.url)
        if (attempt.acting or attempt.command_started or attempt.abandoned
                or getattr(attempt, '_hh_application_posts', [])
                or state.get('owner') != attempt.owner or state.get('status') != 'preparing'):
            reason = 'before_not_preparing'
    else:
        reason = server_read_reason(response, html, target_url) or reason
    topics = tuple(_Topic(t['id'], t['vacancyId'], t['resumeId'], t['creationTimeMillis'],
                          t['initialTopicType'], t['hasResponseLetter']) for t in result.get('topicList', []))
    record = _Snapshot(attempt, attempt.owner, int(vacancy_id), monotonic, wall_time, reason,
                       topics, tuple(result.get('resume_hashes', {}).items()))
    if phase == 'before':
        if not hasattr(attempt, '_hh_delivery_before'):
            attempt._hh_delivery_before = record
    else:
        if not hasattr(attempt, '_hh_delivery_after'):
            attempt._hh_delivery_after = []
        attempt._hh_delivery_after.append(record)


def record_snapshot_error(attempt, phase, reason):
    if attempt is None:
        return
    if phase in {'after', 'after_observation'}:
        attempt._hh_delivery_error = 'snapshot_exception'
        # Do not expose exception text (it may contain private page data).
        record = _Snapshot(attempt, attempt.owner, 0, time.monotonic(), time.time(), reason, (), ())
        if not hasattr(attempt, '_hh_delivery_after'):
            attempt._hh_delivery_after = []
        attempt._hh_delivery_after.append(record)


def capture_resume_hashes(attempt, html, vacancy_id):
    """Reuse preflight HTML, before sanitization; no extra GET or browser action."""
    try:
        if attempt is None or attempt.acting or attempt.command_started:
            return
        hashes = observation.resume_hashes(observation.initial_state(html), vacancy_id)
        attempt._hh_delivery_resume_hashes = (attempt.owner, int(vacancy_id), tuple(hashes.items()))
    except ValueError as exc:
        # A page with no state supplies no map. A broken/conflicting state is
        # an observation failure; do not silently turn it into a missing map.
        if str(exc) != 'template_missing':
            attempt._hh_delivery_error = 'resume_map_invalid'
    except Exception:
        attempt._hh_delivery_error = 'resume_map_exception'


def note_read(attempt):
    attempt._hh_delivery_reads = getattr(attempt, '_hh_delivery_reads', 0) + 1


def _post(session, attempt, resume_hash, letter):
    from hh.recovery import _request_matches
    if attempt.client is not session or attempt.repository.source != 'hh' or not attempt.acting or attempt.proven_no_action:
        return None
    monitor = getattr(attempt.page, '_hh_action_monitor', None)
    if monitor is None or monitor.session is not session or monitor.attempts.get(attempt.owner) is not attempt:
        return None
    state = attempt.repository.get(attempt.url)
    if state.get('owner') != attempt.owner or state.get('status') not in {'acting', 'uncertain', 'completed'}:
        return None
    for post in getattr(attempt, '_hh_application_posts', []):
        proof = post.get('_approval')
        if not (post.get('owned') is True and post.get('status') == 200 and isinstance(proof, dict)):
            continue
        fields = _application_fields(proof)
        target = urlsplit(post['request'].url)
        if (proof.get('owner') == attempt.owner
                and proof.get('used') is True and fields.get('resume_hash') == resume_hash
                and fields.get('letter') == letter
                and canonical_path(target.path) in {RESPONSE_PATH, RESPONSE_PATH + '/popup'}
                and post['request'].frame is attempt.page.main_frame
                and attempt.repository.key(attempt.url) == attempt.repository.key(
                    'https://hh.ru/vacancy/' + fields['vacancy_id'])
                and _request_matches(proof, post['request'])):
            return post
    return None


def _application_fields(proof):
    if proof.get('kind') == 'hh-react-popup':
        return proof.get('fields', {})
    entries = proof.get('entries', [])
    keys = [item[0] for item in entries]
    if len(keys) != len(set(keys)):
        return {}
    fields = dict(entries)
    resumes = [fields[key] for key in ('resume_id', 'resumeId', 'resumeHash', 'resume_hash', 'resume') if key in fields]
    vacancies = [fields[key] for key in ('vacancy_id', 'vacancyId') if key in fields]
    if len(resumes) != 1 or len(vacancies) != 1:
        return {}
    return {'resume_hash': resumes[0], 'vacancy_id': vacancies[0], 'letter': fields.get('letter', '')}


def _evaluate(attempt, post, after):
    before = getattr(attempt, '_hh_delivery_before', None)
    fields = {'reads': getattr(attempt, '_hh_delivery_reads', 0)}
    if getattr(attempt, '_hh_delivery_error', None):
        return 'unknown', attempt._hh_delivery_error, fields
    if not isinstance(before, _Snapshot) or before.attempt is not attempt or before.owner != attempt.owner:
        return 'unknown', 'before_missing', fields
    fields['before_ids'] = tuple(sorted({t.id for t in before.topics}))
    if before.reason:
        return 'unknown', before.reason, fields
    if before.vacancy_id != int(_application_fields(post['_approval'])['vacancy_id']):
        return 'unknown', 'before_vacancy_mismatch', fields
    started, completed = post.get('_started_monotonic'), post.get('_completed_monotonic')
    if started is None or before.monotonic >= started:
        return 'unknown', 'before_not_before_post', fields
    if after is None:
        return 'unknown', 'after_missing', fields
    if after.reason:
        return 'unknown', after.reason, fields
    if (after.attempt is not attempt or after.owner != attempt.owner
            or after.vacancy_id != before.vacancy_id):
        return 'unknown', 'snapshot_identity_changed', fields
    if completed is None or after.monotonic <= completed:
        return 'unknown', 'after_not_after_post_completion', fields
    after_ids = tuple(sorted({t.id for t in after.topics}))
    new_ids = tuple(sorted(set(after_ids) - set(fields['before_ids'])))
    fields.update(after_ids=after_ids, new_ids=new_ids)
    if not new_ids:
        return 'not_seen', 'no_new_topic_observed', fields
    if len(new_ids) != 1:
        return 'ambiguous', 'multiple_new_topics', fields
    topics = [topic for topic in after.topics if topic.id == new_ids[0]]
    if len(topics) != 1:
        return 'ambiguous', 'duplicate_topic_identity', fields
    topic = topics[0]
    fields.update(topic_id=topic.id, has_response_letter=topic.letter)
    if (topic.vacancy_id != before.vacancy_id or topic.kind != 'RESPONSE_BY_APPLICANT'
            or topic.created is None or not (before.wall_time * 1000 - 600000 <= topic.created <= after.wall_time * 1000 + 600000)):
        return 'ambiguous', 'new_topic_identity_or_time', fields
    hashes = dict(before.hashes)
    captured = getattr(attempt, '_hh_delivery_resume_hashes', None)
    if captured is not None and captured[:2] == (attempt.owner, before.vacancy_id):
        for identity, value in captured[2]:
            if identity in hashes and hashes[identity] != value:
                return 'unknown', 'resume_identity_conflict', fields
            hashes[identity] = value
    for identity, value in after.hashes:
        if identity in hashes and hashes[identity] != value:
            return 'unknown', 'resume_identity_conflict', fields
        hashes[identity] = value
    expected = _application_fields(post['_approval'])['resume_hash']
    actual = hashes.get(str(topic.resume_id))
    if hashes:
        fields['resume_verified'] = actual == expected
        if actual != expected:
            return 'mismatch', 'resume_hash_mismatch', fields
    fields['post_to_confirmation_seconds'] = after.monotonic - completed
    return 'confirmed', 'one_new_applicant_topic', fields


async def check_delivery(session, attempt, resume_hash, letter, *, trace=None):
    """Best effort, at most three server reads total; exceptions cannot change safety."""
    try:
        if getattr(attempt, '_hh_delivery_checked', False):
            return
        post = _post(session, attempt, resume_hash, letter)
        if post is None:
            _publish(attempt, 'unknown', 'owned_successful_post_unproven', trace=trace)
            return
        attempt._hh_delivery_checked = True
        reads = getattr(attempt, '_hh_delivery_after', [])
        first_read = reads[0].monotonic if reads else time.monotonic()
        # Existing verifier GETs count too. An earlier blocked/failed read is
        # terminal even if a later read would contain an attractive topic.
        for after in reads:
            status, reason, fields = _evaluate(attempt, post, after)
            if status in {'confirmed', 'unknown'}:
                _publish(attempt, status, reason, trace=trace, **fields)
                return
        while getattr(attempt, '_hh_delivery_reads', 0) < 3:
            count = getattr(attempt, '_hh_delivery_reads', 0)
            snapshots = len(getattr(attempt, '_hh_delivery_after', []))
            if count:
                await asyncio.sleep(max(0, first_read + (1.5 if count == 1 else 3.0) - time.monotonic()))
            await observation.isolated_observation(session, attempt, resume_hash, trace=trace, read_number=count + 1)
            reads = getattr(attempt, '_hh_delivery_after', [])
            if len(reads) <= snapshots:
                raise RuntimeError('delivery_snapshot_missing')
            status, reason, fields = _evaluate(attempt, post, reads[-1])
            if status in {'confirmed', 'unknown'}:
                break
        _publish(attempt, status, reason, trace=trace, **fields)
    except Exception:
        # Deliberately no exception message and no writes to action/session state.
        _publish(attempt, 'unknown', 'delivery_exception', trace=trace)
