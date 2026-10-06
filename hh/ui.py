"""Fail-closed HH dialog handling. Never answer profile/onboarding questions."""
import contextlib
import hashlib
import analytics
import json
import logging
import os
import tempfile
from pathlib import Path

from state_store.hh_ui import HHUIWarnings

log = logging.getLogger("hh_ui")

# Inspect and close share the same fresh DOM classifier. No persisted text/URL.
_INSPECT = r"""() => {
    const visible = el => [...el.getClientRects()].some(box => box.width > 0 && box.height > 0) &&
        getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
    const selectors = '[role="dialog"], dialog[open], [aria-modal="true"], [data-qa="modal-overlay"], [data-qa="whats-new-modal"]';
    const all = [...document.querySelectorAll(selectors)].filter(visible);
    const roots = all.filter(el => !all.some(other => other !== el && other.contains(el)));
    return roots.map((root, index) => {
        const norm = text => (text || '').replace(/\s+/g, ' ').trim().toLowerCase();
        const title = norm(root.querySelector('h1,h2,h3,[role="heading"]')?.textContent);
        const profileMarker = '[data-qa="applicant-profile-onboarding-modal"], [data-qa="applicant-profile-completion-modal"]';
        const optional = title === 'резюме стали компактнее'
            || (['расскажите о себе', 'заполните профиль'].includes(title) &&
                (root.matches(profileMarker) || !!root.querySelector(profileMarker)));
        const response = !!root.querySelector('form[name="vacancy_response"]') &&
            !!root.querySelector('[data-qa="vacancy-response-submit-popup"], [data-qa="vacancy-response-letter-submit"]') &&
            ![...root.querySelectorAll(selectors)].some(visible);
        // HH mounts the response resume picker in a portal OUTSIDE its form.
        // Recognize this exact radio-only surface, not arbitrary drop-base UI.
        // The shared click/submit barrier rechecks this evidence on every event.
        const forms = [...document.querySelectorAll('form[name="vacancy_response"]')].filter(visible);
        const listboxes = root.querySelectorAll('[role="listbox"][data-qa="magritte-select-option-list"]');
        const options = listboxes.length === 1
            ? [...listboxes[0].querySelectorAll('label[role="option"][data-magritte-select-option]')] : [];
        const pickerControls = [...root.querySelectorAll('input,textarea,select,button,[role="button"],[contenteditable="true"]')];
        const resumePicker = root.matches('div[role="dialog"][data-qa="drop-base"]') && !title &&
            !root.querySelector(selectors) && forms.length === 1 &&
            !!forms[0].querySelector('[data-qa="resume-title"]') &&
            !!forms[0].querySelector('[data-qa="vacancy-response-submit-popup"], [data-qa="vacancy-response-letter-submit"]') &&
            options.length > 0 && pickerControls.length === options.length &&
            options.every(option => {
                const radios = option.querySelectorAll('input[type="radio"]');
                const id = option.getAttribute('data-magritte-select-option');
                return radios.length === 1 && id && radios[0].value === id &&
                    !!option.querySelector('[data-qa="resume-title"]') &&
                    pickerControls.includes(radios[0]);
            });
        const captcha = !!root.querySelector('iframe[src*="recaptcha"], iframe[src*="hcaptcha"], [data-qa="captcha"]');
        const kind = optional ? 'optional' : (response || resumePicker) ? 'response' : captcha ? 'captcha' : 'unknown';
        const close = [...root.querySelectorAll('button,[role="button"]')].find(el => {
            const qa = el.getAttribute('data-qa') || '';
            const label = norm(el.getAttribute('aria-label') || el.getAttribute('title'));
            return visible(el) && !el.disabled && el.getAttribute('aria-disabled') !== 'true' &&
                el.type !== 'submit' && (['закрыть','close'].includes(label) ||
                ['modal-close','whats-new-modal-close','dialog-close'].includes(qa));
        });
        const controls = [...root.querySelectorAll('input,select,textarea,button,[role="button"]')]
            .map(el => [el.tagName, el.type || '', el.getAttribute('data-qa') || ''].join(':')).sort();
        const shape = JSON.stringify([kind, root.tagName, root.getAttribute('data-qa') || '',
            title.replace(/\d+/g,'#'), controls]);
        return {root, close, descriptor: {index, kind, shape, closable: !!close}};
    });
}"""
INSPECT_SCRIPT = "/* codex:hh-ui-inspect */ () => { const inspect = (" + _INSPECT + r""");
    document.__hhUIAllowed = __ALLOWED__;
    if (!document.__hhUIBarrier) {
        document.__hhUIBarrier = true;
        for (const eventName of ['click','submit']) document.addEventListener(eventName, event => {
            const blocking = inspect().filter(item => !document.__hhUIAllowed.includes(item.descriptor.kind));
            if (!blocking.length) return;
            if (eventName === 'click' && blocking.length === 1 && blocking[0].descriptor.kind === 'optional' &&
                blocking[0].close && (event.target === blocking[0].close || blocking[0].close.contains(event.target))) return;
            event.preventDefault(); event.stopImmediatePropagation();
        }, true);
    }
    return inspect().map(item => item.descriptor);
}"""
CLOSE_SCRIPT = "/* codex:hh-ui-close */ expected => { const item = (" + _INSPECT + ")()[expected.index]; " + "if (!item || item.descriptor.shape !== expected.shape || item.descriptor.kind !== 'optional' || !item.close) return false; item.close.click(); return true; }"


# Proof and navigation run in one isolated-world task while Chromium has
# disabled page scripts. Suspension lasts until the new document is confirmed.
LEAVE_RESPONSE_SCRIPT = "/* codex:hh-response-leave */ vacancyId => { const inspect = (" + _INSPECT + r""");
    const visible = el => [...el.getClientRects()].some(box => box.width > 0 && box.height > 0) &&
        getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
    const url = new URL(location.href);
    const ownedResponse = /^[0-9]+$/.test(vacancyId) && url.origin === 'https://hh.ru' &&
        ['/applicant/vacancy_response', '/applicant/vacancy_response_question'].includes(url.pathname) &&
        url.searchParams.getAll('vacancyId').length === 1 && url.searchParams.get('vacancyId') === vacancyId;
    const forms = [...document.querySelectorAll('form[name="vacancy_response"]')].filter(visible);
    const profiles = '[data-qa="applicant-profile-onboarding-modal"], [data-qa="applicant-profile-completion-modal"]';
    if ([...document.querySelectorAll(profiles)].some(visible)) return false;
    // Preserve every challenge cue recognized by HHClient, including inline
    // SmartCaptcha and challenges inside an otherwise known response form.
    if (document.querySelector('[data-qa="captcha"], iframe[src*="captcha" i], [class*="captcha" i], [id*="captcha" i]')) return false;
    const body = (document.body.innerText || '').slice(0, 3000).toLowerCase();
    if (['ddos-guard', 'проверка браузера перед переходом на hh.ru',
        'не удалось проверить ваш браузер автоматически', 'checking your browser before accessing',
        'подтвердите, что вы не робот', 'текст с картинки', "i'm not a robot", 'verify you are human',
        'проверка браузера', 'checking your browser', 'verify your browser'].some(text => body.includes(text))) return false;
    const dialogs = inspect();
    if (!ownedResponse) {
        // A normal failed vacancy page is not a cleanup candidate. Preserve
        // its existing flow; no response/modal/challenge is being discarded.
        if (/^[0-9]+$/.test(vacancyId) && url.protocol === 'https:' && !url.port &&
            !url.username && !url.password && (url.hostname === 'hh.ru' || url.hostname.endsWith('.hh.ru')) &&
            ['/vacancy/' + vacancyId, '/vacancy/' + vacancyId + '/'].includes(url.pathname) &&
            forms.length === 0 && dialogs.length === 0) return null;
        return false;
    }
    if (forms.length !== 1 || !forms[0].querySelector('[data-qa="resume-title"]') ||
        !forms[0].querySelector('[data-qa="vacancy-response-submit-popup"], [data-qa="vacancy-response-letter-submit"]')) return false;
    if (dialogs.some(item => item.descriptor.kind !== 'response')) return false;
    const modal = '[role="dialog"], dialog[open], [aria-modal="true"], [data-qa="modal-overlay"], [data-qa="whats-new-modal"]';
    for (const item of dialogs) {
        if (item.root.contains(forms[0])) {
            if (!item.root.matches('[data-qa="vacancy-response-popup"], [data-qa="vacancy-response-popup-form"]')) return false;
            if ([...item.root.querySelectorAll(modal)].some(visible)) return false;
            // A recognized marker cannot authorize other controls or content
            // beside the response form inside the same modal surface.
            const outside = [...item.root.querySelectorAll('*')].filter(el =>
                visible(el) && !forms[0].contains(el) && !el.contains(forms[0]));
            if (outside.some(el => !(el.matches('button[type="button"][data-qa="modal-close"], button[type="button"][data-qa="dialog-close"]') ||
                el.closest('button[type="button"][data-qa="modal-close"], button[type="button"][data-qa="dialog-close"]')))) return false;
        } else if (!item.root.matches('div[role="dialog"][data-qa="drop-base"]')) return false;
    }
    // Only the shared classifier's exact radio-only portal is a known picker.
    const lists = [...document.querySelectorAll('[role="listbox"]')].filter(visible);
    if (lists.some(list => !dialogs.some(item => !item.root.contains(forms[0]) &&
        item.root.matches('div[role="dialog"][data-qa="drop-base"]') && item.root.contains(list)))) return false;
    // Native isolated-world Location: no page callback or event-loop gap.
    location.replace('about:blank');
    return true;
}"""


class HHUnexpectedUI(RuntimeError):
    def __init__(self, stage, fingerprint):
        self.stage, self.fingerprint = stage, fingerprint
        super().__init__("Нестандартное поведение HH — browser flow остановлен; нужна ручная проверка")


def _fingerprint(dialogs):
    return hashlib.sha256("\n".join(sorted(item["shape"] for item in dialogs)).encode()).hexdigest()


class HHUIGuard:
    def __init__(self, home, *, notify, clock=None):
        self.home = Path(home)
        self.notify = notify
        self.warnings = HHUIWarnings(home, clock=clock)
        self.blocked = None

    async def _scan(self, page, allowed):
        dialogs = await page.evaluate(INSPECT_SCRIPT.replace("__ALLOWED__", json.dumps(list(allowed))))
        if not isinstance(dialogs, list):
            raise ValueError("Unknown HH UI inspection result")
        for index, item in enumerate(dialogs):
            if not isinstance(item, dict) or item.get("index") != index or isinstance(item.get("index"), bool):
                raise ValueError("Unknown HH UI dialog shape")
            if item.get("kind") not in {"optional", "response", "captcha", "unknown"}:
                raise ValueError("Unknown HH UI dialog kind")
            if not isinstance(item.get("shape"), str) or not item["shape"] or not isinstance(item.get("closable"), bool):
                raise ValueError("Unknown HH UI dialog evidence")
        return dialogs

    async def ensure(self, page, stage, *, allowed=()):
        if self.blocked is not None:
            raise self.blocked
        dialogs = []
        changed = False
        try:
            for _ in range(4):
                dialogs = await self._scan(page, allowed)
                blocking = [item for item in dialogs if item["kind"] not in allowed]
                if not blocking:
                    return changed
                # Never dismiss a known popup to work around another unknown UI.
                if any(item["kind"] != "optional" or not item["closable"] for item in blocking):
                    break
                closed = await page.evaluate(CLOSE_SCRIPT, blocking[0])
                if closed is not True:
                    break
                changed = True
                # Refetch, not stale element handles; a changed/new modal is reclassified.
                await page.wait_for_timeout(100)
            else:
                dialogs = await self._scan(page, allowed)
        except Exception as exc:
            log.warning("HH UI inspection failed: %s", type(exc).__name__)
            dialogs = [{"shape": "inspection_failed", "kind": "unknown"}]
        if not any(item["kind"] not in allowed for item in dialogs):
            return changed
        fingerprint = _fingerprint(dialogs)
        self.blocked = HHUnexpectedUI(stage, fingerprint)
        context = analytics.current_context()
        if context.get("channel") == "chat" or "chat" in stage:
            observation_stage = "chat"
        elif context.get("stage") == "apply" or stage.startswith(("apply", "response", "resume_preflight", "dom_submit", "verified_dom_submit", "answer_questions", "click:")):
            observation_stage = "apply"
        elif context.get("run_id") or stage.startswith(("search", "details", "vacancy_details")):
            observation_stage = "search"
        else:
            observation_stage = "other"
        try:
            self.warnings.observe(fingerprint, observation_stage, context.get("run_id", ""))
        except Exception as exc:
            log.warning("HH UI observation persistence failed: %s", type(exc).__name__)
        analytics.record_unexpected_ui(fingerprint, observation_stage)
        try:
            attempt = self.warnings.claim_notification(fingerprint)
        except Exception as exc:
            log.warning("HH UI warning persistence failed; delivery suppressed: %s", type(exc).__name__)
            raise self.blocked from None
        if attempt:
            status = "uncertain"
            try:
                with tempfile.TemporaryDirectory(prefix=".hh-ui-", dir=self.home) as directory:
                    photo = Path(directory) / "unexpected-ui.png"
                    try:
                        fd = os.open(photo, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                        os.close(fd)
                        await page.screenshot(path=str(photo), full_page=False)
                        photo_path = str(photo)
                    except Exception as exc:
                        log.warning("HH UI screenshot failed: %s", type(exc).__name__)
                        photo_path = None
                    status = "sent" if await self.notify(photo_path, stage, fingerprint) else "failed"
            except Exception as exc:
                log.warning("HH UI warning delivery failed: %s", type(exc).__name__)
                status = "failed"
            finally:
                try:
                    self.warnings.finish(fingerprint, attempt, status)
                except Exception as exc:
                    log.warning("HH UI warning completion uncertain: %s", type(exc).__name__)
        raise self.blocked


async def ensure_session_ui(session, stage, *, allowed=()):
    guard = getattr(session, "_ensure_expected_ui", None)
    if guard is not None:
        return await guard(stage, allowed=allowed)


async def ensure_page_ui(page, stage):
    guard = getattr(page, "_hh_ui_guard", None)
    if guard is not None:
        await guard.ensure(page, stage, allowed=("captcha",))


def _cleanup_action_seen(session, attempt):
    """Check both the live owner and durable state; uncertainty is sticky."""
    seen = (getattr(session, "_external_attempt", None) is not attempt or
            attempt.acting or getattr(attempt, "uncertain", False))
    repository = getattr(attempt, "repository", None)
    if repository is not None:
        state = repository.get(attempt.url)
        seen = seen or state.get("owner") != attempt.owner or state.get("status") in {"acting", "uncertain"}
    if seen:
        attempt.uncertain = True
        attempt.proven_no_action = False
    return seen


def _block_cleanup(guard, reason):
    # Never call ensure() here: its optional-popup path can click a close button.
    if guard.blocked is None:
        guard.blocked = HHUnexpectedUI("response_cleanup", hashlib.sha256(reason.encode()).hexdigest())


async def leave_known_response_ui(session, vacancy_id: str) -> bool:
    """Discard only a proven unsent response, without executing page lifecycle JS."""
    guard = getattr(session, "_ui_guard", None)
    page = getattr(session, "_page", None)
    attempt = getattr(session, "_external_attempt", None)
    if not isinstance(guard, HHUIGuard) or page is None or attempt is None:
        return False
    if guard.blocked is not None:
        raise guard.blocked
    if _cleanup_action_seen(session, attempt):
        _block_cleanup(guard, "cleanup_action_state")
        return False
    cdp = None
    suspended = False
    observed_action = False
    confirmed = False
    listening = False
    # These are signals, never dispatches. Include native form navigation and
    # page network side effects, even if an owner forgot to set acting.
    def action_signal(request):
        nonlocal observed_action
        observed_action = True
    def safe():
        if observed_action:
            attempt.uncertain = True
            attempt.proven_no_action = False
        if _cleanup_action_seen(session, attempt):
            _block_cleanup(guard, "cleanup_action_state")
            return False
        return guard.blocked is None
    try:
        page.on("request", action_signal)
        listening = True
        cdp = await page.context.new_cdp_session(page)
        # Suspend before proof. Timers, microtasks, mutation observers and
        # beforeunload/unload handlers cannot run between proof and commit.
        await cdp.send("Emulation.setScriptExecutionDisabled", {"value": True})
        suspended = True
        if not safe():
            return False
        tree = (await cdp.send("Page.getFrameTree"))["frameTree"]
        if not safe():
            return False
        if tree.get("childFrames"):
            _block_cleanup(guard, "cleanup_child_frame")
            return False
        frame_id = tree["frame"]["id"]
        previous_loader = tree["frame"]["loaderId"]
        world = await cdp.send("Page.createIsolatedWorld", {
            "frameId": frame_id, "worldName": "hh-response-cleanup"})
        if not safe():
            return False
        # Main-world listeners are inspected by the debugger, without invoking
        # them. Refuse rather than discard UI that lifecycle code could change.
        window = await cdp.send("Runtime.evaluate", {"expression": "window"})
        listeners = await cdp.send("DOMDebugger.getEventListeners", {
            "objectId": window["result"]["objectId"]})
        if not safe():
            return False
        if any(item["type"] in {"beforeunload", "unload", "pagehide", "visibilitychange"}
               for item in listeners["listeners"]):
            _block_cleanup(guard, "cleanup_lifecycle_handler")
            return False
        document = await cdp.send("Runtime.evaluate", {"expression": "document"})
        listeners = await cdp.send("DOMDebugger.getEventListeners", {
            "objectId": document["result"]["objectId"], "depth": -1, "pierce": True})
        if not safe():
            return False
        if any(item["type"] in {"beforeunload", "unload", "pagehide", "visibilitychange"}
               for item in listeners["listeners"]):
            _block_cleanup(guard, "cleanup_lifecycle_handler")
            return False
        proof = await cdp.send("Runtime.evaluate", {
            "expression": "(" + LEAVE_RESPONSE_SCRIPT + ")(" + json.dumps(vacancy_id) + ")",
            "contextId": world["executionContextId"], "returnByValue": True})
        if not safe():
            return False
        if not proof.get("exceptionDetails") and proof.get("result", {}).get("subtype") == "null":
            return False
        if proof.get("exceptionDetails") or proof.get("result", {}).get("value") is not True:
            _block_cleanup(guard, "cleanup_unproven_dom")
            return False
        await page.wait_for_url("about:blank", wait_until="domcontentloaded", timeout=5000)
        if not safe():
            return False
        post = (await cdp.send("Page.getFrameTree"))["frameTree"]
        if not safe():
            return False
        if (post.get("childFrames") or post["frame"].get("url") != "about:blank" or
                post["frame"].get("id") != frame_id or not post["frame"].get("loaderId") or
                post["frame"]["loaderId"] == previous_loader):
            _block_cleanup(guard, "cleanup_unproven_document")
            return False
        confirmed = True
    except Exception as exc:
        safe()
        _block_cleanup(guard, "cleanup_inspection_failed")
        log.warning("HH response cleanup not confirmed: %s", type(exc).__name__)
        return False
    finally:
        if cdp is not None:
            try:
                if suspended:
                    await cdp.send("Emulation.setScriptExecutionDisabled", {"value": False})
            except Exception as exc:
                confirmed = False
                _block_cleanup(guard, "cleanup_restore_failed")
                log.warning("HH response cleanup suspension not released: %s", type(exc).__name__)
            finally:
                with contextlib.suppress(Exception):
                    await cdp.detach()
        safe()
        if listening:
            page.remove_listener("request", action_signal)
    return confirmed and safe()
