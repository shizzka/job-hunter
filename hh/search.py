"""Read HH search server HTML without entering a site document/lifecycle."""
import re
from urllib.parse import parse_qsl, urljoin, urlsplit

from hh.resume_refresh import _trusted_url

_SEARCH_KEYS = {'text', 'area', 'page', 'per_page', 'order_by', 'experience',
                'schedule', 'salary', 'only_with_salary'}


class HHSearchStopped(ValueError):
    """Search cannot continue safely after an access or redirect refusal."""


class HHSearchAuthError(HHSearchStopped):
    """The search response no longer proves an authenticated HH session."""


async def read_search_html(session, url):
    """Only trusted GET redirects; action uncertainty is never cleared/retried."""
    context, page = session._context, session._page
    if context is None or page is None:
        raise RuntimeError('HH search browser ownership unavailable')
    request_context = context.request
    query = sorted(parse_qsl(urlsplit(url).query, keep_blank_values=True))
    if any(key not in _SEARCH_KEYS for key, value in query):
        raise ValueError('Unknown HH search query payload')

    def trusted(target):
        try:
            path = urlsplit(target).path
            login = path.startswith(('/account/login', '/auth/'))
            _trusted_url(target, path if login else '/search/vacancy')
        except ValueError as exc:
            raise HHSearchStopped('HH search redirected outside trusted search') from exc
        if login:
            raise HHSearchAuthError('HH search authentication unavailable')
        if sorted(parse_qsl(urlsplit(target).query, keep_blank_values=True)) != query:
            raise HHSearchStopped('HH changed the search query during redirect')

    async def fresh():
        if (session._context is not context or session._page is not page
                or context.request is not request_context):
            session._hh_recovery_stop_reason = 'browser_ownership_changed'
            session._hh_recovery_uncertain = True
        await session._ensure_expected_ui('search')

    for _ in range(4):
        trusted(url)
        await fresh()
        response = await request_context.get(url, max_redirects=0, timeout=30000)
        try:
            await fresh()
            trusted(response.url)
            if response.status in (301, 302, 303, 307, 308):
                location = response.headers.get('location', '')
                if not location:
                    raise ValueError('HH search redirect has no location')
                target = urljoin(url, location)
                trusted(target)
                url = target
                body = None
            else:
                if response.status == 401:
                    raise HHSearchAuthError('HH search authentication unavailable')
                if response.status in (403, 429):
                    raise HHSearchStopped(f'HH search access refused: HTTP {response.status}')
                if response.status != 200:
                    raise ValueError('HH search server response unavailable')
                if response.headers.get('content-type', '').split(';', 1)[0].strip().casefold() != 'text/html':
                    raise ValueError('HH search returned unknown content type')
                body = await response.text()
                await fresh()
                if re.search(r'"userType"\s*:\s*"anonymous"|"luxPageName"\s*:\s*"ForbiddenPage"', body, re.I):
                    raise HHSearchAuthError('HH search authentication unavailable')
        finally:
            await response.dispose()
        await fresh()
        if body is not None:
            return body, response.url
    raise ValueError('HH search redirect limit exceeded')
