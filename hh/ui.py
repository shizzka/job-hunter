"""Fail-closed HH dialog handling. Never answer profile/onboarding questions."""
import hashlib
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
            !!root.querySelector('[data-qa="vacancy-response-submit-popup"], [data-qa="vacancy-response-letter-submit"]');
        const captcha = !!root.querySelector('iframe[src*="recaptcha"], iframe[src*="hcaptcha"], [data-qa="captcha"]');
        const kind = optional ? 'optional' : response ? 'response' : captcha ? 'captcha' : 'unknown';
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
        try:
            attempt = self.warnings.claim(fingerprint)
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
