"""Private request signatures only; never an input to HH action decisions."""
import contextlib
from datetime import datetime, timezone
from pathlib import Path
import re
import time
from urllib.parse import parse_qsl, urlsplit

from state_store.private_journal import append_json


JOURNAL_NAME = 'hh_unknown_requests.jsonl'
_MEDIA_TYPE = re.compile(r'[a-z0-9!#$&^_.+-]+/[a-z0-9!#$&^_.+-]+')


def journal_path(session):
    """Capture this browser's profile destination, without a global fallback."""
    with contextlib.suppress(Exception):
        return Path(session._cookie_paths.state_dir).absolute() / JOURNAL_NAME
    return None


def record_unknown_request(monitor, scope, request, reason):
    if request is None:
        return
    context = monitor.context if scope == 'context' else monitor.page.context
    watch = getattr(context, '_hh_action_watch', None)
    if watch is None or watch.session is not monitor.session or watch.request_journal_path is None:
        return
    session = monitor.session
    # A retained context can emit after a newer context acquired the session.
    # Attribute its signature/timing to its own retained HH attempt.
    attempt = next((item for item in (getattr(session, '_external_attempt', None), watch.last_attempt)
                    if item is not None and item.client is session and item.context is context
                    and item.repository.source == 'hh'), None)
    if attempt is None and getattr(session, '_manual_apply_guard', None) is None:
        return

    now = time.monotonic()
    record = {'observed_at': datetime.now(timezone.utc).isoformat(),
              'scope': scope, 'observation': monitor.observations, 'reason': reason,
              'host': None, 'path': None, 'method': None, 'content_type': None,
              'query_fields': [], 'body_length': None, 'since_owned_post_ms': None}
    # Extract each component separately: one unreadable property must not hide
    # the available signature. No URL, header map, body or exception is stored.
    with contextlib.suppress(Exception):
        parsed = urlsplit(request.url)
        record['host'] = parsed.hostname
        record['path'] = parsed.path
        record['query_fields'] = sorted({key for key, _ in parse_qsl(parsed.query, keep_blank_values=True)})
    with contextlib.suppress(Exception):
        record['method'] = request.method.upper()
    with contextlib.suppress(Exception):
        media_type = request.headers.get('content-type', '').split(';', 1)[0].strip().lower()
        record['content_type'] = media_type if _MEDIA_TYPE.fullmatch(media_type) else None
    with contextlib.suppress(Exception):
        body = request.post_data_buffer
        record['body_length'] = len(body) if body is not None else 0
    if record['body_length'] is None:
        with contextlib.suppress(Exception):
            body = request.post_data
            record['body_length'] = len(body.encode('utf-8')) if body is not None else 0
    for post in getattr(attempt, '_hh_application_posts', []):
        if post.get('owned') is True and isinstance(post.get('_started_monotonic'), (int, float)):
            record['since_owned_post_ms'] = round((now - post['_started_monotonic']) * 1000, 3)
            break
    append_json(watch.request_journal_path, record)
