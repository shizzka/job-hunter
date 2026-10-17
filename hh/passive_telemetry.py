"""Exact passive POST signatures proven by the 2026-10-17 private journal.

No URL values or body contents are retained. A partial/unreadable signature
does not authorize an exception to the ordinary action monitor.
"""
import os
import re
from urllib.parse import parse_qsl, urlsplit


ANATSKYTICS_FIELDS = frozenset({
    'active', 'activeTab', 'archived', 'disabled', 'elementName', 'employerId',
    'event', 'fpAppVersion', 'hhid', 'hhtmFrom', 'hhtmFromLabel', 'hhtmSource',
    'hhtmSourceLabel', 'isBrandingPreview', 'needFix', 'not_in_experiment',
    'originalRequestId', 'position', 'requestId', 'source', 'supernova', 'type', 'userType',
    'vacancyId', 'waiting',
})
SENTRY_FIELDS = frozenset({'sentry_client', 'sentry_key', 'sentry_version'})
CPA_FIELDS = frozenset({'ref', 'rnd', 'sh', 'sw', 'uid', 'url'})
TARGETADS_FIELDS = frozenset({
    'cid', 'dl', 'dt', 'edttm', 'pid', 'ref', 'sid', 'ss', 'ul', 'wh', 'ww', 'yandexuid',
})
ANATSKYTICS_MAX_BODY = 256
ANATSKYTICS_JSON_MAX_BODY = 4 * 1024
CPA_MAX_BODY = 256
SENTRY_MAX_BODY = 64 * 1024
TARGETADS_MAX_BODY = 4 * 1024
_HH_REGION = re.compile(r'[a-z0-9]+(?:-[a-z0-9]+)*\.hh\.ru', re.ASCII)
_ENVELOPE = re.compile(r'/api/[0-9]+/envelope/', re.ASCII)
_RESUME_HASH = re.compile(r'[0-9a-f]{38}', re.ASCII)


def enabled():
    # Read at observation time so an explicit rollback restores the old rule.
    return os.getenv('HH_PASSIVE_TELEMETRY_ENABLED', '1').strip().lower() in {'1', 'true', 'yes', 'on'}


def _passive_read(request):
    """Exact read seen right after a confirmed delivery: the applicant resume page.

    Carries only the resume hash, no vacancy field, so it cannot submit a response.
    """
    target = urlsplit(request.url)
    if (target.scheme != 'https' or target.username is not None or target.password is not None
            or target.port not in {None, 443} or target.fragment):
        return None
    host = target.hostname or ''
    if target.path != '/applicant/resume' or not (host == 'hh.ru' or _HH_REGION.fullmatch(host)):
        return None
    pairs = parse_qsl(target.query, keep_blank_values=True, encoding='utf-8', errors='strict')
    if len(pairs) != 1 or pairs[0][0] != 'resume' or not _RESUME_HASH.fullmatch(pairs[0][1]):
        return None
    body = getattr(request, 'post_data_buffer', None)
    if body is None:
        body = request.post_data
    return 'passive_read' if not body else None


def classify_request(request):
    """Return passive_telemetry only on a complete, bounded wire signature."""
    if not enabled():
        return None
    try:
        if request.method.upper() == 'GET':
            return _passive_read(request)
        if request.method.upper() != 'POST':
            return None
        target = urlsplit(request.url)
        # Do not normalize paths, accept credentials, arbitrary ports or nested
        # subdomains. Only the observed HTTPS origins have been proven.
        if (target.scheme != 'https' or target.username is not None
                or target.password is not None or target.port not in {None, 443}
                or target.fragment):
            return None
        host, path = target.hostname, target.path
        if path == '/anatskytics' and (host == 'hh.ru' or _HH_REGION.fullmatch(host or '')):
            fields, media_type, limit = ANATSKYTICS_FIELDS, 'application/x-www-form-urlencoded', ANATSKYTICS_MAX_BODY
            # Seen live 2026-10-17: the same beacon batched as JSON (~1.3 KB) after a delivery.
            if request.headers.get('content-type', '').split(';', 1)[0].strip().lower() == 'application/json':
                media_type, limit = 'application/json', ANATSKYTICS_JSON_MAX_BODY
        elif host == 'sentry.hh.ru' and _ENVELOPE.fullmatch(path):
            fields, media_type, limit = SENTRY_FIELDS, 'text/plain', SENTRY_MAX_BODY
        elif host == 'cpa.hh.ru' and path == '/api/tracker/hit':
            fields, media_type, limit = CPA_FIELDS, 'text/plain', CPA_MAX_BODY
        elif host == 'eye.targetads.io' and path == '/web/collect':
            fields, media_type, limit = TARGETADS_FIELDS, 'text/plain', TARGETADS_MAX_BODY
        else:
            return None
        headers = request.headers
        if (headers.get('content-type', '').split(';', 1)[0].strip().lower() != media_type
                or headers.get('content-encoding', 'identity').strip().lower() != 'identity'):
            return None
        query_fields = {key for key, _ in parse_qsl(target.query, keep_blank_values=True,
                                                   encoding='utf-8', errors='strict')}
        if not query_fields <= fields:
            return None
        # Playwright exposes wire bytes. Text-only test doubles can use UTF-8;
        # exceptions/unavailable data fail closed instead of guessing a length.
        body = getattr(request, 'post_data_buffer', None)
        if body is not None:
            if not isinstance(body, bytes):
                return None
            length = len(body)
        else:
            body = request.post_data
            length = 0 if body is None else len(body.encode('utf-8'))
        return 'passive_telemetry' if length <= limit else None
    except Exception:
        return None
