"""Local browser commit primitive: wait without dispatch, then validate and act.

Payload validators remain in their source modules. Bindings belong to a nonce
and a document; a Locator is pinned to its original element before it waits.
Only an explicit refusal before calling page code proves zero dispatch.
"""
import asyncio
import contextlib
import time

from playwright.async_api import Error, Locator

RUNTIME = r'''(() => {
    if (window.__jhActionBoundary) return true;
    const bindings = new Map(), fences = new Set();
    let active = null, listening = false, navigationFence = true;
    const nativeFormData = FormData;
    const nativeClick = HTMLElement.prototype.click;
    const nativeRequestSubmit = HTMLFormElement.prototype.requestSubmit;
    const nativeSubmit = HTMLFormElement.prototype.submit;
    const nativeDispatch = EventTarget.prototype.dispatchEvent;
    const externalControl = el => !!el && (el.matches?.(
        '[data-qa="chatik-do-send-message"],[data-qa*="vacancy-response"],[data-qa*="response-link"],button.f-test-vacancy-response-button,button.f-test-button-Otkliknutsya') ||
        /(откликнуться|отправить|submit|verzenden|send|envoyer|senden)/i.test((el.innerText || el.value || '').trim()));
    const externalForm = el => el?.matches?.('form[name="vacancy_response"],form[data-vacancy-id]') ||
        [...(el?.querySelectorAll?.('button,input,[role="button"]') || [])].some(externalControl);
    const actionable = el => {
        if (!el?.isConnected || el.matches(':disabled')) return false;
        const box = el.getBoundingClientRect(), style = getComputedStyle(el);
        if (!box.width || !box.height || style.visibility !== 'visible') return false;
        const x = (Math.max(0,box.left)+Math.min(innerWidth,box.right))/2;
        const y = (Math.max(0,box.top)+Math.min(innerHeight,box.bottom))/2;
        return el.contains(document.elementFromPoint(x,y));
    };
    const stop = event => {event.preventDefault();event.stopImmediatePropagation();};
    const filesMatch = binding => !binding.files?.some(({el,files})=>el.files.length!==files.length || files.some((file,i)=>el.files[i]!==file));
    const submittingForm = binding => binding.form || binding.root;
    const candidates = (name,event) => [...bindings.values()].filter(binding =>
        binding.control !== undefined && (name === 'submit' ? submittingForm(binding) === event.target :
        binding.control?.contains(event.target) || binding.root.contains(event.target)));
    const onEvent = event => {
        const name = event.type;
        const matches = candidates(name,event);
        const binding = active ? bindings.get(active) : matches.length === 1 ? matches[0] : null;
        if (!binding && !matches.length) {
            const target = event.target.closest?.('button,input,a,[role="button"],[data-qa]');
            if ((navigationFence || bindings.size) && (name === 'submit' ? externalForm(event.target) : externalControl(target))) stop(event);
            return;
        }
        if (!binding || !matches.includes(binding) || !filesMatch(binding) || !binding.eventMatches(name,event,
                name === 'click' ? binding.control?.contains(event.target) ? binding.control : event.target : event.submitter)) {
            if (binding) binding.blocked = true;
            stop(event);return;
        }
        binding.admitted = true;
    };
    const listen = () => {
        if (listening) return;
        listening = true;
        for (const name of ['click','submit']) window.addEventListener(name,onEvent,true);
    };
    const permittedSubmit = (form,submitter) => {
        const matches = [...bindings.values()].filter(binding => binding.control !== undefined && submittingForm(binding) === form);
        const binding = active ? bindings.get(active) : matches.length === 1 ? matches[0] : null;
        if (!binding && !matches.length) return !((navigationFence || bindings.size) && externalForm(form));
        const event = {type:'submit',target:form,submitter:submitter || null};
        if (!binding || submittingForm(binding) !== form || !filesMatch(binding) || !binding.eventMatches('submit',event,event.submitter)) {
            if (binding) binding.blocked = true;
            return false;
        }
        return true;
    };
    // Validate the actual submitter before requestSubmit can invoke any page
    // submit handler, including window capture handlers registered earlier.
    HTMLFormElement.prototype.requestSubmit = function(submitter) {
        if (!permittedSubmit(this,submitter)) return;
        return nativeRequestSubmit.call(this,submitter);
    };
    HTMLFormElement.prototype.submit = function() {
        if (!permittedSubmit(this,null)) return;
        return nativeSubmit.call(this);
    };
    EventTarget.prototype.dispatchEvent = function(event) {
        if (event.type === 'submit' && this instanceof HTMLFormElement && !permittedSubmit(this,event.submitter)) return false;
        return nativeDispatch.call(this,event);
    };
    const release = id => {
        const binding = bindings.get(id);
        fences.delete(id);
        bindings.delete(id);
        if (!bindings.size && !fences.size) {
            navigationFence = false;
            // Keep the document-start trampoline in its original position;
            // with no binding/fence it is disarmed and unrelated events pass.
        }
    };
    const api = Object.freeze({
        register(binding) {
            if (!binding.id || bindings.has(binding.id)) return false;
            const form=submittingForm(binding);
            const fields=form.tagName==='FORM' ? [...form.elements] : [...binding.root.querySelectorAll('input[type=file]')];
            binding.files=fields.filter(el=>el.matches('input[type=file]')).map(el=>({el,files:[...el.files]}));
            bindings.set(binding.id,binding);navigationFence = false;listen();return true;
        },
        formData(form,submitter) {
            // FormData(form) fires formdata handlers. Read successful controls
            // on an inert document copy so validation cannot dispatch page JS.
            if ([...form.elements].some(el=>el.tagName.includes('-'))) throw new Error('Unsupported successful control');
            const copy = document.implementation.createHTMLDocument();
            copy.replaceChild(copy.importNode(document.documentElement,true),copy.documentElement);
            const original = [...document.querySelectorAll('*')], cloned = [...copy.querySelectorAll('*')];
            if (original.length !== cloned.length) throw new Error('Unsupported form clone');
            for (let i=0;i<original.length;i++) {
                const el=original[i], twin=cloned[i];
                for (const attribute of [...twin.attributes]) if (/^on/i.test(attribute.name)) twin.removeAttribute(attribute.name);
                if (el.tagName==='INPUT') {
                    if (el.type==='file') twin.files=el.files;
                    else twin.value=el.value;
                    twin.checked=el.checked;
                } else if (el.tagName==='TEXTAREA') twin.value=el.value;
                else if (el.tagName==='SELECT') [...el.options].forEach((option,j)=>twin.options[j].selected=option.selected);
            }
            const index=original.indexOf(form), control=submitter ? cloned[original.indexOf(submitter)] : undefined;
            if (index<0 || (submitter && !control)) throw new Error('Unsupported successful control');
            return new nativeFormData(cloned[index],control);
        },
        get: id => bindings.get(id),
        fence: id => {fences.add(id);navigationFence = true;listen();},
        release,
        dispatch(id,control,mode='click') {
            const binding = bindings.get(id);
            if (!binding || binding.invoked) return {id,ok:false};
            const event = {type:mode === 'click' ? 'click' : 'submit',target:mode === 'click' ? control : submittingForm(binding),submitter:control};
            if (!binding || binding.control !== control || binding.root.ownerDocument !== document ||
                !binding.root.isConnected || (control && (mode === 'click' ? !actionable(control) : !control.isConnected || control.matches(':disabled'))) ||
                !filesMatch(binding) ||
                !binding.eventMatches(event.type,event,control)) return {id,dispatched:false,ok:false};
            // No awaits/event-loop handoff between this check and invoking page
            // code. Once invoked, no later listener can prove zero dispatch.
            binding.invoked = true;
            active = id;
            try {
                if (mode === 'click') nativeClick.call(control);
                else if (typeof submittingForm(binding).requestSubmit === 'function') submittingForm(binding).requestSubmit(control || undefined);
                else return {id,dispatched:false,ok:false};
                return {id,dispatched:true,ok:!binding.blocked};
            } finally {active = null;}
        }
    });
    Object.defineProperty(window,'__jhActionBoundary',{value:api,configurable:false,writable:false});
    listen();
    return true;
})()'''


async def bootstrap_boundary(page):
    """Install before site scripts; entry controls remain usable until an approval."""
    await page.add_init_script(script=RUNTIME.replace('navigationFence = true', 'navigationFence = false', 1))


async def install_boundary(page, boundary_id):
    """An attempt owns its removable document-start fence, including navigation."""
    pending = getattr(page, '_jh_boundary_fences', None)
    if pending is None:
        pending = {}
        page._jh_boundary_fences = pending
    session = await page.context.new_cdp_session(page)
    try:
        await session.send('Page.enable')
        import json
        source = RUNTIME + ';window.__jhActionBoundary.fence(' + json.dumps(boundary_id) + ');'
        result = await session.send('Page.addScriptToEvaluateOnNewDocument', {'source':source})
        pending[boundary_id] = (session, result['identifier'])
    except BaseException:
        await session.detach()
        raise


async def dispatch_approved(page, control, boundary_id, *, timeout=10000, mode='click', on_no_action=None):
    """One commit command. Exceptions/lost results after invocation remain ambiguous."""
    dispatch_started = False
    try:
        # Pin before Locator auto-wait; never resolve an action on a new document.
        element = await control.element_handle(timeout=timeout) if isinstance(control, Locator) else control
        if mode == 'click':
            await asyncio.wait_for(wait_for_actionable(element, timeout=timeout), timeout/1000)
        dispatch_started = True
        command = (element.evaluate(r'''(el,args) => {
            /* codex:action-dispatch */
            return window.__jhActionBoundary?.dispatch(args.id,el,args.mode);
        }''', {'id':boundary_id,'mode':mode}) if element is not None else page.evaluate(r'''args => {
            /* codex:action-dispatch */
            return window.__jhActionBoundary?.dispatch(args.id,null,args.mode);
        }''', {'id':boundary_id,'mode':mode}))
        receipt = await asyncio.wait_for(command, timeout/1000)
        if not isinstance(receipt, dict) or receipt.get('id') != boundary_id:
            return False
        if receipt.get('dispatched') is False and on_no_action is not None:
            on_no_action()
        return receipt.get('ok') is True
    except (Error, asyncio.TimeoutError):
        if not dispatch_started and on_no_action is not None:
            # Preparation contains no pointer/key/submit command.
            on_no_action()
        return False
    except asyncio.CancelledError:
        if not dispatch_started and on_no_action is not None:
            on_no_action()
        raise
    finally:
        await release_boundary(page, boundary_id)


async def release_boundary(page, boundary_id):
    with contextlib.suppress(Exception):
        await page.evaluate('/* codex:action-disarm */ id => window.__jhActionBoundary?.release(id)', boundary_id)
    fence = getattr(page, '_jh_boundary_fences', {}).pop(boundary_id, None)
    if fence:
        session, identifier = fence
        try:
            with contextlib.suppress(Exception):
                await session.send('Page.removeScriptToEvaluateOnNewDocument', {'identifier':identifier})
        finally:
            with contextlib.suppress(Exception):
                await session.detach()


async def wait_for_actionable(element, *, timeout):
    """No mouse/key events: even Playwright trial clicks can reach earlier listeners."""
    deadline = time.monotonic() + timeout / 1000
    remaining = lambda: max(1, int((deadline - time.monotonic()) * 1000))
    for state in ('visible', 'stable', 'enabled'):
        await element.wait_for_element_state(state, timeout=remaining())
    await element.scroll_into_view_if_needed(timeout=remaining())
    while time.monotonic() < deadline:
        if await element.evaluate(r"""el => {
            /* codex:action-ready */
            const box=el.getBoundingClientRect(),style=getComputedStyle(el);
            const x=(Math.max(0,box.left)+Math.min(innerWidth,box.right))/2;
            const y=(Math.max(0,box.top)+Math.min(innerHeight,box.bottom))/2;
            return el.isConnected && !el.matches(':disabled') && box.width>0 && box.height>0 &&
                style.visibility==='visible' && el.contains(document.elementFromPoint(x,y));
        }""") is True:
            return
        await asyncio.sleep(min(.05, max(0, deadline-time.monotonic())))
    raise Error('Approved element did not become actionable')
