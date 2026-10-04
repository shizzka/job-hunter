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
        const valid = () => {
            const ids = identity().ids;
            if (!expected.resume_id || !ids.length || ids.some(id => id !== expected.resume_id)) return false;
            const letters = [...root.querySelectorAll('[name="letter"], [data-qa="vacancy-response-popup-form-letter-input"], textarea[data-qa*="letter"]')];
            if (expected.cover_letter && letters.length !== 1) return false;
            if (letters.some(el => el.disabled || el.value !== (expected.cover_letter || ''))) return false;
            return !expected.answers?.length || answersMatch(expected.answers);
        };
        const snapshot = () => JSON.stringify([identity(), [...root.querySelectorAll('input,textarea,select,[contenteditable="true"]')].map(el =>
            [el.tagName, el.type, el.name, el.getAttribute('data-qa'), el.getAttribute('data-codex-auto-field-id'),
             el.disabled, el.required, el.value ?? el.textContent, el.checked,
             [...(el.options || [])].map(o => [o.value,o.text,o.selected,o.disabled])])]);
        if (!valid()) return false;
        const approved = snapshot();
        document.__hhSubmitApproval = {root, valid, snapshot, approved, blocked: false};
        if (!document.__hhSubmitBoundary) {
            document.__hhSubmitBoundary = true;
            for (const name of ['click', 'submit']) document.addEventListener(name, event => {
                const approval = document.__hhSubmitApproval;
                if (!approval) return;
                const target = event.target.closest?.('button,input[type="submit"],a,[role="button"]');
                if (name === 'click' && (!target || !approval.root.contains(target))) return;
                if (name === 'submit' && event.target !== approval.root && !approval.root.contains(event.target)) return;
                if (approval.root.isConnected && approval.valid() && approval.snapshot() === approval.approved) return;
                approval.blocked = true;
                event.preventDefault(); event.stopImmediatePropagation();
            }, true);
        }
        return true;
    }'''.replace('__IDENTITY__', SELECTED_RESUME_SCRIPT).replace('__ANSWERS__', VERIFY_ANSWERS_SCRIPT)
    return await session._page.evaluate(script, expected) is True


async def submit_boundary_passed(session):
    if getattr(session, '_approved_hh_payload', None) is None:
        return True
    return await session._page.evaluate('/* codex:hh-submit-readback */ () => !document.__hhSubmitApproval?.blocked') is True
