"""Fail-closed HH dialog handling. Never answer profile/onboarding questions."""
import hashlib
import analytics
import json
import logging
import os
import tempfile
from pathlib import Path

from state_store.hh_ui import HHUIWarnings

log = logging.getLogger("hh_ui")

# Shared by passive recovery and synchronous action admission. A challenge is
# a page-wide stop signal, including inline/hidden widgets without a modal.
CHALLENGE_SELECTOR = ('iframe[src*="captcha" i], [data-qa="captcha"], '
                      '[class*="captcha" i], [id*="captcha" i]')
CHALLENGE_TEXT = ('ddos-guard', 'проверка браузера перед переходом на hh.ru',
                  'не удалось проверить ваш браузер автоматически',
                  'checking your browser before accessing', 'подтвердите, что вы не робот',
                  'текст с картинки', "i'm not a robot", 'verify you are human',
                  'проверка браузера', 'checking your browser', 'verify your browser')

# Inspect and close share the same fresh DOM classifier. No persisted text/URL.
_INSPECT = r"""() => {
    const visible = el => [...el.getClientRects()].some(box => box.width > 0 && box.height > 0) &&
        getComputedStyle(el).visibility !== 'hidden' && getComputedStyle(el).display !== 'none';
    const selectors = '[role="dialog"], dialog[open], [aria-modal="true"], [data-qa="modal-overlay"], [data-qa="whats-new-modal"], [data-qa="applicant-profile-onboarding-modal"], [data-qa="applicant-profile-completion-modal"]';
    const all = [...document.querySelectorAll(selectors)].filter(visible);
    const roots = all.filter(el => !all.some(other => other !== el && other.contains(el)));
    const norm = text => (text || '').replace(/\s+/g, ' ').trim().toLowerCase();
    const closeSelector = 'button[type="button"][data-qa="modal-close"], button[type="button"][data-qa="dialog-close"]';
    // A known descendant never authorizes additional content on its wrapper.
    // Inspect text nodes too: querySelectorAll alone misses bare consent text.
    const covered = (root, surfaces, allowedClose = false) => {
        const inside = el => surfaces.some(surface => surface === el || surface.contains(el)) ||
            (allowedClose && !!el.closest(closeSelector) && root.contains(el.closest(closeSelector)));
        if ([...root.querySelectorAll('*')].some(el => visible(el) && !inside(el) &&
                !surfaces.some(surface => el.contains(surface)))) return false;
        const texts = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
        while (texts.nextNode()) {
            const node = texts.currentNode;
            if (norm(node.textContent) && visible(node.parentElement) && !inside(node.parentElement)) return false;
        }
        return true;
    };
    const forms = [...document.querySelectorAll('form[name="vacancy_response"]')].filter(visible);
    const path = location.pathname.toLowerCase();
    const globalChallenge = path.includes('captcha') || !!document.querySelector(__CHALLENGE_SELECTOR__) ||
        __CHALLENGE_TEXT__.some(cue => norm(document.body?.innerText).slice(0, 3000).includes(cue));
    const authLost = path.includes('/account/login') || path.includes('/auth/') ||
        /"userType"\s*:\s*"anonymous"|"luxPageName"\s*:\s*"ForbiddenPage"/i.test(document.documentElement?.innerHTML || '');
    const globalStopCount = Number(globalChallenge) + Number(authLost);
    const incidents = roots.map((root, index) => {
        const nested = [...root.querySelectorAll(selectors)].filter(visible);
        // Preserve HH's inert overlay around one known dialog, without using
        // it to hide a second dialog or any sibling controls/content.
        const overlay = root.matches('[data-qa="modal-overlay"]') &&
            nested.length === 1 && covered(root, nested);
        const surface = overlay ? nested[0] : root;
        const noNested = ![...surface.querySelectorAll(selectors)].some(visible);
        const title = norm(surface.querySelector('h1,h2,h3,[role="heading"]')?.textContent);
        const qa = surface.getAttribute('data-qa') || '';
        const surfaceForms = [...surface.querySelectorAll('form')].filter(visible);
        const close = [...surface.querySelectorAll('button,[role="button"]')].find(el => {
            const qa = el.getAttribute('data-qa') || '';
            const label = norm(el.getAttribute('aria-label') || el.getAttribute('title'));
            return visible(el) && !el.disabled && el.getAttribute('aria-disabled') !== 'true' &&
                el.type !== 'submit' && (['закрыть','close'].includes(label) ||
                ['modal-close','whats-new-modal-close','dialog-close'].includes(qa));
        });
        // Profile/onboarding surfaces require manual handling even when their
        // title is known. Only this informational notice may be dismissed.
        const optionalMarker = !qa || qa === 'whats-new-modal';
        const heading = surface.querySelector('h1,h2,h3,[role="heading"]');
        const optionalButtons = [...surface.querySelectorAll('button,[role="button"]')];
        const optional = noNested && optionalMarker && title === 'резюме стали компактнее' && close &&
            !surface.querySelector('form,input,textarea,select,[contenteditable]:not([contenteditable="false"])') &&
            optionalButtons.every(control => control === close) && covered(surface, [heading, close]);
        const form = surfaceForms.length === 1 ? surfaceForms[0] : null;
        const responseMarker = ['vacancy-response-popup', 'vacancy-response-popup-form'].includes(qa) ||
            (!qa && form?.parentElement === surface);
        // Native dialogs may embed an unnamed form; its actual successful
        // controls still undergo exact resume/payload boundary validation.
        const responseForm = form && (form.getAttribute('name') === 'vacancy_response'
            ? forms.length === 1 && forms[0] === form : !form.getAttribute('name') && forms.length === 0);
        const response = noNested && responseMarker && responseForm &&
            !!form.querySelector('[data-qa="vacancy-response-submit-popup"], [data-qa="vacancy-response-letter-submit"]') &&
            covered(surface, [form], true);
        // The resume picker is a separate radio-only portal, scoped to one
        // response form. Extra text or controls remain unknown UI.
        const listboxes = surface.querySelectorAll('[role="listbox"][data-qa="magritte-select-option-list"]');
        const options = listboxes.length === 1
            ? [...listboxes[0].querySelectorAll('label[role="option"][data-magritte-select-option]')] : [];
        const pickerControls = [...surface.querySelectorAll('input,textarea,select,button,[role="button"],[contenteditable="true"]')];
        const resumePicker = surface.matches('div[role="dialog"][data-qa="drop-base"]') && !title &&
            noNested && forms.length === 1 &&
            !!forms[0].querySelector('[data-qa="resume-title"]') &&
            !!forms[0].querySelector('[data-qa="vacancy-response-submit-popup"], [data-qa="vacancy-response-letter-submit"]') &&
            options.length > 0 && pickerControls.length === options.length && covered(surface, options) &&
            options.every(option => {
                const radios = option.querySelectorAll('input[type="radio"]');
                const id = option.getAttribute('data-magritte-select-option');
                return radios.length === 1 && id && radios[0].value === id &&
                    !!option.querySelector('[data-qa="resume-title"]') &&
                    pickerControls.includes(radios[0]);
            });
        const captchaSelector = '[data-qa="captcha"], iframe[src*="captcha" i], [class*="captcha" i], [id*="captcha" i]';
        const captcha = root.matches(captchaSelector) || !!root.querySelector(captchaSelector) ||
            ['ddos-guard', 'подтвердите, что вы не робот', 'verify you are human',
                'checking your browser', 'проверка браузера'].some(cue => norm(root.innerText).includes(cue));
        const kind = captcha ? 'captcha' : optional ? 'optional' : (response || resumePicker) ? 'response' : 'unknown';
        const controls = [...root.querySelectorAll('input,select,textarea,button,[role="button"]')]
            .map(el => [el.tagName, el.type || '', el.getAttribute('data-qa') || ''].join(':')).sort();
        const shape = JSON.stringify([kind, root.tagName, root.getAttribute('data-qa') || '',
            title.replace(/\d+/g,'#'), controls]);
        return {root, close, descriptor: {index: index + globalStopCount, kind, shape, closable: !!close}};
    });
    if (authLost) incidents.unshift({root: document.documentElement, close: null,
        descriptor: {index: Number(globalChallenge), kind: 'unknown', shape: 'auth:session-lost', closable: false}});
    if (globalChallenge) incidents.unshift({root: document.documentElement, close: null,
        descriptor: {index: 0, kind: 'captcha', shape: 'captcha:global-challenge', closable: false}});
    return incidents;
}""".replace("__CHALLENGE_SELECTOR__", json.dumps(CHALLENGE_SELECTOR)).replace("__CHALLENGE_TEXT__", json.dumps(CHALLENGE_TEXT))
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

    def _check_action_stop(self, page, stage):
        """Do not keep interacting after an asynchronously observed action."""
        monitor = getattr(page, '_hh_action_monitor', None)
        session = getattr(monitor, 'session', None)
        if session is None:
            return
        context = getattr(page, 'context', None)
        watch = getattr(context, '_hh_action_watch', None)
        reason = getattr(session, '_hh_recovery_stop_reason', '')
        ownership_changed = (getattr(session, '_page', None) is not page
                             or getattr(session, '_context', None) is not context)
        uncertain = bool(getattr(session, '_hh_recovery_uncertain', False)
                         or getattr(monitor, 'unknown', False) or getattr(watch, 'unknown', False)
                         or ownership_changed)
        if not (reason or uncertain or getattr(session, '_recovering', False)):
            return
        reason = reason or ('browser_ownership_changed' if ownership_changed else
                            'possible_external_action' if uncertain else 'recovery_ownership_unavailable')
        if self.blocked is None:
            self.blocked = HHUnexpectedUI(stage, hashlib.sha256(reason.encode()).hexdigest())
        self.blocked.hh_stop_reason = reason
        self.blocked.hh_uncertain = bool(getattr(self.blocked, 'hh_uncertain', False) or uncertain)
        self.blocked.hh_recovered = False
        raise self.blocked

    async def ensure(self, page, stage, *, allowed=()):
        self._check_action_stop(page, stage)
        if self.blocked is not None:
            raise self.blocked
        dialogs = []
        changed = False
        try:
            for _ in range(4):
                self._check_action_stop(page, stage)
                dialogs = await self._scan(page, allowed)
                self._check_action_stop(page, stage)
                blocking = [item for item in dialogs if item["kind"] not in allowed]
                if not blocking:
                    return changed
                # Never dismiss a known popup to work around another unknown UI.
                if any(item["kind"] != "optional" or not item["closable"] for item in blocking):
                    break
                self._check_action_stop(page, stage)
                closed = await page.evaluate(CLOSE_SCRIPT, blocking[0])
                self._check_action_stop(page, stage)
                if closed is not True:
                    break
                changed = True
                # Refetch, not stale element handles; a changed/new modal is reclassified.
                await page.wait_for_timeout(100)
                self._check_action_stop(page, stage)
            else:
                dialogs = await self._scan(page, allowed)
                self._check_action_stop(page, stage)
        except HHUnexpectedUI:
            raise
        except Exception as exc:
            log.warning("HH UI inspection failed: %s", type(exc).__name__)
            dialogs = [{"shape": "inspection_failed", "kind": "unknown"}]
        self._check_action_stop(page, stage)
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
