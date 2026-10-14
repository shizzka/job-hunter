"""Recheck approved native payload synchronously at browser click/submit events."""

RESPONSE_PATH = '/applicant/vacancy_response'

# Captured HH component 501788. DOM-attached Fibers can be alternates: only
# HostRoot.stateNode.current may supply the current selected-resume props.
REACT_RESPONSE_STATE_SCRIPT = r'''() => {
    const url = new URL(location.href), query = [...url.searchParams];
    if (url.protocol !== 'https:' || !['hh.ru','spb.hh.ru'].includes(url.host) ||
        url.pathname !== '/applicant/vacancy_response' || url.hash || query.length !== 1 ||
        query[0][0] !== 'vacancyId' || !/^\d+$/.test(query[0][1])) return null;
    const forms = [...document.querySelectorAll('form[name="vacancy_response"]')];
    if (forms.length !== 1) return null;
    const form = forms[0], key = Object.keys(form).find(key => key.startsWith('__reactFiber$'));
    let host = key && form[key];
    for (let count=0; host?.return && count<100; count++) host=host.return;
    const current = host?.stateNode?.current;
    if (!current) return null;
    const stack=[current], seen=new Set();
    let node=null, store=null;
    while (stack.length && seen.size<20000) {
        const fiber=stack.pop();
        if (!fiber || seen.has(fiber)) continue;
        seen.add(fiber);
        if (fiber.stateNode === form) node=fiber;
        const candidate=fiber.memoizedProps?.value?.store;
        if (candidate && typeof candidate.getState === 'function') {
            if (store && store !== candidate) return null;
            store=candidate;
        }
        if (fiber.child) stack.push(fiber.child);
        if (fiber.sibling) stack.push(fiber.sibling);
    }
    if (stack.length || !node || !store) return null;
    let props=null;
    for (let count=0; node && count<100; node=node.return,count++) {
        if (node.type?.displayName === 'components/VacancyResponseForm/index/VacancyResponseForm') {
            if (props) return null;
            props=node.memoizedProps;
        }
    }
    const resume=props?.selectedResume, state=store.getState(), id=query[0][1];
    const status=state?.applicantVacancyResponseStatuses?.[id];
    const matches=Object.values(status?.resumes || {}).filter(item => item?.hash === resume?.hash);
    const agreement=state?.countriesProfileVisibilityAgreement;
    if (!resume || typeof resume.hash !== 'string' || !resume.hash ||
        typeof resume.isIncomplete !== 'boolean' || matches.length !== 1 ||
        matches[0].isIncomplete !== resume.isIncomplete || String(props.vacancyId) !== id ||
        typeof props.letterValue !== 'string' || props.vacancySource != null ||
        !agreement || String(agreement.vacancyId) !== id || agreement.show !== false ||
        !['boolean','undefined'].includes(typeof agreement.confirmed) ||
        !(agreement.confirmedCountryIds === undefined || Array.isArray(agreement.confirmedCountryIds)) ||
        Object.keys(state.router?.location?.query || {}).some(key => key !== 'vacancyId')) return null;
    const countries=agreement.confirmedCountryIds || [];
    if (!countries.every(value => Number.isSafeInteger(Number(value)))) return null;
    return {hash:resume.hash, incomplete:resume.isIncomplete, letter:props.letterValue,
        visibility:!!agreement.confirmed, countries:countries.map(Number), vacancy:id};
}'''


def popup_request_matches(proof, request):
    """Compare every unique multipart field against the state-derived approval."""
    from email import policy
    from email.parser import BytesParser
    from urllib.parse import urlsplit
    import re
    try:
        expected = proof['fields']
        keys = {'_xsrf', 'vacancy_id', 'resume_hash', 'ignore_postponed', 'incomplete',
                'mark_applicant_visible_in_vacancy_country', 'country_ids', 'letter',
                'lux', 'withoutTest', 'hhtmFromLabel', 'hhtmSourceLabel'}
        if (not isinstance(expected, dict) or set(expected) != keys
                or not all(isinstance(value, str) for value in expected.values())):
            return False
        actual, approved = urlsplit(request.url), urlsplit(proof['url'])
        if (actual != approved or actual.scheme != 'https'
                or actual.netloc not in {'hh.ru', 'spb.hh.ru'}
                or actual.path != RESPONSE_PATH + '/popup'
                or actual.query or actual.fragment or request.method != 'POST'):
            return False
        content_type = request.headers.get('content-type', '')
        if not re.fullmatch(r'multipart/form-data; boundary=[A-Za-z0-9_-]{1,70}', content_type):
            return False
        raw = request.post_data_buffer
        if not isinstance(raw, bytes):
            return False
        message = BytesParser(policy=policy.default).parsebytes(
            b'Content-Type: ' + content_type.encode('ascii') + b'\r\nMIME-Version: 1.0\r\n\r\n' + raw)
        if (not message.is_multipart() or message.defects
                or message['content-type'].defects):
            return False
        values = {}
        for part in message.iter_parts():
            headers = list(part.raw_items())
            if (part.is_multipart() or part.defects or len(headers) != 1
                    or headers[0][0].lower() != 'content-disposition'
                    or part['content-disposition'].defects):
                return False
            disposition = re.fullmatch(r'form-data; name="([A-Za-z_][A-Za-z0-9_]*)"', headers[0][1])
            if not disposition or disposition[1] in values:
                return False
            name = disposition[1]
            values[name] = part.get_payload(decode=True).decode('utf-8')
        # Browser multipart serialization canonicalizes text line endings.
        fields = {key: value.replace('\r\n', '\n').replace('\r', '\n').replace('\n', '\r\n')
                  for key, value in expected.items()}
        return values == fields
    except Exception:
        return False


async def _install_popup_gate(session, boundary_id, descriptor):
    """Validate owned, fresh payloads before forwarding any response POST."""
    from hh.recovery import _request_matches
    page = session._page
    attempt = getattr(session, '_external_attempt', None)
    used = False

    async def gate(route):
        nonlocal used
        request = route.request
        if request.method in {'GET', 'HEAD'}:
            await route.fallback()
            return
        proof = getattr(page, '_hh_request_approval', None)
        watch = getattr(page.context, '_hh_action_watch', None)
        owned = (attempt is not None and attempt.acting and not attempt.abandoned
                 and session._page is page and getattr(session, '_external_attempt', None) is attempt
                 and getattr(session, '_context', None) is page.context
                 and attempt.page is page and attempt.context is page.context
                 and request.frame is page.main_frame
                 and isinstance(proof, dict) and proof.get('owner') == attempt.owner
                 and proof.get('id') == boundary_id and proof.get('used') is True
                 and watch is not None and watch.session is session
                 and watch.context is page.context and not watch.unknown)
        fresh = False
        try:
            if owned and not used and _request_matches(descriptor, request):
                fresh = await page.evaluate("id => window['__jhHHPopup_' + id]?.fresh() === true", boundary_id)
        except Exception:
            pass
        if not fresh:
            monitor = getattr(page, '_hh_action_monitor', None)
            if monitor is not None:
                monitor.mark_unknown(request)
            await route.abort()
            return
        used = True
        await route.continue_()

    await page.route('**/applicant/vacancy_response**', gate)


async def wait_popup_receipt_navigation(session, boundary_id):
    """Read the new document only after this owned popup's vacancy redirect."""
    from urllib.parse import urlsplit
    page, context = session._page, session._context
    proof = getattr(page, '_hh_request_approval', None)
    if not isinstance(proof, dict) or proof.get('kind') != 'hh-react-popup':
        return
    attempt = getattr(session, '_external_attempt', None)
    vacancy_id = proof.get('fields', {}).get('vacancy_id', '')
    if not isinstance(vacancy_id, str) or not vacancy_id.isascii() or not vacancy_id.isdigit():
        raise RuntimeError('Unverified HH popup receipt vacancy')

    def fresh():
        monitor = getattr(page, '_hh_action_monitor', None)
        watch = getattr(context, '_hh_action_watch', None)
        if (attempt is None or not attempt.acting or attempt.abandoned or attempt.uncertain
                or session._page is not page or session._context is not context
                or getattr(session, '_external_attempt', None) is not attempt
                or attempt.page is not page or attempt.context is not context
                or getattr(page, '_hh_request_approval', None) is not proof
                or proof.get('id') != boundary_id or proof.get('owner') != attempt.owner
                or proof.get('used') is not True or monitor is None or monitor.unknown
                or attempt.owner not in monitor.owners or watch is None or watch.unknown
                or watch.session is not session or watch.context is not context
                or watch.pages.get(page) is not monitor):
            raise RuntimeError('HH popup receipt ownership is uncertain')
        session._raise_stopped_ui('after_submit_navigation')

    def receipt_url(url):
        parsed = urlsplit(str(url))
        return (parsed.scheme == 'https' and parsed.netloc in {'hh.ru', 'spb.hh.ru'}
                and parsed.path == '/vacancy/' + vacancy_id
                and not parsed.query and not parsed.fragment)

    fresh()
    await page.wait_for_url(receipt_url, wait_until='domcontentloaded', timeout=15000)
    fresh()


async def arm_submit_boundary(session):
    expected = getattr(session, '_approved_hh_payload', None)
    if expected is None:
        return True  # Non-application controls have their existing UI guards.
    from browser_action_boundary import RUNTIME, install_boundary, release_boundary
    import uuid
    expected = {**expected, "boundary_id": uuid.uuid4().hex}
    await install_boundary(session._page, expected['boundary_id'])
    from hh.apply import SELECTED_RESUME_SCRIPT
    from hh.forms import VERIFY_ANSWERS_SCRIPT
    from hh.ui import _INSPECT
    from hh.recovery import CHALLENGE_SELECTOR, CHALLENGE_TEXT
    import json
    script = r'''expected => {
        /* codex:hh-submit-arm */
        const identity = (__IDENTITY__);
        const answersMatch = (__ANSWERS__);
        const inspectUI = (__UI__);
        const challengeTexts = __CHALLENGE_TEXT__;
        const uiAdmissible = () => !document.querySelector(__CHALLENGE_SELECTOR__) &&
            !challengeTexts.some(text => (document.body?.innerText || '').slice(0, 3000).toLowerCase().includes(text)) &&
            inspectUI().every(item => item.descriptor.kind === 'response');
        const visible = el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
        const forms = [...document.querySelectorAll('form[name="vacancy_response"]')].filter(visible);
        const dialogs = [...document.querySelectorAll('[role="dialog"]')].filter(el => visible(el) &&
            el.querySelector('[data-qa="vacancy-response-submit-popup"], [data-qa="vacancy-response-letter-submit"]'));
        const root = forms.length === 1 ? forms[0] : forms.length === 0 && dialogs.length === 1 ? dialogs[0] : null;
        if (!root) return false;
        const embedded = [...root.querySelectorAll('form')];
        if (embedded.length > 1) return false;
        const form = root.tagName === 'FORM' ? root : embedded[0] || null;
        const url = location.href;
        const controls = () => [...new Set([
            ...root.querySelectorAll('input,textarea,select,[contenteditable="true"]'),
            ...(form ? [...form.elements].filter(el => el.matches('input,textarea,select')) : [])
        ])];
        const entries = submitter => form ? [...window.__jhActionBoundary.formData(form, submitter || undefined).entries()]
            .map(([key,value]) => [key, typeof value === 'string' ? value :
                ['file',value.name,value.size,value.type,value.size ? value.lastModified : 0]]) : [];
        const payload = submitter => JSON.stringify(entries(submitter));
        const reactState = (__REACT_STATE__);
        const metadata = reactState();
        const react = !!metadata && !!form && form === root && !form.hasAttribute('action') &&
            form.method === 'post' && !form.target && !expected.answers?.length &&
            metadata.incomplete === false && metadata.visibility === false && metadata.countries.length === 0 &&
            metadata.hash === expected.resume_id && metadata.letter === (expected.cover_letter || '') &&
            controls().length === 2 &&
            controls().filter(el => el.matches('input[type="hidden"][name="_xsrf"]') && el.value).length === 1 &&
            controls().filter(el => el.matches('textarea[data-qa="vacancy-response-popup-form-letter-input"]') && !el.name).length === 1 &&
            JSON.stringify(entries().map(([key]) => key)) === '["_xsrf"]';
        const valid = (submitter = null) => {
            const actual = entries(submitter);
            const successfulIds = actual.filter(([key]) => ['resume_id','resumeId','resumeHash','resume'].includes(key)).map(([,value]) => value);
            if (form && !successfulIds.length && !react) return false;
            if (react && JSON.stringify(reactState()) !== JSON.stringify(metadata)) return false;
            const ids = identity().ids.concat(successfulIds);
            if (!expected.resume_id || !ids.length || ids.some(id => id !== expected.resume_id)) return false;
            const letters = controls().filter(el => !el.matches(':disabled') && el.matches(
                '[name="letter"], [data-qa="vacancy-response-popup-form-letter-input"], textarea[data-qa*="letter"]'));
            if (expected.cover_letter && letters.length !== 1) return false;
            if (letters.some(el => el.value !== (expected.cover_letter || ''))) return false;
            const letterNames = new Set(['letter', ...letters.map(el => el.name).filter(Boolean)]);
            const successfulLetters = actual.filter(([key]) => letterNames.has(key));
            if (form && expected.cover_letter && successfulLetters.length !== 1 && !react) return false;
            if (successfulLetters.some(([,value]) => value !== (expected.cover_letter || ''))) return false;
            return !expected.answers?.length || answersMatch(expected.answers);
        };
        const snapshot = () => JSON.stringify([identity(),
            form ? [form.id,form.action,form.method,form.enctype,form.target] : null, payload(null), controls().map(el =>
            [el.tagName, el.type, el.name, el.getAttribute('form'), el.getAttribute('data-qa'), el.getAttribute('data-codex-auto-field-id'),
             el.matches('input[type=button],input[type=submit]') ? null : el.disabled, el.required, el.value ?? el.textContent, el.checked,
             [...(el.options || [])].map(o => [o.value,o.text,o.selected,o.disabled])])]);
        if (!valid() || !uiAdmissible()) return false;
        const approved = snapshot(), approvedControls = controls();
        const unchanged = () => {
            const current = controls();
            return root.isConnected && location.href === url && current.length === approvedControls.length &&
                current.every((el,index) => el === approvedControls[index]) && valid() && snapshot() === approved && uiAdmissible();
        };
        const submitterState = el => el ? JSON.stringify([el.name,el.value,el.type,el.getAttribute('form'),
            el.getAttribute('formaction'),el.getAttribute('formmethod'),el.getAttribute('formenctype'),el.getAttribute('formtarget')]) : null;
        const approval = {root, form, valid, snapshot, approved, unchanged, id: expected.boundary_id, admitted: false, blocked: false, payload: payload(null)};
        approval.bindControl = el => {
            if (react && (!el?.matches('button[type="submit"][data-qa="vacancy-response-submit-popup"]') ||
                el.form !== form || el.name || ['formaction','formmethod','formenctype','formtarget'].some(key => el.hasAttribute(key)) ||
                form.querySelectorAll('button[type="submit"],input[type="submit"]').length !== 1)) return false;
            if (approval.bound) return el === approval.control && unchanged() &&
                submitterState(el) === approval.submitterState &&
                payload(el?.form === form && el.type === 'submit' ? el : null) === approval.payload;
            if (el?.form && el.form !== form) return false;
            if (!unchanged() || !valid(el?.form === form && el.type === 'submit' ? el : null) || (el && (!el.isConnected || !(root.contains(el) || (form && el.form === form))))) return false;
            approval.bound = true;
            approval.control = el;
            approval.submitterState = submitterState(el);
            approval.payload = payload(el?.form === form && el.type === 'submit' ? el : null);
            return true;
        };
        approval.eventMatches = (name,event,target) => unchanged() &&
            valid(name === 'submit' ? event.submitter : target?.form === form && target.type === 'submit' ? target : null) &&
            (name !== 'click' || target === approval.control && submitterState(target) === approval.submitterState) &&
            (name !== 'submit' || event.target === form && event.submitter === approval.control) &&
            payload(name === 'submit' ? event.submitter : target?.form === form && target.type === 'submit' ? target : null) === approval.payload;
        if (react) {
            approval.request = {id:expected.boundary_id, kind:'hh-react-popup',
                url:location.origin + '/applicant/vacancy_response/popup',
                fields:{_xsrf:entries()[0][1],vacancy_id:metadata.vacancy,resume_hash:metadata.hash,
                    ignore_postponed:'true',incomplete:String(metadata.incomplete),
                    mark_applicant_visible_in_vacancy_country:String(metadata.visibility),
                    country_ids:JSON.stringify(metadata.countries),letter:metadata.letter,lux:'true',withoutTest:'no',
                    hhtmFromLabel:'',hhtmSourceLabel:''}};
            Object.defineProperty(window,'__jhHHPopup_' + expected.boundary_id, {
                value:Object.freeze({fresh:() => approval.invoked === true && approval.admitted === true &&
                    !approval.blocked && approval.control?.isConnected && approval.control.form === form &&
                    submitterState(approval.control) === approval.submitterState &&
                    form.querySelector('[data-qa="vacancy-response-submit-popup"]') === approval.control &&
                    unchanged()}),writable:false,configurable:false});
        }
        return window.__jhActionBoundary.register(approval);
    }'''.replace('__IDENTITY__', SELECTED_RESUME_SCRIPT).replace('__REACT_STATE__', REACT_RESPONSE_STATE_SCRIPT).replace('__ANSWERS__', VERIFY_ANSWERS_SCRIPT).replace('__UI__', _INSPECT).replace('__CHALLENGE_SELECTOR__', json.dumps(CHALLENGE_SELECTOR)).replace('__CHALLENGE_TEXT__', json.dumps(CHALLENGE_TEXT))
    script = script.replace('/* codex:hh-submit-arm */', '/* codex:hh-submit-arm */' + RUNTIME + ';')
    try:
        ok = await session._page.evaluate(script, expected) is True
    except BaseException:
        await release_boundary(session._page, expected['boundary_id'])
        raise
    session._submit_boundary_id = expected['boundary_id']
    if not ok:
        await release_boundary(session._page, expected['boundary_id'])
    return ok


async def bind_submit_control(session, element, boundary_id=None):
    if getattr(session, '_approved_hh_payload', None) is None:
        return True
    page = session._page
    attempt = getattr(session, '_external_attempt', None)
    boundary_id = boundary_id or session._submit_boundary_id
    script = r"""(el,id) => {
        /* codex:hh-submit-control */
        const approval = window.__jhActionBoundary?.get(id);
        if (!approval || !approval.bindControl(el)) return false;
        const form = approval.form;
        const submitter = el?.form === form && el.type === 'submit' ? el : null;
        return {bound:true, request:approval.request || (form ? {
            id,
            url:submitter?.hasAttribute('formaction') ? submitter.formAction : form.action,
            method:(submitter?.hasAttribute('formmethod') ? submitter.formMethod : form.method).toUpperCase(),
            enctype:submitter?.hasAttribute('formenctype') ? submitter.formEnctype : form.enctype,
            entries:JSON.parse(approval.payload)
        } : null)};
    }"""
    receipt = (await element.evaluate(script, boundary_id) if element is not None else
               await page.evaluate('id => (' + script + ')(null,id)', boundary_id))
    if page is not session._page or getattr(session, '_external_attempt', None) is not attempt:
        return False
    if isinstance(receipt, dict) and receipt.get('bound') is True:
        descriptor = receipt.get('request')
        if isinstance(descriptor, dict) and descriptor.get('kind') == 'hh-react-popup':
            await _install_popup_gate(session, boundary_id, descriptor)
        if isinstance(descriptor, dict) and descriptor.get('id') == boundary_id and attempt is not None:
            page._hh_request_approval = {**descriptor, 'owner': attempt.owner, 'used': False}
        return True
    # Old synthetic adapters may model admission as a boolean; no request
    # proof is installed, so any real mutation still fails closed.
    return receipt is True


async def submit_boundary_passed(session):
    if getattr(session, '_approved_hh_payload', None) is None:
        return True
    boundary_id = session._submit_boundary_id
    passed = await session._page.evaluate('/* codex:hh-submit-readback */ id => { const approval=window.__jhActionBoundary?.get(id);return !!approval && !approval.blocked; }', boundary_id) is True
    return passed
