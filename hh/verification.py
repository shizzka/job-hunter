"""Read-only HH reconciliation; browser uncertainty never authorizes a retry."""
import asyncio
import hashlib
import logging
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlsplit

from hh.recovery import AUTH_URL, _auth_cookies, _authenticated_server_html, _request_matches
from hh.ui import CHALLENGE_TEXT, HHUIGuard
from hh import observation

log = logging.getLogger('hh_verification')


def _diagnose(attempt, trace, step, ok=False, **fields):
    """Only classifications and counts: never headers, cookies or page text."""
    diagnostic = {'step': step, **fields}
    name = '_hh_continuation_diagnostic' if step.startswith('continuation_') else '_hh_reconciliation_diagnostic'
    setattr(attempt, name, diagnostic)
    log.info('HH reconciliation ok=%s evidence=%s', ok, diagnostic)
    if trace is not None:
        try:
            trace.event('POST_SUBMIT_RECONCILIATION', ok=ok, **diagnostic)
        except Exception:
            log.warning('HH reconciliation trace unavailable')


class _AppliedHTML(HTMLParser):
    """Require active vacancy markup, excluding scripts, templates and embeds."""
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack, self.text = [], []
        self.title = self.again = self.conflict = False

    def handle_starttag(self, tag, attributes):
        attrs = dict(attributes)
        inert = (any(hidden for _, hidden in self.stack)
                 or tag in {'script', 'style', 'template', 'noscript', 'iframe', 'frame', 'object', 'embed'}
                 or 'hidden' in attrs or attrs.get('aria-hidden', '').lower() == 'true'
                 or bool(re.search(r'(?:display\s*:\s*none|visibility\s*:\s*hidden)', attrs.get('style', ''), re.I)))
        if not inert:
            qa = attrs.get('data-qa', '')
            self.title |= tag == 'h1' and qa == 'vacancy-title'
            self.again |= tag in {'a', 'button'} and qa in {
                'vacancy-response-link-top-again', 'vacancy-response-link-bottom-again'}
            self.conflict |= (qa in {'vacancy-response-link-top', 'vacancy-response-link-bottom', 'captcha'}
                              or tag == 'form' and attrs.get('name') == 'vacancy_response')
        if tag not in {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}:
            self.stack.append((tag, inert))

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        self.handle_endtag(tag)

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break

    def handle_data(self, value):
        if not any(hidden for _, hidden in self.stack):
            self.text.append(value)


def applied_server_html(html):
    parser = _AppliedHTML()
    parser.feed(html)
    parser.close()
    text = ' '.join(' '.join(parser.text).casefold().split())
    return (parser.title and parser.again and not parser.conflict
            and 'резюме доставлено' in text
            and not any(marker in text for marker in CHALLENGE_TEXT)
            and not re.search(r'"userType"\s*:\s*"anonymous"|"luxPageName"\s*:\s*"ForbiddenPage"', html, re.I))


@dataclass(frozen=True)
class _Verified:
    attempt: object
    proof: dict
    resume_id: str
    letter: str
    body_sha256: str
    post_sequence: int


def _owned_post(session, attempt, resume_id, letter):
    page, context = attempt.page, attempt.context
    monitor = getattr(page, '_hh_action_monitor', None)
    watch = getattr(context, '_hh_action_watch', None)
    proof = getattr(page, '_hh_request_approval', None)
    posts = getattr(attempt, '_hh_application_posts', [])
    if (attempt.client is not session or attempt.repository.source != 'hh'
            or not attempt.acting or attempt.abandoned or attempt.proven_no_action
            or monitor is None or monitor.session is not session
            or monitor.page is not page or monitor.attempts.get(attempt.owner) is not attempt
            or watch is None or watch.session is not session or watch.context is not context
            or watch.last_attempt is not attempt
            or page.context is not context or watch.pages.get(page) is not monitor
            or not isinstance(proof, dict) or proof.get('kind') != 'hh-react-popup'
            or proof.get('owner') != attempt.owner or proof.get('used') is not True
            or len(posts) != 1 or posts[0]['owned'] is not True or posts[0]['status'] != 200):
        return None
    fields = proof.get('fields', {})
    vacancy_id = fields.get('vacancy_id', '')
    if (not isinstance(vacancy_id, str) or not vacancy_id.isascii() or not vacancy_id.isdigit()
            or fields.get('resume_hash') != resume_id or fields.get('letter') != letter
            or attempt.repository.key(attempt.url) != attempt.repository.key('https://hh.ru/vacancy/' + vacancy_id)
            or posts[0]['request'].frame is not page.main_frame
            or not _request_matches(proof, posts[0]['request'])):
        return None
    state = attempt.repository.get(attempt.url)
    if state.get('owner') != attempt.owner or state.get('status') not in {'acting', 'uncertain', 'completed'}:
        return None
    return proof


async def reconcile_post_submit(session, attempt, resume_id, letter, *, trace=None):
    """Verify one owned POST; at most two GETs allow server rendering to catch up."""
    if trace is None:
        trace = getattr(attempt, '_hh_reconciliation_trace', None)
    response = None
    step = 'reentry'
    read_number = 0
    get_started = False
    observed_reads = set()
    try:
        if getattr(attempt, '_hh_reconciliation_started', False):
            _diagnose(attempt, trace, step)
            return
        step = 'owned_post'
        proof = _owned_post(session, attempt, resume_id, letter)
        if proof is None:
            _diagnose(attempt, trace, step)
            return
        attempt._hh_reconciliation_started = True
        page, context = attempt.page, attempt.context
        request_context = context.request
        binding = getattr(session, '_cookie_binding', None)
        sequence = context._hh_action_watch.application_post_sequence
        def fresh():
            if context._hh_action_watch.application_post_sequence != sequence:
                _diagnose(attempt, trace, 'post_sequence_changed', after=step,
                          expected=sequence, actual=context._hh_action_watch.application_post_sequence,
                          read_number=read_number)
                raise RuntimeError('HH reconciliation ownership changed')
            checks = {
                'page': session._page is page, 'context': session._context is context,
                'attempt': session._external_attempt is attempt,
                'request_context': context.request is request_context,
                'cookie_binding': getattr(session, '_cookie_binding', None) is binding,
                'cookie_owner': binding is None or (binding.context is context and not binding.closing and not binding.revoked),
                'owned_post': _owned_post(session, attempt, resume_id, letter) is proof,
                'page_monitor': not page._hh_action_monitor.unknown,
                'context_watch': not context._hh_action_watch.unknown,
            }
            failed = [name for name, valid in checks.items() if not valid]
            if failed:
                _diagnose(attempt, trace, 'ownership_changed', after=step, failed=failed,
                          read_number=read_number)
                raise RuntimeError('HH reconciliation ownership changed')
        step = 'initial_ownership'
        fresh()
        # The Cookie header belongs to the actual dispatched request, rather
        # than an account snapshot taken only after navigation has completed.
        step = 'post_headers'
        headers = await attempt._hh_application_posts[0]['request'].all_headers()
        fresh()
        sent_tokens = [part.strip().split('=', 1)[1] for part in headers.get('cookie', '').split(';')
                       if part.strip().split('=', 1)[0].casefold() == 'hhtoken' and '=' in part]
        if len(sent_tokens) != 1 or not sent_tokens[0]:
            _diagnose(attempt, trace, 'post_cookie_missing_or_ambiguous', token_count=len(sent_tokens))
            return
        step = 'cookies_before'
        before = _auth_cookies(await context.cookies(['https://hh.ru/applicant/resumes']))
        fresh()
        if {dict(cookie)['value'] for cookie in before} != {sent_tokens[0]}:
            _diagnose(attempt, trace, 'post_cookie_mismatch')
            return
        # The form may disappear without a redirect, or navigation may destroy
        # its renderer. The exact owned POST supplies the vacancy and origin;
        # the current browser URL is not a receipt of the remote result.
        target = urlsplit(proof['url'])
        url = target._replace(path='/vacancy/' + proof['fields']['vacancy_id']).geturl()
        for read_number in (1, 2):
            if read_number == 2:
                step = 'cookies_before_retry'
                current = _auth_cookies(await context.cookies([AUTH_URL]))
                fresh()
                if before != current:
                    _diagnose(attempt, trace, 'cookies_changed', read_number=read_number)
                    return
            step = 'vacancy_get'
            get_started = True
            response = await request_context.get(url, max_redirects=0, timeout=15000,
                                                 headers={'Cache-Control': 'no-cache'})
            fresh()
            content_type = response.headers.get('content-type', '').split(';', 1)[0].strip().lower()
            if response.status != 200 or response.url != url or content_type != 'text/html':
                attempt._hh_reconciliation_blocked = True
                _diagnose(attempt, trace, 'vacancy_response', read_number=read_number,
                          status=response.status, url_matches=response.url == url, html=content_type == 'text/html')
                return
            step = 'vacancy_html'
            html = await response.text()
            try:
                observation.snapshot(attempt, html, proof['fields']['vacancy_id'], resume_id,
                                     'after', trace=trace, read_number=read_number)
            except Exception as exc:
                observation.snapshot_error(attempt, 'after', type(exc).__name__, trace=trace,
                                           read_number=read_number)
            observed_reads.add(read_number)
            fresh()
            parser = _AppliedHTML()
            parser.feed(html)
            parser.close()
            text = ' '.join(parser.text).casefold()
            blocked = (any(marker in text for marker in CHALLENGE_TEXT)
                       or bool(re.search(r'"userType"\s*:\s*"anonymous"|"luxPageName"\s*:\s*"ForbiddenPage"', html, re.I))
                       or bool(re.search(r'(?:data-qa|id|class)=[\"\'][^\"\']*captcha', html, re.I)))
            if blocked:
                attempt._hh_reconciliation_blocked = True
                _diagnose(attempt, trace, 'server_auth_or_challenge', read_number=read_number)
                return
            step = 'cookies_after'
            after = _auth_cookies(await context.cookies([AUTH_URL]))
            fresh()
            if before != after:
                _diagnose(attempt, trace, 'cookies_changed', read_number=read_number)
                return
            if applied_server_html(html):
                break
            _diagnose(attempt, trace, 'applied_server_html_false', read_number=read_number,
                      title=parser.title, again=parser.again, conflict=parser.conflict,
                      delivered='резюме доставлено' in ' '.join(text.split()))
            step = 'response_dispose'
            await response.dispose()
            response = None
            fresh()
            if read_number == 2:
                return
            step = 'render_delay'
            await asyncio.sleep(0.5)
            fresh()
        step = 'response_dispose'
        await response.dispose()
        response = None
        fresh()
        step = 'cookies_final'
        after = _auth_cookies(await context.cookies([AUTH_URL]))
        fresh()
        if before != after:
            _diagnose(attempt, trace, 'cookies_changed', read_number=read_number)
            return
        snapshot = {**proof, 'fields': dict(proof['fields'])}
        attempt._hh_verified_receipt = _Verified(attempt, snapshot, resume_id, letter,
                                                hashlib.sha256(html.encode()).hexdigest(), sequence)
        session._hh_verified_attempt = attempt
        _diagnose(attempt, trace, 'verified', ok=True, read_number=read_number)
    except Exception as exc:
        # Verification failure preserves the original uncertain result.
        if not (isinstance(exc, RuntimeError) and str(exc) == 'HH reconciliation ownership changed'):
            _diagnose(attempt, trace, step, error_kind=type(exc).__name__, read_number=read_number)
        if get_started and response is None and read_number not in observed_reads:
            observation.snapshot_error(attempt, 'after', type(exc).__name__, trace=trace, read_number=read_number)
        return
    finally:
        if response is not None:
            # On rejected responses the original verifier never reads HTML.
            # Collect it only during teardown, after its decision is fixed.
            if read_number not in observed_reads:
                try:
                    observation.snapshot(attempt, await response.text(), proof['fields']['vacancy_id'],
                                         resume_id, 'after', trace=trace, read_number=read_number)
                except Exception as exc:
                    observation.snapshot_error(attempt, 'after', type(exc).__name__, trace=trace,
                                               read_number=read_number)
            try:
                await response.dispose()
            except Exception:
                # A failed read lifecycle must not publish verification.
                attempt._hh_verified_receipt = None
                _diagnose(attempt, trace, 'response_dispose', read_number=read_number)
        # This separate read cannot set a receipt, diagnostic, continuation or stop latch.
        try:
            await observation.finish_receipts(attempt)
            if not get_started and step != 'reentry':
                await observation.after_observation(session, attempt, resume_id, trace=trace)
        except Exception as exc:
            observation.snapshot_error(attempt, 'after_observation', type(exc).__name__, trace=trace)


@dataclass(frozen=True)
class _UncertainSession:
    attempt: object
    binding: object
    request_context: object
    post_sequence: int


def _session_intact(session, record):
    """Session permission is separate from the unresolved vacancy's outcome."""
    attempt = record.attempt
    page, context = attempt.page, attempt.context
    monitor, watch = page._hh_action_monitor, context._hh_action_watch
    active = getattr(session, '_external_attempt', None)
    if (session._page is not page or session._context is not context or page.context is not context
            or page.is_closed() or context.request is not record.request_context
            or getattr(session, '_cookie_binding', None) is not record.binding
            or record.binding.context is not context or record.binding.closing or record.binding.revoked
            or monitor.session is not session or monitor.page is not page or monitor.unknown
            or watch.session is not session or watch.context is not context or watch.unknown
            or watch.pages.get(page) is not monitor or monitor.attempts.get(attempt.owner) is not attempt
            or getattr(session, '_hh_recovery_stop_reason', '')
            or getattr(session, '_hh_recovery_uncertain', False) or getattr(session, '_recovering', False)
            or getattr(session._ui_guard, 'blocked', None) is not None
            or getattr(attempt, '_hh_guard_failure', None) or getattr(attempt, '_hh_reconciliation_blocked', False)
            or watch.application_post_sequence < record.post_sequence):
        return False
    # Subsequent legitimate work may replace last_attempt. Never accept an
    # unregistered/foreign owner, and never clear any of its action evidence.
    latest = watch.last_attempt
    if (latest is None or latest.client is not session or latest.page is not page or latest.context is not context
            or monitor.attempts.get(latest.owner) is not latest or monitor.last_attempt is not latest
            or latest.repository.source != 'hh' or (active is not None and active is not latest)):
        return False
    state = latest.repository.get(latest.url)
    if state.get('owner') != latest.owner:
        return False
    if latest is not attempt:
        if state.get('status') in {'preparing', 'acting'}:
            if active is not latest or latest.uncertain:
                return False
        elif state.get('status') == 'uncertain':
            later = getattr(latest, '_hh_uncertain_session', None)
            if not isinstance(later, _UncertainSession) or later.attempt is not latest or later.binding is not record.binding:
                return False
        elif state.get('status') not in {'completed', 'failed'}:
            return False
    allowed_owners = {attempt.owner}
    if active is not None and active.acting:
        allowed_owners.add(active.owner)
    return not (monitor.owners - allowed_owners)


async def prepare_uncertain_continuation(session, attempt, result, resume_id, letter, *, trace=None):
    """Permit later vacancies only after independent authentication and UI reads."""
    response = None
    step = 'continuation_eligibility'
    try:
        if (result.get('anti_bot_kind') or getattr(attempt, '_hh_guard_failure', None)
                or getattr(attempt, '_hh_reconciliation_diagnostic', {}).get('step') != 'applied_server_html_false'):
            return False
        proof = _owned_post(session, attempt, resume_id, letter)
        binding = getattr(session, '_cookie_binding', None)
        if proof is None or binding is None or not isinstance(session._ui_guard, HHUIGuard):
            return False
        record = _UncertainSession(attempt, binding, attempt.context.request,
                                   attempt.context._hh_action_watch.application_post_sequence)

        def fresh():
            if (not _session_intact(session, record) or session._external_attempt is not attempt
                    or _owned_post(session, attempt, resume_id, letter) is not proof
                    or attempt.context._hh_action_watch.application_post_sequence != record.post_sequence
                    or attempt.page._hh_action_monitor.owners - {attempt.owner}):
                raise RuntimeError('HH continuation ownership changed')

        fresh()
        step = 'continuation_ui'
        dialogs = await session._ui_guard._scan(attempt.page, ('response',))
        fresh()
        if any(item['kind'] != 'response' for item in dialogs):
            _diagnose(attempt, trace, step, reason='blocking_ui')
            return False
        before = _auth_cookies(await attempt.context.cookies([AUTH_URL]))
        fresh()
        # Recheck the actual POST's account too, even if cookies changed after
        # the unsuccessful vacancy verification but before this separate read.
        headers = await attempt._hh_application_posts[0]['request'].all_headers()
        fresh()
        sent = [part.strip().split('=', 1)[1] for part in headers.get('cookie', '').split(';')
                if '=' in part and part.strip().split('=', 1)[0].casefold() == 'hhtoken']
        if len(sent) != 1 or {dict(cookie)['value'] for cookie in before} != {sent[0]}:
            _diagnose(attempt, trace, 'continuation_cookie_mismatch')
            return False
        step = 'continuation_auth'
        response = await record.request_context.get(AUTH_URL, max_redirects=0, timeout=15000,
                                                    headers={'Cache-Control': 'no-cache'})
        fresh()
        if (response.status != 200 or response.url != AUTH_URL
                or response.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'text/html'):
            _diagnose(attempt, trace, step, reason='response_unproven', status=response.status)
            return False
        _authenticated_server_html(await response.text())
        fresh()
        after = _auth_cookies(await attempt.context.cookies([AUTH_URL]))
        fresh()
        if before != after:
            _diagnose(attempt, trace, 'continuation_cookies_changed')
            return False
        await response.dispose()
        response = None
        fresh()
        step = 'continuation_ui_final'
        dialogs = await session._ui_guard._scan(attempt.page, ('response',))
        fresh()
        if any(item['kind'] != 'response' for item in dialogs):
            _diagnose(attempt, trace, step, reason='blocking_ui')
            return False
        after = _auth_cookies(await attempt.context.cookies([AUTH_URL]))
        fresh()
        if before != after:
            _diagnose(attempt, trace, 'continuation_cookies_changed')
            return False
        attempt._hh_uncertain_session = record
        _diagnose(attempt, trace, 'continuation_session_verified', ok=True)
        return True
    except Exception as exc:
        _diagnose(attempt, trace, step, error_kind=type(exc).__name__)
        return False
    finally:
        if response is not None:
            try:
                await response.dispose()
            except Exception:
                pass


def can_continue_uncertain(session, attempt):
    """A typed session receipt never converts an uncertain outcome into success."""
    try:
        record = getattr(attempt, '_hh_uncertain_session', None)
        if not isinstance(record, _UncertainSession) or record.attempt is not attempt:
            return False
        state = attempt.repository.get(attempt.url)
        return (state.get('owner') == attempt.owner and state.get('status') == 'uncertain'
                and _session_intact(session, record))
    except Exception:
        return False


def confirmed_result(session, vacancy_url, result, *, owned_attempt=None, close_attempt=False):
    """Report the proven remote fact separately from a dirty browser receipt."""
    try:
        attempt = getattr(session, '_hh_verified_attempt', None)
        record = getattr(attempt, '_hh_verified_receipt', None)
        if not isinstance(record, _Verified) or record.attempt is not attempt:
            return None
        if owned_attempt is not None and owned_attempt is not attempt:
            return None
        if (attempt.context._hh_action_watch.application_post_sequence != record.post_sequence
                or not _owned_browser(session, attempt)):
            return None
        if attempt.repository.key(vacancy_url) != attempt.repository.key(attempt.url):
            return None
        proof = _owned_post(session, attempt, record.resume_id, record.letter)
        if proof != record.proof:
            return None
        if result.get('selected_resume_id') not in {None, '', record.resume_id}:
            return None
        monitor = attempt.page._hh_action_monitor
        watch = attempt.context._hh_action_watch
        # A confirmed vacancy does not erase an unrelated external action,
        # authentication failure, or revoked browser/cookie ownership.
        allowed_stops = {'', 'native_action_or_uncertainty', 'native_attempt_already_acting'}
        if (monitor.unknown or watch.unknown or result.get('anti_bot_kind')
                or getattr(attempt, '_hh_guard_failure', None)
                or getattr(session, '_hh_recovery_stop_reason', '') not in allowed_stops
                or result.get('hh_recovery_reason', '') not in allowed_stops):
            return None
        binding = getattr(session, '_cookie_binding', None)
        if (session._context is attempt.context and binding is not None
                and (binding.context is not attempt.context or binding.closing or binding.revoked)):
            return None
        status = attempt.repository.get(attempt.url)['status']
        # Close stale attempt-local inference before NativeAttempt writes its
        # terminal receipt. Acting remains set and completed/uncertain durable
        # receipts stay non-retryable; never reopen a terminal uncertain owner.
        if status != 'completed' and not (close_attempt and status == 'acting'):
            return None
        if close_attempt and status == 'acting':
            attempt.uncertain = False
            session._hh_recovery_stop_reason = ''
            session._hh_recovery_uncertain = False
        result = {key: value for key, value in result.items()
                  if key not in {'hh_hard_stop', 'hh_recovery_reason'}}
        return {**result, 'ok': True, 'uncertain': False, 'already_applied': False,
                'submission_status': 'success_confirmed', 'native_receipt_status': status,
                'resume_selection_verified': True, 'selected_resume_id': record.resume_id,
                'cover_letter_status': 'submitted_with_application' if record.letter else 'not_requested',
                'message': 'Отклик подтверждён свежим HH GET после одного успешного application POST'}
    except Exception:
        return None


def _owned_browser(session, attempt):
    if session._context is attempt.context and session._page is attempt.page:
        return True
    # Preserve a verified remote result across only the normal, fully awaited
    # shutdown of this captured browser; an arbitrary reference swap is unsafe.
    shutdown = getattr(session, '_hh_shutdown_operation', None)
    if (not isinstance(shutdown, tuple) or len(shutdown) != 3
            or any(getattr(session, name, None) is not None for name in
                   ('_context', '_page', '_browser', '_pw', '_cookie_binding'))):
        return False
    context, browser, operation = shutdown
    from hh.browser import _stop_browser_owned
    return (context is attempt.context and browser is not None
            and context.browser is browser and getattr(browser, '_hh_owner_session', None) is session
            and isinstance(operation, asyncio.Task) and operation.done() and not operation.cancelled()
            and operation.exception() is None
            and getattr(operation.get_coro(), 'cr_code', None) is _stop_browser_owned.__code__)
