"""Recheck approved native payload synchronously at browser click/submit events."""

async def arm_submit_boundary(session):
    expected = getattr(session, '_approved_hh_payload', None)
    if expected is None:
        return True  # Non-application controls have their existing UI guards.
    from hh.apply import SELECTED_RESUME_SCRIPT
    from hh.forms import VERIFY_ANSWERS_SCRIPT
    script = r'''expected => {
        /* codex:hh-submit-arm */
        const identity = (__IDENTITY__);
        const answersMatch = (__ANSWERS__);
        const visible = el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
        const forms = [...document.querySelectorAll('form[name="vacancy_response"]')].filter(visible);
        const dialogs = [...document.querySelectorAll('[role="dialog"]')].filter(el => visible(el) &&
            el.querySelector('[data-qa="vacancy-response-submit-popup"], [data-qa="vacancy-response-letter-submit"]'));
        const root = forms.length === 1 ? forms[0] : forms.length === 0 && dialogs.length === 1 ? dialogs[0] : null;
        if (!root) return false;
        const form = root.tagName === 'FORM' ? root : null;
        const controls = () => [...new Set([
            ...root.querySelectorAll('input,textarea,select,[contenteditable="true"]'),
            ...(form ? [...form.elements].filter(el => el.matches('input,textarea,select')) : [])
        ])];
        const entries = submitter => form ? [...new FormData(form, submitter || undefined).entries()]
            .map(([key,value]) => [key, typeof value === 'string' ? value :
                ['file',value.name,value.size,value.type,value.size ? value.lastModified : 0]]) : [];
        const payload = submitter => JSON.stringify(entries(submitter));
        const valid = () => {
            const ids = identity().ids.concat(entries(null)
                .filter(([key]) => ['resume_id','resumeId','resumeHash','resume'].includes(key)).map(([,value]) => value));
            if (!expected.resume_id || !ids.length || ids.some(id => id !== expected.resume_id)) return false;
            const letters = controls().filter(el => !el.matches(':disabled') && el.matches(
                '[name="letter"], [data-qa="vacancy-response-popup-form-letter-input"], textarea[data-qa*="letter"]'));
            if (expected.cover_letter && letters.length !== 1) return false;
            if (letters.some(el => el.value !== (expected.cover_letter || ''))) return false;
            return !expected.answers?.length || answersMatch(expected.answers);
        };
        const snapshot = () => JSON.stringify([identity(),
            form ? [form.id,form.action,form.method,form.enctype,form.target] : null, payload(null), controls().map(el =>
            [el.tagName, el.type, el.name, el.getAttribute('form'), el.getAttribute('data-qa'), el.getAttribute('data-codex-auto-field-id'),
             el.disabled, el.required, el.value ?? el.textContent, el.checked,
             [...(el.options || [])].map(o => [o.value,o.text,o.selected,o.disabled])])]);
        if (!valid()) return false;
        const approved = snapshot(), approvedControls = controls();
        const unchanged = () => {
            const current = controls();
            return root.isConnected && current.length === approvedControls.length &&
                current.every((el,index) => el === approvedControls[index]) && valid() && snapshot() === approved;
        };
        const submitterState = el => el ? JSON.stringify([el.name,el.value,el.type,el.getAttribute('form'),
            el.getAttribute('formaction'),el.getAttribute('formmethod'),el.getAttribute('formenctype'),el.getAttribute('formtarget')]) : null;
        const approval = {root, valid, snapshot, approved, unchanged, blocked: false, payload: payload(null)};
        approval.bindControl = el => {
            if (!unchanged() || (el && (!el.isConnected || !(root.contains(el) || (form && el.form === form))))) return false;
            approval.control = el;
            approval.submitterState = submitterState(el);
            approval.payload = payload(el?.form === form && el.type === 'submit' ? el : null);
            return true;
        };
        approval.eventMatches = (name,event,target) => unchanged() &&
            (name !== 'click' || submitterState(target) === approval.submitterState) &&
            (name !== 'submit' || !approval.control || event.submitter === approval.control) &&
            payload(name === 'submit' ? event.submitter : target?.form === form && target.type === 'submit' ? target : null) === approval.payload;
        document.__hhSubmitApproval = approval;
        if (!document.__hhSubmitBoundary) {
            document.__hhSubmitBoundary = true;
            for (const name of ['click', 'submit']) document.addEventListener(name, event => {
                const approval = document.__hhSubmitApproval;
                if (!approval) return;
                const target = event.target.closest?.('button,input[type="submit"],a,[role="button"]');
                if (name === 'click' && (!target || target !== approval.control)) return;
                const belongs = name === 'click' ? approval.root.contains(target) || target.form === approval.root :
                    event.target === approval.root || approval.root.contains(event.target);
                if (belongs && approval.eventMatches(name,event,target)) return;
                approval.blocked = true;
                event.preventDefault(); event.stopImmediatePropagation();
            }, true);
        }
        return true;
    }'''.replace('__IDENTITY__', SELECTED_RESUME_SCRIPT).replace('__ANSWERS__', VERIFY_ANSWERS_SCRIPT)
    return await session._page.evaluate(script, expected) is True


async def bind_submit_control(session, element):
    if getattr(session, '_approved_hh_payload', None) is None:
        return True
    return await element.evaluate(r"""el => {
        /* codex:hh-submit-control */
        const approval = document.__hhSubmitApproval;
        return !!approval && approval.bindControl(el);
    }""") is True


async def submit_boundary_passed(session):
    if getattr(session, '_approved_hh_payload', None) is None:
        return True
    return await session._page.evaluate('/* codex:hh-submit-readback */ () => !document.__hhSubmitApproval?.blocked') is True
