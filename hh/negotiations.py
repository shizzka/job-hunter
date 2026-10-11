"""Read startup negotiation statuses without entering the site document."""
import re
from urllib.parse import urljoin

from hh.resume_refresh import _trusted_url
from hh.search_html import _tree, search_antibot_kind

PATH = '/applicant/negotiations'


def parse_negotiation_statuses(html, *, base_url):
    tree = _tree(html)
    if search_antibot_kind(html):
        raise ValueError('HH negotiations require manual challenge verification')
    for node in tree.root.walk():
        attrs = node.attrs
        if (attrs.get('role') == 'dialog' or attrs.get('aria-modal') == 'true'
                or (node.tag == 'dialog' and 'open' in attrs)
                or 'modal' in attrs.get('data-qa', '').casefold()):
            raise ValueError('Unexpected HH negotiations dialog')
    items = []
    for card in tree.root.walk():
        attrs = card.attrs
        if (attrs.get('data-qa') != 'negotiations-item'
                and not {'negotiations-item', 'resume-negotiations-item'}.intersection(attrs.get('class', '').split())):
            continue
        lines = [line.strip() for line in card.raw_text().splitlines() if line.strip()]
        if len(lines) < 3:
            continue
        link = next((node for node in card.walk()
                     if node.tag == 'a' and '/vacancy/' in node.attrs.get('href', '')), None)
        href = urljoin(base_url, link.attrs['href']) if link is not None else ''
        vacancy_id = href.split('/vacancy/')[-1].split('?')[0].split('/')[0] if '/vacancy/' in href else ''
        items.append({'id': vacancy_id, 'status': lines[0], 'title': lines[1],
                      'company': lines[2], 'url': href})
    return items


async def read_negotiation_statuses(session, *, base_url):
    context, page = session._context, session._page
    if context is None or page is None:
        raise RuntimeError('HH negotiations browser ownership unavailable')
    request_context = context.request
    url = base_url.rstrip('/') + PATH

    async def fresh():
        if (session._context is not context or session._page is not page
                or context.request is not request_context):
            session._hh_recovery_stop_reason = 'browser_ownership_changed'
            session._hh_recovery_uncertain = True
        await session._ensure_expected_ui('negotiation_statuses')

    for _ in range(4):
        _trusted_url(url, PATH)
        await fresh()
        response = await request_context.get(url, max_redirects=0, timeout=20000)
        try:
            await fresh()
            _trusted_url(response.url, PATH)
            if response.status in (301, 302, 303, 307, 308):
                location = response.headers.get('location', '')
                if not location:
                    raise ValueError('HH negotiations redirect has no location')
                url = urljoin(url, location)
                _trusted_url(url, PATH)
                body = None
            else:
                if response.status != 200:
                    raise ValueError('HH negotiations server response unavailable')
                if response.headers.get('content-type', '').split(';', 1)[0].strip().casefold() != 'text/html':
                    raise ValueError('HH negotiations returned unknown content type')
                body = await response.text()
                await fresh()
                if re.search(r'"userType"\s*:\s*"anonymous"|"luxPageName"\s*:\s*"ForbiddenPage"', body, re.I):
                    raise ValueError('HH negotiations authentication unavailable')
        finally:
            await response.dispose()
        await fresh()
        if body is not None:
            return parse_negotiation_statuses(body, base_url=response.url)
    raise ValueError('HH negotiations redirect limit exceeded')
