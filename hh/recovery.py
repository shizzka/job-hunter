"""Abandon an HH Page without running its response or lifecycle handlers.

Recovery never dismisses a dialog or navigates the old document. A native
preparing owner is sealed before yielding; the old renderer stays suspended
until destruction. The replacement proves authentication before publication.
"""
from __future__ import annotations

import contextlib
import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from urllib.parse import parse_qs, parse_qsl, urlsplit

from browser_action_boundary import RUNTIME, bootstrap_boundary
from hh.ui import CHALLENGE_SELECTOR, CHALLENGE_TEXT, HHUIGuard, HHUnexpectedUI

AUTH_URL = 'https://hh.ru/applicant/resumes'
BLOCK_WORKER_CONSTRUCTORS = r'''(() => {
    const previous = window.__jhHHWorkersBlocked;
    const valid = api => !!api && api.blocked === true && ['Worker','SharedWorker'].every(name => {
        const descriptor = Object.getOwnPropertyDescriptor(window,name);
        return descriptor?.value === api[name] && descriptor.writable === false && descriptor.configurable === false;
    });
    if (previous) return valid(previous);
    const denied = function() { throw new Error('HH background workers require manual intervention'); };
    for (const name of ['Worker','SharedWorker'])
        Object.defineProperty(window,name,{value:denied,writable:false,configurable:false});
    const api = Object.freeze({blocked:true,Worker:denied,SharedWorker:denied});
    Object.defineProperty(window,'__jhHHWorkersBlocked',{value:api,writable:false,configurable:false});
    return valid(api);
})()'''
PASSIVE_INSPECTION = r'''(() => ({
    embedded: !!document.querySelector('iframe,frame,object[type="text/html"],embed[type="text/html"]'),
    challenge: !!document.querySelector('iframe[src*="captcha" i], [data-qa="captcha"], [class*="captcha" i], [id*="captcha" i]'),
    text: (document.body?.innerText || '').slice(0, 3000),
    anonymous: /"userType"\s*:\s*"anonymous"|"luxPageName"\s*:\s*"ForbiddenPage"/i.test(document.documentElement?.innerHTML || '')
}))()'''


@dataclass
class PageActions:
    session: object
    page: object
    unknown: bool = False
    owners: set[str] = field(default_factory=set)
    last_attempt: object = None

    def mark_unknown(self):
        self.unknown = True
        self.session._hh_recovery_stop_reason = 'possible_external_action'
        self.session._hh_recovery_uncertain = True
        live = getattr(self.session, '_external_attempt', None)
        observed = live if live is not None and live.page is self.page else self.last_attempt
        _preserve_unknown_attempt(observed)
        if getattr(self.session, '_page', None) is not self.page:
            watch = getattr(self.page.context, '_hh_action_watch', None)
            if watch is not None:
                watch.mark_unknown()

    def observe(self, request):
        attempt = getattr(self.session, '_external_attempt', None)
        proof = getattr(self.page, '_hh_request_approval', None)
        if (attempt is not None and attempt.acting and not attempt.abandoned
                and attempt.page is self.page and isinstance(proof, dict)
                and proof.get('owner') == attempt.owner and not proof.get('used')
                and attempt.owner not in self.owners and _request_matches(proof, request)):
            proof['used'] = True
            self.owners.add(attempt.owner)
        else:
            self.mark_unknown()


@dataclass
class ContextActions:
    session: object
    pages: dict = field(default_factory=dict)
    unknown: bool = False

    def mark_unknown(self):
        self.unknown = True
        self.session._hh_recovery_stop_reason = 'possible_external_action'
        self.session._hh_recovery_uncertain = True
        attempt = getattr(self.session, '_external_attempt', None)
        if attempt is None:
            page = getattr(self.session, '_page', None)
            monitor = self.pages.get(page)
            attempt = monitor.last_attempt if monitor is not None else None
        candidates = [attempt, *(monitor.last_attempt for monitor in self.pages.values())]
        observed = set()
        for candidate in candidates:
            if candidate is not None and candidate.owner not in observed:
                observed.add(candidate.owner)
                _preserve_unknown_attempt(candidate)


def _preserve_unknown_attempt(attempt):
    if attempt is None:
        return
    attempt.uncertain = True
    attempt.proven_no_action = False
    # Late browser events can upgrade this owner's terminal receipt too.
    with contextlib.suppress(Exception):
        attempt.repository.transition(attempt.url, attempt.owner, 'uncertain')


def _request_matches(proof, request):
    """A NativeAttempt being acting is not proof of a request's approval."""
    try:
        entries = proof['entries']
        if not all(isinstance(item, list) and len(item) == 2 and all(isinstance(value, str) for value in item) for item in entries):
            return False
        actual, approved = urlsplit(request.url), urlsplit(proof['url'])
        if (actual.scheme, actual.netloc, actual.path) != (approved.scheme, approved.netloc, approved.path):
            return False
        method = request.method.upper()
        if method != proof['method']:
            return False
        if method == 'GET':
            return parse_qsl(actual.query, keep_blank_values=True) == [tuple(item) for item in entries]
        if method != 'POST' or actual.query != approved.query or proof['enctype'] != 'application/x-www-form-urlencoded':
            return False
        content_type = request.headers.get('content-type', '').split(';', 1)[0].strip().casefold()
        if content_type != 'application/x-www-form-urlencoded' or request.post_data is None:
            return False
        return parse_qsl(request.post_data, keep_blank_values=True) == [tuple(item) for item in entries]
    except Exception:
        return False


def _mutating_request(request):
    method = request.method.upper()
    if method not in {'GET', 'HEAD'}:
        return True
    # A form may use GET. Response entry navigation has only vacancyId; a
    # successful-control payload is an external action even with a GET method.
    keys = set(parse_qs(urlsplit(request.url).query))
    return bool(keys & {'resume_id', 'resumeId', 'resumeHash', 'resume', 'letter'})


def monitor_page(session, page):
    """Install before initial navigation; absence of this evidence fails closed."""
    context = page.context
    watch = getattr(context, '_hh_action_watch', None)
    if watch is None:
        watch = ContextActions(session)
        context._hh_action_watch = watch
        def request_seen(request):
            try:
                if not _mutating_request(request):
                    return
                observed_page = request.frame.page
                monitor = watch.pages.get(observed_page)
                if monitor is None:
                    watch.mark_unknown()
                else:
                    monitor.observe(request)
            except Exception:
                # Worker-originated requests have no owning Frame/Page.
                watch.mark_unknown()
        context.on('request', request_seen)
        context.on('serviceworker', lambda worker: watch.mark_unknown())
    if watch.session is not session:
        raise RuntimeError('HH BrowserContext action ownership changed')
    if page in watch.pages:
        return watch.pages[page]
    monitor = PageActions(session, page)
    watch.pages[page] = monitor
    page._hh_action_monitor = monitor
    def socket_seen(socket):
        socket.on('framesent', lambda frame: monitor.mark_unknown())
    page.on('websocket', socket_seen)
    # A removed iframe can have exposed a pristine Worker constructor before
    # its document-start hook ran. Snapshots cannot erase that history.
    page.on('frameattached', lambda frame: monitor.mark_unknown())
    page.on('popup', lambda popup: monitor.mark_unknown())
    page.on('worker', lambda worker: monitor.mark_unknown())
    return monitor


def action_observed(client, attempt):
    page = getattr(attempt, 'page', None)
    monitor = getattr(page, '_hh_action_monitor', None)
    watch = getattr(getattr(attempt, 'context', None), '_hh_action_watch', None)
    return bool((monitor is not None and (monitor.unknown or attempt.owner in monitor.owners))
                or (watch is not None and watch.unknown))


def acknowledge_completed_action(client, attempt):
    """Clear only requests belonging to a verified, durably completed owner."""
    monitor = getattr(getattr(attempt, 'page', None), '_hh_action_monitor', None)
    if monitor is not None and getattr(client, '_external_attempt', None) is attempt:
        state = attempt.repository.get(attempt.url)
        if state.get('owner') == attempt.owner and state.get('status') == 'completed' and not attempt.uncertain:
            monitor.owners.discard(attempt.owner)


@dataclass(frozen=True)
class RecoveryResult:
    recovered: bool
    reason: str = ''
    uncertain: bool = False


class RecoveryBlocked(RuntimeError):
    def __init__(self, reason, *, uncertain=False):
        self.reason, self.uncertain = reason, uncertain
        super().__init__(reason)


def _auth_cookies(cookies):
    tokens = []
    for item in cookies:
        if item.get('name', '').casefold() == 'hhtoken':
            if not item.get('value'):
                raise RecoveryBlocked('auth_cookie_empty')
            tokens.append(tuple((key, item.get(key)) for key in ('name', 'value', 'domain', 'path', 'expires', 'secure', 'httpOnly', 'sameSite')))
    if not tokens:
        raise RecoveryBlocked('auth_cookie_missing')
    return sorted(tokens, key=repr)


def _passive_problem(url, inspection):
    parsed = urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname != 'hh.ru' or parsed.port not in (None, 443)
            or parsed.username is not None or parsed.password is not None):
        return 'auth_origin_unproven'
    if '/account/login' in parsed.path or '/auth/' in parsed.path or inspection.get('anonymous'):
        return 'auth_session_lost'
    if ('captcha' in parsed.path.casefold() or inspection.get('challenge')
            or any(text in str(inspection.get('text', '')).casefold() for text in CHALLENGE_TEXT)):
        return 'captcha_or_antibot'
    return ''


class _AuthenticatedResumeHTML(HTMLParser):
    """Read server markup without creating a document or executing site code."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.inert = []
        self.marker = False
        self.problem = ''

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {'script', 'style', 'template', 'noscript'}:
            self.inert.append(tag)
            return
        if self.inert:
            return
        qa = attrs.get('data-qa', '').casefold()
        identity = ' '.join(attrs.get(key, '') for key in ('id', 'class', 'data-qa')).casefold()
        if tag in {'iframe', 'frame', 'object', 'embed'}:
            self.problem = 'authenticated_server_embedded_ui'
        elif 'captcha' in identity:
            self.problem = 'captcha_or_antibot'
        elif (tag == 'dialog' or attrs.get('role', '').casefold() == 'dialog'
              or attrs.get('aria-modal', '').casefold() == 'true'
              or any(word in identity for word in ('modal', 'onboarding', 'whats-new'))
              or any(word in identity for word in ('profile-update', 'profile-completion', 'complete-profile'))):
            self.problem = 'authenticated_server_unexpected_ui'
        if qa == 'resume' or qa.startswith('resume-card-link-'):
            self.marker = True

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        if self.inert and tag == self.inert[-1]:
            self.inert.pop()

    def handle_data(self, data):
        if not self.inert and any(text in data.casefold() for text in CHALLENGE_TEXT):
            self.problem = 'captcha_or_antibot'


def _authenticated_server_html(body):
    if not isinstance(body, str) or re.search(r'"userType"\s*:\s*"anonymous"|"luxPageName"\s*:\s*"ForbiddenPage"', body, re.I):
        raise RecoveryBlocked('auth_session_lost')
    parser = _AuthenticatedResumeHTML()
    parser.feed(body)
    parser.close()
    if parser.problem:
        raise RecoveryBlocked(parser.problem)
    if not parser.marker:
        raise RecoveryBlocked('authenticated_resume_marker_missing')


async def abandon_page(session, *, attempt=None):
    """No click, submit, FormData or old-document navigation is performed here."""
    if getattr(session, '_recovering', False):
        session._hh_recovery_stop_reason = 'recovery_already_active'
        return RecoveryResult(False, 'recovery_already_active', True)
    old, context = getattr(session, '_page', None), getattr(session, '_context', None)
    guard = getattr(session, '_ui_guard', None)
    binding = getattr(session, '_cookie_binding', None)
    nonce = getattr(session, '_cookie_write_nonce', None)
    monitor = getattr(old, '_hh_action_monitor', None)
    watch = getattr(context, '_hh_action_watch', None)
    request_context = getattr(context, 'request', None)
    token = object()
    session._recovering = token
    cdp = fresh = None
    uncertain = False

    def prove():
        if (getattr(session, '_recovering', None) is not token or session._page is not old
                or session._context is not context or getattr(session, '_external_attempt', None) is not attempt):
            raise RecoveryBlocked('browser_ownership_changed', uncertain=True)
        if getattr(session, '_hh_recovery_stop_reason', ''):
            raise RecoveryBlocked('recovery_previously_stopped', uncertain=True)
        if (binding is None or getattr(session, '_cookie_binding', None) is not binding
                or binding.context is not context or binding.closing or binding.revoked
                or getattr(session, '_cookie_write_nonce', None) is not nonce):
            raise RecoveryBlocked('cookie_session_ownership_unproven', uncertain=True)
        if request_context is None or context.request is not request_context:
            raise RecoveryBlocked('auth_request_context_ownership_unproven', uncertain=True)
        if (old is None or context is None or old.context is not context or not isinstance(guard, HHUIGuard)
                or monitor is None or monitor.session is not session or watch is None
                or watch.session is not session or watch.pages.get(old) is not monitor):
            raise RecoveryBlocked('action_monitor_unproven', uncertain=True)
        if (getattr(session, '_hh_blocked_worker_context', None) is not context
                or getattr(context, '_hh_workers_owner', None) is not session):
            raise RecoveryBlocked('blocked_service_worker_policy_unproven', uncertain=True)
        if watch.unknown or monitor.unknown or monitor.owners:
            raise RecoveryBlocked('possible_external_action', uncertain=True)
        if len(old.frames) != 1:
            raise RecoveryBlocked('embedded_frame_lifecycle_unproven')
        if old.workers:
            raise RecoveryBlocked('background_worker_ownership_unproven', uncertain=True)
        if context.service_workers:
            raise RecoveryBlocked('service_worker_ownership_unproven', uncertain=True)
        if any(page is not old and page is not fresh for page in context.pages):
            raise RecoveryBlocked('additional_page_ownership_unproven', uncertain=True)
        if getattr(old, '_jh_boundary_fences', {}):
            raise RecoveryBlocked('action_boundary_owner_unproven', uncertain=True)
        if attempt is not None:
            if attempt.acting or attempt.command_started or attempt.uncertain:
                raise RecoveryBlocked('native_action_or_uncertainty', uncertain=True)
            state = attempt.repository.get(attempt.url)
            if (state.get('owner') != attempt.owner or state.get('status') != 'preparing'
                    or not state.get('recovery_sealed')):
                raise RecoveryBlocked('native_receipt_ownership_unproven', uncertain=True)
        if fresh is not None:
            if len(fresh.frames) != 1:
                raise RecoveryBlocked('fresh_page_embedded_frame_unproven')
            new_monitor = getattr(fresh, '_hh_action_monitor', None)
            if new_monitor is None or new_monitor.unknown or new_monitor.owners:
                raise RecoveryBlocked('replacement_possible_external_action', uncertain=True)

    try:
        # Both the live latch and durable preparing reservation happen before
        # any await. Another owner cannot claim, and this owner cannot act.
        if attempt is not None:
            if attempt.acting or attempt.command_started or attempt.uncertain:
                raise RecoveryBlocked('native_action_or_uncertainty', uncertain=True)
            attempt.seal_recovery()
        prove()
        cdp = await context.new_cdp_session(old)
        prove()
        await cdp.send('Emulation.setScriptExecutionDisabled', {'value': True})
        prove()
        before = _auth_cookies(await context.cookies([AUTH_URL]))
        prove()
        tree = (await cdp.send('Page.getFrameTree'))['frameTree']
        prove()
        # Top-level suspension is not proof for an out-of-process iframe.
        # Abandonment is allowed only for a single proven renderer surface.
        if tree.get('childFrames'):
            raise RecoveryBlocked('embedded_frame_lifecycle_unproven')
        world = await cdp.send('Page.createIsolatedWorld', {'frameId': tree['frame']['id'], 'worldName': 'hh-abandon-read'})
        prove()
        evidence = await cdp.send('Runtime.evaluate', {'expression': PASSIVE_INSPECTION,
                                  'contextId': world['executionContextId'], 'returnByValue': True})
        prove()
        inspection = evidence.get('result', {}).get('value')
        if evidence.get('exceptionDetails') or not isinstance(inspection, dict):
            raise RecoveryBlocked('old_page_challenge_inspection_unproven')
        if inspection.get('embedded'):
            raise RecoveryBlocked('embedded_frame_lifecycle_unproven')
        problem = _passive_problem(old.url, inspection)
        if problem:
            raise RecoveryBlocked(problem)
        # A suspended renderer cannot run unload/pagehide or a close handler.
        # Never restore script execution on this abandoned document.
        prove()
        await old.close(run_before_unload=False)
        prove()
        if not old.is_closed():
            raise RecoveryBlocked('old_page_destruction_unproven')
        fresh = await context.new_page()
        fresh_monitor = monitor_page(session, fresh)
        fresh_monitor.last_attempt = attempt
        prove()
        await bootstrap_boundary(fresh)
        prove()
        if fresh.url != 'about:blank' or await fresh.evaluate(BLOCK_WORKER_CONSTRUCTORS) is not True:
            raise RecoveryBlocked('fresh_blank_worker_policy_unproven', uncertain=True)
        prove()
        # add_init_script applies to future documents. This current document
        # is trusted about:blank and needs the same boundary explicitly now.
        if fresh.url != 'about:blank' or await fresh.evaluate(RUNTIME.replace('navigationFence = true', 'navigationFence = false', 1)) is not True:
            raise RecoveryBlocked('fresh_blank_action_boundary_unproven', uncertain=True)
        prove()
        fresh_guard = HHUIGuard(guard.home, notify=guard.notify, clock=guard.warnings.clock)
        fresh._hh_ui_guard = fresh_guard
        # BrowserContext.request shares this context's cookie jar. A read-only
        # server response proves HH authentication without loading any site
        # document, timer, lifecycle handler, or out-of-process iframe.
        response = await request_context.get(AUTH_URL, max_redirects=0, timeout=20000)
        try:
            prove()
            if response.status != 200 or response.url != AUTH_URL:
                raise RecoveryBlocked('authenticated_server_response_unproven')
            if response.headers.get('content-type', '').split(';', 1)[0].strip().casefold() != 'text/html':
                raise RecoveryBlocked('authenticated_server_content_unproven')
            body = await response.text()
            prove()
            _authenticated_server_html(body)
        finally:
            await response.dispose()
        prove()
        after = _auth_cookies(await context.cookies([AUTH_URL]))
        prove()
        if before != after:
            raise RecoveryBlocked('auth_cookie_changed')
        final_inspection = await fresh.evaluate(PASSIVE_INSPECTION)
        prove()
        if not isinstance(final_inspection, dict) or final_inspection.get('embedded'):
            raise RecoveryBlocked('fresh_page_embedded_frame_unproven')
        if fresh.url != 'about:blank' or final_inspection.get('text') or final_inspection.get('challenge'):
            raise RecoveryBlocked('fresh_blank_document_changed', uncertain=True)
        # Publish all page-scoped ownership together, without an await.
        session._page, session._ui_guard = fresh, fresh_guard
        session._approved_hh_payload = None
        session._submit_boundary_id = None
        return RecoveryResult(True)
    except BaseException as exc:
        uncertain = isinstance(exc, RecoveryBlocked) and exc.uncertain
        reason = exc.reason if isinstance(exc, RecoveryBlocked) else 'recovery_not_confirmed'
        if monitor is not None and (monitor.unknown or monitor.owners):
            uncertain = True
        if watch is not None and watch.unknown:
            uncertain = True
        if attempt is not None and attempt.uncertain:
            uncertain = True
        if attempt is not None and uncertain:
            attempt.uncertain = True
            attempt.proven_no_action = False
        session._hh_recovery_stop_reason = reason
        session._hh_recovery_uncertain = uncertain
        if fresh is not None:
            # An unproven replacement must not remain an actionable page.
            # Failure to suspend leaves it owned by browser shutdown, never
            # used by the next vacancy; do not close live lifecycle handlers.
            with contextlib.suppress(Exception):
                fresh_cdp = await context.new_cdp_session(fresh)
                await fresh_cdp.send('Emulation.setScriptExecutionDisabled', {'value': True})
                await fresh_cdp.detach()
        hard_stop = getattr(session, 'hard_stop_browser', None)
        if hard_stop is not None:
            # Abrupt owned-process termination is safe for OOPIFs too. No
            # cookie persistence or graceful Page/context close is allowed.
            await hard_stop()
        if not isinstance(exc, Exception):
            raise
        return RecoveryResult(False, reason, uncertain)
    finally:
        if cdp is not None:
            with contextlib.suppress(Exception):
                await cdp.detach()
        if getattr(session, '_recovering', None) is token:
            session._recovering = False


async def recover_unexpected_ui(session, exc, *, attempt=None):
    result = await abandon_page(session, attempt=attempt)
    if isinstance(exc, HHUnexpectedUI):
        exc.hh_recovered = result.recovered
        exc.hh_stop_reason = result.reason
        exc.hh_uncertain = result.uncertain
    return result
