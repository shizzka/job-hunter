"""Recheck approved native payload synchronously at browser click/submit events."""

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
        const valid = (submitter = null) => {
            const actual = entries(submitter);
            const successfulIds = actual.filter(([key]) => ['resume_id','resumeId','resumeHash','resume'].includes(key)).map(([,value]) => value);
            if (form && !successfulIds.length) return false;
            const ids = identity().ids.concat(successfulIds);
            if (!expected.resume_id || !ids.length || ids.some(id => id !== expected.resume_id)) return false;
            const letters = controls().filter(el => !el.matches(':disabled') && el.matches(
                '[name="letter"], [data-qa="vacancy-response-popup-form-letter-input"], textarea[data-qa*="letter"]'));
            if (expected.cover_letter && letters.length !== 1) return false;
            if (letters.some(el => el.value !== (expected.cover_letter || ''))) return false;
            const letterNames = new Set(['letter', ...letters.map(el => el.name).filter(Boolean)]);
            const successfulLetters = actual.filter(([key]) => letterNames.has(key));
            if (form && expected.cover_letter && successfulLetters.length !== 1) return false;
            if (successfulLetters.some(([,value]) => value !== (expected.cover_letter || ''))) return false;
            return !expected.answers?.length || answersMatch(expected.answers);
        };
        const snapshot = () => JSON.stringify([identity(),
            form ? [form.id,form.action,form.method,form.enctype,form.target] : null, payload(null), controls().map(el =>
            [el.tagName, el.type, el.name, el.getAttribute('form'), el.getAttribute('data-qa'), el.getAttribute('data-codex-auto-field-id'),
             el.matches('input[type=button],input[type=submit]') ? null : el.disabled, el.required, el.value ?? el.textContent, el.checked,
             [...(el.options || [])].map(o => [o.value,o.text,o.selected,o.disabled])])]);
        if (!valid()) return false;
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
        return window.__jhActionBoundary.register(approval);
    }'''.replace('__IDENTITY__', SELECTED_RESUME_SCRIPT).replace('__ANSWERS__', VERIFY_ANSWERS_SCRIPT).replace('__UI__', _INSPECT).replace('__CHALLENGE_SELECTOR__', json.dumps(CHALLENGE_SELECTOR)).replace('__CHALLENGE_TEXT__', json.dumps(CHALLENGE_TEXT))
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
        return {bound:true, request:form ? {
            id,
            url:submitter?.hasAttribute('formaction') ? submitter.formAction : form.action,
            method:(submitter?.hasAttribute('formmethod') ? submitter.formMethod : form.method).toUpperCase(),
            enctype:submitter?.hasAttribute('formenctype') ? submitter.formEnctype : form.enctype,
            entries:JSON.parse(approval.payload)
        } : null};
    }"""
    receipt = (await element.evaluate(script, boundary_id) if element is not None else
               await page.evaluate('id => (' + script + ')(null,id)', boundary_id))
    if page is not session._page or getattr(session, '_external_attempt', None) is not attempt:
        return False
    if isinstance(receipt, dict) and receipt.get('bound') is True:
        descriptor = receipt.get('request')
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
