"""Read-only HH reconciliation; browser uncertainty never authorizes a retry."""
import asyncio
import hashlib
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlsplit

from hh.recovery import _auth_cookies, _request_matches
from hh.ui import CHALLENGE_TEXT


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


async def reconcile_post_submit(session, attempt, resume_id, letter):
    """One fresh authenticated GET before normal hard-stop destroys the context."""
    response = None
    try:
        if getattr(attempt, '_hh_reconciliation_started', False):
            return
        proof = _owned_post(session, attempt, resume_id, letter)
        if proof is None:
            return
        attempt._hh_reconciliation_started = True
        page, context = attempt.page, attempt.context
        request_context = context.request
        binding = getattr(session, '_cookie_binding', None)
        def fresh():
            if (session._page is not page or session._context is not context
                    or session._external_attempt is not attempt or context.request is not request_context
                    or getattr(session, '_cookie_binding', None) is not binding
                    or binding is not None and (binding.context is not context or binding.closing or binding.revoked)
                    or _owned_post(session, attempt, resume_id, letter) is not proof):
                raise RuntimeError('HH reconciliation ownership changed')
        fresh()
        # The Cookie header belongs to the actual dispatched request, rather
        # than an account snapshot taken only after navigation has completed.
        headers = await attempt._hh_application_posts[0]['request'].all_headers()
        fresh()
        sent_tokens = [part.strip().split('=', 1)[1] for part in headers.get('cookie', '').split(';')
                       if part.strip().split('=', 1)[0].casefold() == 'hhtoken' and '=' in part]
        if len(sent_tokens) != 1 or not sent_tokens[0]:
            return
        before = _auth_cookies(await context.cookies(['https://hh.ru/applicant/resumes']))
        fresh()
        if {dict(cookie)['value'] for cookie in before} != {sent_tokens[0]}:
            return
        sequence = context._hh_action_watch.application_post_sequence
        target = urlsplit(page.url)
        path = '/vacancy/' + proof['fields']['vacancy_id']
        if (target.scheme != 'https' or target.netloc not in {'hh.ru', 'spb.hh.ru'}
                or target.path != path or target.query or target.fragment):
            return
        url = target.geturl()
        response = await request_context.get(url, max_redirects=0, timeout=15000,
                                             headers={'Cache-Control': 'no-cache'})
        fresh()
        if (response.status != 200 or response.url != url
                or response.headers.get('content-type', '').split(';', 1)[0].strip().lower() != 'text/html'):
            return
        html = await response.text()
        fresh()
        if not applied_server_html(html):
            return
        after = _auth_cookies(await context.cookies(['https://hh.ru/applicant/resumes']))
        fresh()
        if before != after:
            return
        if context._hh_action_watch.application_post_sequence != sequence:
            return
        snapshot = {**proof, 'fields': dict(proof['fields'])}
        attempt._hh_verified_receipt = _Verified(attempt, snapshot, resume_id, letter,
                                                hashlib.sha256(html.encode()).hexdigest(), sequence)
        session._hh_verified_attempt = attempt
    except Exception:
        # Verification failure preserves the original uncertain result.
        return
    finally:
        if response is not None:
            try:
                await response.dispose()
            except Exception:
                # A failed read lifecycle must not publish verification.
                attempt._hh_verified_receipt = None


def confirmed_result(session, vacancy_url, result, *, owned_attempt=None):
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
        status = attempt.repository.get(attempt.url)['status']
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
