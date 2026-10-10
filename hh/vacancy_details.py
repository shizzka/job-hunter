"""Render a read-only vacancy preflight without running site integrations.

The live Page/URL/DOM are retained for trace metadata. This document stays
passive until the next ordinary navigation; no queued site JS is re-enabled.
"""
import asyncio
import contextlib
import re
from contextlib import asynccontextmanager
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

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
        try:
            fresh()
            navigation_count += 1
            if navigation_count > 4:
                raise ValueError('Vacancy details navigation limit exceeded')
            request = route.request
            _trusted_url(request.url, path.rstrip('/'))
            if request.method != 'GET' or _mutating_request(request):
                raise ValueError('Unknown vacancy details navigation payload')
            response = await route.fetch(max_redirects=0, max_retries=0, timeout=20000)
            fresh()
            _trusted_url(response.url, path.rstrip('/'))
            headers = dict(response.headers)
            if response.status in (301, 302, 303, 307, 308):
                target = urljoin(request.url, headers.get('location', ''))
                if not headers.get('location'):
                    raise ValueError('Vacancy redirect has no location')
                _trusted_url(target, path.rstrip('/'))
                if _mutating_request(type('Navigation', (), {'method': 'GET', 'url': target})()):
                    raise ValueError('Unknown vacancy redirect payload')
                body = ''
            else:
                if response.status != 200 or headers.get('content-type', '').split(';', 1)[0].strip().casefold() != 'text/html':
                    raise ValueError('Unknown vacancy details response')
                body = inert_details_html(await response.text())
                fresh()
                existing = headers.get('content-security-policy', '')
                headers['content-security-policy'] = (existing + ', ' if existing else '') + DETAILS_CSP
            headers.pop('content-length', None)
            headers.pop('content-encoding', None)
            await route.fulfill(status=response.status, headers=headers, body=body)
        except BaseException as exc:
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

    fresh()
    await page.route('**/*', handler)
    try:
        fresh()
        try:
            yield
        except BaseException:
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
