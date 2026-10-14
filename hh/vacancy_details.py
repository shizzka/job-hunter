"""Render a read-only vacancy preflight without running site integrations.

The live Page/URL/DOM are retained for trace metadata. This document stays
passive until the next ordinary navigation; no queued site JS is re-enabled.
"""
import asyncio
import contextlib
import re
import secrets
from contextlib import asynccontextmanager
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

from hh.ui import HHUnexpectedUI
from hh.recovery import _mutating_request
from hh.resume_refresh import _trusted_url

DETAILS_CSP = ("script-src 'none'; connect-src 'none'; worker-src 'none'; "
               "frame-src 'none'; object-src 'none'; form-action 'none'; base-uri 'none'")
_MTS_SRCDOC = re.compile(r"""\s*<script>\s*
function\s+stream_land\(op\)\s*\{\s*
var\s+sfmb\s*=\s*document\.createElement\('iframe'\);\s*
sfmb\.style\s*=\s*'position:absolute;top:-2000px;left:0px;frameborder:0px';\s*
sfmb\.src\s*=\s*'https://sm\.rtb\.mts\.ru/s\?p='\s*\+\s*op\s*\+\s*'&random='\s*\+\s*Math\.random\(\);\s*
document\.head\.appendChild\(sfmb\);\s*\}\s*
stream_land\('[A-Za-z0-9%+/_=-]+'\);\s*</script>\s*""", re.X)


def safe_navigation_url(value):
    """Keep the exact vacancy path and origin, without query/fragment or credentials."""
    try:
        parsed = urlsplit(value)
        _trusted_url(value, parsed.path.rstrip('/'))
        if not re.fullmatch(r'/vacancy/[0-9]+/?', parsed.path):
            return ''
        return 'https://' + parsed.hostname + parsed.path
    except (TypeError, ValueError):
        return ''


class HHVacancyNavigationError(ValueError):
    """Read-only document transport failed before an actionable page was loaded."""
    def __init__(self, requested_url, *, response_url='', status=None, content_type='',
                 redirect_chain=(), error_type='ValueError', error_code='unexpected_response',
                 message='Vacancy details navigation failed', method=None):
        safe_response = safe_navigation_url(response_url)
        self.metadata = {
            'stage': 'VACANCY_NAVIGATION', 'reason_code': 'vacancy_navigation_failed',
            'requested_url': safe_navigation_url(requested_url), 'response_url': safe_response,
            'response_origin': ('https://' + urlsplit(safe_response).hostname) if safe_response else '',
            'status': status, 'content_type': content_type,
            'redirect_chain': list(redirect_chain),
            'error_type': error_type, 'error_code': error_code,
        }
        if method is not None:
            self.metadata['method'] = method
        super().__init__(message)


class HHVacancyUnavailable(RuntimeError):
    """Proven vacancy-specific GET denial before matching or apply intent."""
    reason = 'vacancy_forbidden'

    def __init__(self, metadata):
        self.metadata = metadata
        super().__init__(self.reason)


async def classify_vacancy_forbidden(session, error, vacancy_url):
    """Only an owned, action-free preflight with fresh auth proof can continue."""
    from hh.recovery import AUTH_URL, _auth_cookies
    from hh.resume_refresh import get_resume_ids_readonly

    metadata = error.metadata
    if metadata.get('status') != 403:
        return False

    def stop(reason, *, uncertain=False):
        session._hh_recovery_stop_reason = (getattr(session, '_hh_recovery_stop_reason', '') or reason)
        session._hh_recovery_uncertain = bool(getattr(session, '_hh_recovery_uncertain', False) or uncertain)
        session._raise_stopped_ui('vacancy_forbidden_preflight')

    expected = safe_navigation_url(vacancy_url)
    path = urlsplit(expected).path
    try:
        if (not expected or metadata.get('method') != 'GET'
                or metadata.get('requested_url') != expected
                or metadata.get('content_type') != 'text/html'
                or _mutating_request(type('Navigation', (), {'method': 'GET', 'url': vacancy_url})())):
            raise ValueError('Unproven vacancy GET')
        _trusted_url(metadata.get('response_url', ''), path.rstrip('/'))
    except (TypeError, ValueError):
        stop('vacancy_forbidden_scope_unproven')

    page, context, guard = session._page, session._context, session._ui_guard
    request_context = getattr(context, 'request', None)
    watch = getattr(context, '_hh_action_watch', None)
    monitor = getattr(page, '_hh_action_monitor', None)
    document_epoch = getattr(page, '_hh_passive_document_epoch', None)
    generation = document_epoch.get('value') if isinstance(document_epoch, dict) else None

    def prove():
        session._raise_stopped_ui('vacancy_forbidden_preflight')
        if (page is None or context is None or guard is None or request_context is None
                or session._page is not page or session._context is not context or session._ui_guard is not guard
                or context.request is not request_context or page.context is not context
                or getattr(context, '_hh_action_watch', None) is not watch
                or getattr(page, '_hh_action_monitor', None) is not monitor
                or watch is None or monitor is None or watch.session is not session
                or watch.context is not context or monitor.session is not session or monitor.page is not page
                or watch.pages.get(page) is not monitor
                or getattr(session, '_hh_blocked_worker_context', None) is not context
                or getattr(context, '_hh_workers_owner', None) is not session):
            stop('vacancy_forbidden_ownership_unproven', uncertain=True)
        retained = [monitor.last_attempt, watch.last_attempt, *monitor.attempts.values()]
        if any(attempt is not None and any(getattr(attempt, key, False)
                for key in ('uncertain', 'acting', 'command_started')) for attempt in retained):
            stop('possible_external_action', uncertain=True)
        if (watch.unknown or monitor.unknown or monitor.owners
                or getattr(session, '_hh_recovery_uncertain', False)):
            stop('possible_external_action', uncertain=True)
        if (getattr(session, '_external_attempt', None) is not None
                or watch.application_post_sequence or getattr(page, '_jh_boundary_fences', {})
                or getattr(session, '_submit_boundary_id', None)):
            stop('vacancy_forbidden_action_boundary_unproven')
        if (page.is_closed() or len(page.frames) != 1 or page.workers or context.service_workers
                or len(context.pages) != 1 or context.pages[0] is not page):
            stop('vacancy_forbidden_browser_surface_unproven', uncertain=True)
        binding = getattr(session, '_cookie_binding', None)
        if binding is not None and (binding.context is not context or binding.revoked or binding.closing):
            stop('cookie_session_ownership_unproven', uncertain=True)

    async def prove_document():
        def same_document():
            if (not isinstance(document_epoch, dict) or document_epoch.get('owner') is not session
                    or getattr(page, '_hh_passive_document_epoch', None) is not document_epoch
                    or document_epoch.get('value') != generation):
                stop('vacancy_forbidden_document_unproven')
        prove()
        same_document()
        token = getattr(error, '_passive_document_token', '')
        try:
            if (not token or safe_navigation_url(page.url) != expected
                    or page.main_frame.url != page.url
                    or await page.locator('meta[data-jh-vacancy-forbidden]').get_attribute('content', timeout=2000) != token):
                raise ValueError('Unproven inactive vacancy document')
        except Exception:
            stop('vacancy_forbidden_document_unproven')
        prove()
        same_document()
        if safe_navigation_url(page.url) != expected or page.main_frame.url != page.url:
            stop('vacancy_forbidden_document_unproven')

    await prove_document()
    ids = set(getattr(session, '_hh_consecutive_forbidden_vacancies', ()))
    ids.add(path.rstrip('/').split('/')[-1])
    if len(ids) >= 3:
        stop('repeated_vacancy_forbidden')
    try:
        before = _auth_cookies(await context.cookies([AUTH_URL]))
        await prove_document()
        await get_resume_ids_readonly(session)
        await prove_document()
        after = _auth_cookies(await context.cookies([AUTH_URL]))
        await prove_document()
        if before != after:
            stop('auth_cookie_changed')
    except HHUnexpectedUI:
        raise
    except Exception:
        stop('vacancy_forbidden_auth_unproven')
    session._hh_consecutive_forbidden_vacancies = ids
    session._hh_forbidden_document_proof = prove_document
    return True


class _PassiveEmbed(HTMLParser):
    """Wrap only the observed, validated tracking iframe; reject other embeds."""
    def __init__(self, html):
        super().__init__(convert_charrefs=False)
        self.html = html
        self.offsets = [0]
        self.offsets.extend(match.end() for match in re.finditer('\n', html))
        self.stack, self.spans = [], []
        self.pending = None

    def source_offset(self):
        line, column = self.getpos()
        return self.offsets[line - 1] + column

    def handle_starttag(self, tag, attributes):
        inert = any(t in {'script', 'style', 'template', 'noscript'} for t in self.stack)
        if not inert and tag in {'iframe', 'frame', 'object', 'embed'}:
            attrs = dict(attributes)
            if (tag != 'iframe' or set(attrs) != {'id', 'style', 'srcdoc'}
                    or len(attrs) != len(attributes) or attrs['id'] != 'mts-pixel'
                    or re.sub(r'\s+', '', attrs['style']) not in {'display:none;', 'display:none'}
                    or not _MTS_SRCDOC.fullmatch(attrs['srcdoc']) or self.pending is not None
                    or not self.stack or self.stack[-1] != 'div' or self.spans
                    or self.get_starttag_text().rstrip().endswith('/>')):
                raise ValueError('Unknown vacancy details embed')
            start = self.source_offset()
            self.pending = (start, start + len(self.get_starttag_text()))
        if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        if tag == 'iframe' and self.pending is not None:
            start, content = self.pending
            end = self.source_offset()
            if self.html[content:end].strip():
                raise ValueError('Unknown vacancy tracking iframe content')
            closing = re.match(r'</iframe\s*>', self.html[end:], re.I)
            if closing is None:
                raise ValueError('Unknown vacancy iframe closing markup')
            self.spans.append((start, end + closing.end()))
            self.pending = None
        if tag in self.stack:
            index = len(self.stack) - 1 - self.stack[::-1].index(tag)
            del self.stack[index:]


def inert_details_html(html):
    parser = _PassiveEmbed(html)
    parser.feed(html)
    parser.close()
    if parser.pending is not None:
        raise ValueError('Incomplete vacancy tracking iframe')
    for start, end in reversed(parser.spans):
        html = (html[:start] + '<template data-jh-passive-details="mts-pixel">'
                + html[start:end] + '</template>' + html[end:])
    return html


@asynccontextmanager
async def passive_details_navigation(session, vacancy_url, *, stage="vacancy_details", allowed=()):
    """Temporary Page routing; the next apply navigation gets an ordinary document."""
    path = urlsplit(vacancy_url).path
    if not re.fullmatch(r'/vacancy/[0-9]+/?', path):
        raise ValueError('Unknown HH vacancy URL')
    _trusted_url(vacancy_url, path.rstrip('/'))
    page, context = session._page, session._context
    guard, request_context = session._ui_guard, context.request
    document_epoch = getattr(page, '_hh_passive_document_epoch', None)
    if document_epoch is None:
        document_epoch = {'owner': session, 'value': 0}
        page._hh_passive_document_epoch = document_epoch
        def navigated(frame):
            if frame is page.main_frame:
                document_epoch['value'] += 1
        # Passive identity evidence only; never suppress or initiate navigation.
        page.on('framenavigated', navigated)
    failures, pending = [], set()
    navigation_count = 0

    def fresh():
        if (session._page is not page or session._context is not context
                or session._ui_guard is not guard or context.request is not request_context):
            session._hh_recovery_stop_reason = 'browser_ownership_changed'
            session._hh_recovery_uncertain = True
        session._raise_stopped_ui(stage)

    async def fetch_document(route):
        nonlocal navigation_count
        response = None
        redirect_chain = []
        content_type = ''
        try:
            fresh()
            navigation_count += 1
            if navigation_count > 4:
                raise ValueError('Vacancy details navigation limit exceeded')
            request = route.request
            _trusted_url(request.url, path.rstrip('/'))
            if request.method != 'GET' or _mutating_request(request):
                raise ValueError('Unknown vacancy details navigation payload')
            target, visited = request.url, set()
            # Native redirected documents bypass Playwright routing. Fetch only
            # validated read-only hops here and fulfill the final passive HTML.
            for _ in range(4):
                if target in visited:
                    raise ValueError('Vacancy details redirect loop detected')
                visited.add(target)
                response = await route.fetch(url=target, max_redirects=0, max_retries=0, timeout=20000)
                fresh()
                _trusted_url(response.url, path.rstrip('/'))
                if _mutating_request(type('Navigation', (), {'method': 'GET', 'url': response.url})()):
                    raise ValueError('Unknown vacancy details response payload')
                headers = dict(response.headers)
                redirect_chain.append({'url': safe_navigation_url(response.url), 'status': response.status})
                if response.status not in (301, 302, 303, 307, 308):
                    break
                location = headers.get('location', '')
                if not location:
                    raise ValueError('Vacancy redirect has no location')
                target = urljoin(target, location)
                _trusted_url(target, path.rstrip('/'))
                if _mutating_request(type('Navigation', (), {'method': 'GET', 'url': target})()):
                    raise ValueError('Unknown vacancy redirect payload')
                await response.dispose()
                response = None
                fresh()
            else:
                raise ValueError('Vacancy details redirect limit exceeded')
            content_type = headers.get('content-type', '').split(';', 1)[0].strip().casefold()
            if (stage == 'vacancy_details' and response.status == 403
                    and content_type == 'text/html' and getattr(session, '_external_attempt', None) is None):
                # Never execute the denial response, or continue from a browser
                # error/previous active document. Keep a proven inert shell.
                error = HHVacancyNavigationError(vacancy_url, response_url=response.url,
                    status=403, content_type=content_type, redirect_chain=redirect_chain, method=request.method)
                token = secrets.token_hex(24)
                error._passive_document_token = token
                fresh()
                await route.fulfill(status=403, headers={
                    'content-type': 'text/html; charset=utf-8',
                    'content-security-policy': DETAILS_CSP,
                    'cache-control': 'no-store',
                }, body=('<!doctype html><html><head><meta data-jh-vacancy-forbidden content="'
                         + token + '"><title>HeadHunter: vacancy unavailable</title></head>'
                         '<body>Вакансия недоступна.</body></html>'))
                failures.append(error)
                return
            if response.status != 200 or content_type != 'text/html':
                raise HHVacancyNavigationError(vacancy_url, response_url=response.url,
                    status=response.status, content_type=content_type, redirect_chain=redirect_chain, method=request.method)
            body = inert_details_html(await response.text())
            fresh()
            existing = headers.get('content-security-policy', '')
            headers['content-security-policy'] = (existing + ', ' if existing else '') + DETAILS_CSP
            headers.pop('content-length', None)
            headers.pop('content-encoding', None)
            await route.fulfill(status=response.status, headers=headers, body=body)
        except BaseException as exc:
            if isinstance(exc, Exception) and not isinstance(exc, (HHUnexpectedUI, ValueError)):
                # Never persist transport exception text: it can contain request secrets.
                exc = HHVacancyNavigationError(vacancy_url,
                    response_url=getattr(response, 'url', ''), status=getattr(response, 'status', None),
                    content_type=content_type, redirect_chain=redirect_chain,
                    error_type=type(exc).__name__, error_code='document_fetch_failed')
            failures.append(exc)
            with contextlib.suppress(Exception):
                await route.abort()
        finally:
            if response is not None:
                try:
                    await response.dispose()
                    fresh()
                except BaseException as exc:
                    failures.append(exc)

    async def handler(route):
        if not route.request.is_navigation_request() or route.request.frame is not page.main_frame:
            await route.fallback()
            return
        task = asyncio.create_task(fetch_document(route))
        pending.add(task)
        try:
            await task
        finally:
            pending.discard(task)

    # The prior denial shell must still be owned and inert at the next
    # navigation boundary; the ordinary UI scan does not establish this.
    prior_proof = getattr(session, '_hh_forbidden_document_proof', None)
    if prior_proof is not None:
        await prior_proof()
    fresh()
    await page.route('**/*', handler)
    try:
        fresh()
        if prior_proof is not None:
            await prior_proof()
            session._hh_forbidden_document_proof = None
        try:
            yield
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            if failures:
                raise failures[0]
            raise
    finally:
        try:
            await page.unroute('**/*', handler)
        finally:
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
    fresh()
    if failures:
        raise failures[0]
    await session._ensure_expected_ui(stage, allowed=allowed)
