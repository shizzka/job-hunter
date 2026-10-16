"""Keep the standard response form functional without evidenced tracker frames."""
import asyncio
import contextlib
import json
import re
from contextlib import asynccontextmanager
from urllib.parse import parse_qsl, urljoin, urlsplit

from hh.response_path import RESPONSE_PATH, CANONICAL_PATH_SCRIPT, canonical_path
from hh.vacancy_details import _MTS_SRCDOC, inert_details_html


RESPONSE_CSP = (
    "script-src https://i.hh.ru/build/ 'unsafe-inline'; "
    "connect-src https://hh.ru/applicant/vacancy_response "
    "https://spb.hh.ru/applicant/vacancy_response "
    "https://hh.ru/applicant/vacancy_response/popup "
    "https://spb.hh.ru/applicant/vacancy_response/popup; "
    "worker-src 'none'; frame-src 'none'; object-src 'none'; "
    "form-action 'self'; base-uri 'none'"
)

# Captured HH replay timer helper (Wu); other builds/actors fail closed.
# This bundle's helper only reads native timer functions and has a window
# fallback if its temporary iframe cannot be appended/accessed/removed.
REPLAY_VENDOR_URLS = (
    'https://i.hh.ru/build/vendors.df512a23f097c519.js',
    # Captured 2026-10-11: identical Wu timer fallback, newer static build.
    'https://i.hh.ru/build/vendors.91818c9dc6b48eb9.js',
)


def _exact_response_url(url, vacancy_id):
    p = urlsplit(str(url))
    return (p.scheme == 'https' and p.hostname in {'hh.ru', 'spb.hh.ru'}
            and p.netloc == p.hostname and not p.fragment
            and canonical_path(p.path) == RESPONSE_PATH
            and parse_qsl(p.query, keep_blank_values=True) == [('vacancyId', vacancy_id)])


def _tracker_init(vacancy_id):
    # This is the same fully anchored grammar used to validate the server HTML.
    grammar = re.sub(r'\s+', '', _MTS_SRCDOC.pattern)
    return r"""(() => {
      const id=__ID__, mts=new RegExp('^(?:'+__GRAMMAR__+')$');
      const replayVendors=__REPLAY_VENDORS__;
      const exact=()=>location.protocol==='https:'&&['hh.ru','spb.hh.ru'].includes(location.host)&&(__CANONICAL_PATH__)(location.pathname)==='/applicant/vacancy_response'&&!location.hash&&new URLSearchParams(location.search).toString()==='vacancyId='+id;
      for(const method of ['appendChild','insertBefore']) {
        const original=Node.prototype[method];
        Node.prototype[method]=function(node,...args) {
          if(exact() && node instanceof HTMLIFrameElement) {
            const attrs=[...node.attributes].map(a=>a.name).sort().join(',');
            const style=(node.getAttribute('style')||'').replace(/\s+/g,'');
            const mtsFrame=this instanceof HTMLDivElement && attrs==='id,srcdoc,style' && node.id==='mts-pixel' && ['display:none','display:none;'].includes(style) && mts.test(node.getAttribute('srcdoc')||'');
            const websocketFrame=this===document.body && attrs==='id,src,style' && node.id==='websocket-iframe' && node.getAttribute('src')==='https://websocket.hh.ru/proxy-webapp/index.html' && ['display:none','display:none;'].includes(style);
            const feedbackFrame=this===document.body && attrs==='class,sandbox,src' && node.getAttribute('class')==='uxfeedback-widget' && node.getAttribute('sandbox')==='allow-scripts allow-same-origin allow-forms allow-popups' && node.getAttribute('src')==='https://hhcdn.ru/uxfeedback/widget.html';
            let replayTimerFrame=false;
            if(method==='appendChild' && this===document.head && attrs==='hidden' && node.getAttribute('hidden')==='' && node.hidden && node.childNodes.length===0) {
              try {
                replayTimerFrame=(new Error().stack||'').split('\n').some(line=>{
                  line=line.trim();
                  return replayVendors.some(vendor=>{
                    const prefix='at Wu ('+vendor+':';
                    return line.startsWith(prefix) && /^\d+:\d+\)$/.test(line.slice(prefix.length));
                  });
                });
              } catch (_) {}
            }
            if(mtsFrame || websocketFrame || feedbackFrame || replayTimerFrame) return node;
          }
          return Reflect.apply(original,this,[node,...args]);
        };
      }
    })();""".replace('__CANONICAL_PATH__', CANONICAL_PATH_SCRIPT).replace('__ID__', json.dumps(vacancy_id)).replace('__GRAMMAR__', json.dumps(grammar)).replace('__REPLAY_VENDORS__', json.dumps(REPLAY_VENDOR_URLS))


@asynccontextmanager
async def response_navigation(session, target_url):
    """Isolate only this GET initialization; existing action guards own the form."""
    query = parse_qsl(urlsplit(target_url).query, keep_blank_values=True)
    vacancy_id = query[0][1] if len(query) == 1 and query[0][0] == 'vacancyId' else ''
    if not re.fullmatch(r'[0-9]+', vacancy_id) or not _exact_response_url(target_url, vacancy_id):
        raise ValueError('Unknown response URL')
    page, context, guard = session._page, session._context, session._ui_guard
    request_context = context.request
    pending, failures = set(), []

    def fresh():
        if (session._page is not page or session._context is not context
                or session._ui_guard is not guard or context.request is not request_context):
            session._hh_recovery_stop_reason = 'browser_ownership_changed'
            session._hh_recovery_uncertain = True
        session._raise_stopped_ui('resume_preflight')

    async def fetch_document(route):
        response = None
        try:
            fresh()
            request = route.request
            if request.method != 'GET' or not _exact_response_url(request.url, vacancy_id):
                raise ValueError('Unknown response navigation')
            target = request.url
            # Playwright routes only the first native redirect request. Follow
            # each validated read-only hop here and fulfill the final HTML once.
            for _ in range(4):
                response = await route.fetch(url=target, max_redirects=0, max_retries=0, timeout=20000)
                fresh()
                if not _exact_response_url(response.url, vacancy_id):
                    raise ValueError('Unknown response document URL')
                headers = dict(response.headers)
                if response.status not in (301, 302, 303, 307, 308):
                    break
                location = headers.get('location', '')
                target = urljoin(target, location)
                if not location or not _exact_response_url(target, vacancy_id):
                    raise ValueError('Unknown response redirect')
                await response.dispose()
                response = None
            else:
                raise ValueError('Response redirect limit exceeded')
            if response.status != 200 or headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'text/html':
                raise ValueError('Unknown response document')
            html = await response.text()
            # Reuse this existing GET before artifact sanitization. No selected
            # control or application permission is inferred from this map.
            with contextlib.suppress(Exception):
                from hh.delivery import capture_resume_hashes
                capture_resume_hashes(getattr(session, '_external_attempt', None), html, vacancy_id)
            body = inert_details_html(html)
            fresh()
            old = headers.get('content-security-policy', '')
            headers['content-security-policy'] = (old + ', ' if old else '') + RESPONSE_CSP
            headers.pop('content-length', None)
            headers.pop('content-encoding', None)
            await route.fulfill(status=200, headers=headers, body=body)
        except BaseException as exc:
            failures.append(exc)
            with contextlib.suppress(Exception):
                await route.abort()
        finally:
            if response is not None:
                with contextlib.suppress(Exception):
                    await response.dispose()

    async def handler(route):
        request = route.request
        if not request.is_navigation_request() or request.frame != page.main_frame:
            await route.fallback()
            return
        task = asyncio.create_task(fetch_document(route))
        pending.add(task)
        try:
            await task
        finally:
            pending.discard(task)

    fresh()
    await context.add_init_script(_tracker_init(vacancy_id))
    await page.route('**/*', handler)
    try:
        yield
        fresh()
        if failures:
            raise failures[0]
    finally:
        try:
            await page.unroute('**/*', handler)
        finally:
            tasks = tuple(pending)
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)
