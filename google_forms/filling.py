from __future__ import annotations

import contextlib
import logging
import time

from google_forms.answering import (
    _answers_by_index,
    _best_option_match,
    _is_bare_other_option,
    _norm,
)
from google_forms.extraction import extract_form_questions

log = logging.getLogger("google_form_filler")


def _looks_like_google_form_login_required(page_text: str) -> bool:
    text = _norm(page_text)
    return (
        ("log in om door te gaan" in text and "ingelogd" in text)
        or "je moet zijn ingelogd om dit formulier in te vullen" in text
        or "sign in to continue" in text
        or "you must be signed in to fill out this form" in text
        or "sign in to fill out this form" in text
        or "войдите, чтобы продолжить" in text
        or "необходимо войти" in text
    )


def _is_google_form_next_button_text(value: str) -> bool:
    text = _norm(value)
    return text in {"далее", "next", "volgende", "continuar", "weiter"}


def _is_google_form_submit_button_text(value: str) -> bool:
    text = _norm(value)
    return text in {"отправить", "submit", "verzenden", "send", "envoyer", "senden"}


def _looks_like_google_form_submit_success(page_text: str) -> bool:
    text = _norm(page_text)
    if not text:
        return False
    return any(
        marker in text
        for marker in (
            "ваш ответ записан",
            "ответ записан",
            "ответ отправлен",
            "форма отправлена",
            "отправить еще один ответ",
            "отправить ещё один ответ",
            "your response has been recorded",
            "response has been recorded",
            "submit another response",
            "je antwoord is geregistreerd",
            "je antwoord is opgenomen",
            "uw antwoord is geregistreerd",
            "uw antwoord is opgenomen",
            "verzend nog een reactie",
            "envoyer une autre réponse",
            "eine weitere antwort senden",
        )
    )


def _is_google_form_email_consent_text(value: str) -> bool:
    text = _norm(value)
    if not text:
        return False
    return (
        ("указать" in text and "электрон" in text and "почт" in text)
        or ("record" in text and "email" in text)
        or ("email" in text and "address" in text and "response" in text)
        or ("e-mailadres" in text and "antwoord" in text)
        or ("emailadres" in text and "antwoord" in text)
    )


def _google_form_preview_status(questions: list[dict], fill_result: dict, *, reached_submit: bool = True) -> tuple[bool, str]:
    filled_count = len((fill_result or {}).get("filled") or [])
    skipped = (fill_result or {}).get("skipped") or []
    skipped_indices = {int(item.get("index")) for item in skipped if str(item.get("index", "")).lstrip("-").isdigit()}
    required_skipped = [q for q in questions or [] if q.get("required") and int(q.get("index", -1)) in skipped_indices]
    if not questions:
        return False, "form questions not found"
    if any(item.get("blocking") for item in skipped):
        return False, "actual form values do not equal approved values"
    if required_skipped:
        return False, "required form fields were not filled"
    if filled_count <= 0:
        return False, "form detected but no fields were filled"
    if not reached_submit:
        return False, "form preview did not reach submit page"
    return True, "preview"


def _reindex_page_questions(page_questions: list[dict], *, page_index: int, start_index: int) -> list[dict]:
    out = []
    for offset, question in enumerate(page_questions or []):
        item = dict(question)
        item["page_index"] = page_index
        item["page_question_index"] = int(item.get("index") or offset)
        item["index"] = start_index + offset
        out.append(item)
    return out


def _merge_fill_results(results: list[dict]) -> dict:
    filled = []
    skipped = []
    for result in results or []:
        filled.extend(result.get("filled") or [])
        skipped.extend(result.get("skipped") or [])
    return {"filled": filled, "skipped": skipped}


def _question_signature(questions: list[dict]) -> list[str]:
    return [_norm(item.get("question") or "") for item in questions]


async def _fill_google_form_email_consent(page) -> bool:
    """Compatibility hook: never grant consent based on an email substring.

    Even Google's standard-looking label is not proof of candidate/account
    identity or approved intent. Ordinary question checkboxes must follow the
    grounded/reviewed answer path; an unhandled required account-email control
    stays manual rather than being implicitly consented to before extraction.
    """
    return False


async def _google_form_buttons(page) -> list[dict]:
    buttons = await page.locator('div[role="button"]:visible, button:visible').element_handles()
    out = []
    for button in buttons:
        try:
            text = await button.inner_text()
        except Exception:
            text = ""
        try:
            aria = await button.get_attribute("aria-label") or ""
        except Exception:
            aria = ""
        try:
            disabled = await button.get_attribute("aria-disabled")
            disabled_attr = await button.get_attribute("disabled")
        except Exception:
            disabled = disabled_attr = None
        label = " ".join((text or aria or "").split())
        out.append({
            "element": button,
            "text": label,
            "disabled": str(disabled or "").casefold() == "true" or disabled_attr is not None,
        })
    return out


async def _find_google_form_button(page, predicate):
    for button in await _google_form_buttons(page):
        if not button.get("disabled") and predicate(button.get("text") or ""):
            return button.get("element")
    return None


async def _has_google_form_submit_button(page) -> bool:
    return await _find_google_form_button(page, _is_google_form_submit_button_text) is not None


async def _google_form_page_signature(page) -> list[str]:
    try:
        return _question_signature(await extract_form_questions(page))
    except Exception:
        return []


async def _google_form_required_errors(page) -> list[str]:
    try:
        text = await page.locator("body").inner_text(timeout=3000)
    except Exception:
        return []
    markers = (
        "это обязательный вопрос",
        "this is a required question",
        "required question",
        "dit is een verplichte vraag",
    )
    lines = []
    for line in str(text or "").splitlines():
        clean = " ".join(line.split())
        if clean and any(marker in clean.casefold() for marker in markers):
            lines.append(clean)
    return lines[:5]


async def _click_google_form_next(page) -> tuple[bool, str]:
    before_signature = await _google_form_page_signature(page)
    before_url = page.url
    button = await _find_google_form_button(page, _is_google_form_next_button_text)
    if not button:
        return False, "google form next button not found"
    try:
        await button.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass
    await button.click(timeout=10000)

    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        await page.wait_for_timeout(500)
        if await _has_google_form_submit_button(page):
            return True, "advanced"
        after_signature = await _google_form_page_signature(page)
        if after_signature and after_signature != before_signature:
            return True, "advanced"
        if page.url != before_url:
            return True, "advanced"

    errors = await _google_form_required_errors(page)
    if errors:
        return False, "google form required validation blocked next"
    return False, "google form did not advance after next"


async def _arm_google_form_submit_boundary(page, questions, rows, items) -> bool:
    return await page.evaluate(r"""expected => {
        /* codex:google-form-arm */
        const visible = el => el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden';
        const currentItems = () => [...document.querySelectorAll('div[role="listitem"]')].filter(visible);
        const items = currentItems();
        const previous = document.__googleFormApproval;
        document.__googleFormApproval = null;
        if (!items.length || items.length !== expected.items.length || items.some((el,i) => el !== expected.items[i])) return false;
        const root = items[0].closest('form') || document.body;
        const pageIndex = expected.questions[0]?.page_index || 0;
        if (pageIndex && (!previous || previous.root !== root || previous.pageIndex < pageIndex - 1)) return false;
        const byIndex = new Map(expected.rows.map(row => [row.index,row]));
        const plans = expected.questions.map(q => ({index:q.index,type:q.type || 'text', options:q.options || [],
            item:items[q.dom_index ?? q.index], row:byIndex.get(q.index)}));
        const indices = new Set(plans.map(plan => plan.index));
        const currentPlanItems = new Set(plans.map(plan => plan.item));
        if (pageIndex) plans.unshift(...previous.plans.filter(plan => !indices.has(plan.index)));
        const norm = value => String(value || '').replace(/\u00a0/g,' ').replace(/\s+/g,' ').trim().toLowerCase();
        const valuesMatch = () => plans.every(plan => {
            if (!plan.item?.isConnected || !root.contains(plan.item)) return false;
            if (plan.type === 'text') {
                const fields = [...plan.item.querySelectorAll('textarea,input[type="text"],input[type="email"],input[type="number"],input[type="url"],input[type="tel"]')];
                return fields.length === 1 && !fields[0].disabled && fields[0].value === (plan.row?.answer || '');
            }
            if (!['radio','checkbox'].includes(plan.type)) return false;
            const options = [...plan.item.querySelectorAll('[role="radio"],[role="checkbox"]')];
            const labels = options.map(el => norm(el.getAttribute('aria-label') || el.innerText));
            const allowed = plan.options.map(norm).sort();
            if (!options.length || new Set(labels).size !== labels.length || JSON.stringify([...labels].sort()) !== JSON.stringify(allowed)) return false;
            if (options.some(el => !['true','false'].includes(el.getAttribute('aria-checked')))) return false;
            const selected = options.filter(el => el.getAttribute('aria-checked') === 'true').map(el => norm(el.getAttribute('aria-label') || el.innerText)).sort();
            return JSON.stringify(selected) === JSON.stringify((plan.row?.options || []).map(norm).sort());
        });
        const fields = () => [...new Set([...root.querySelectorAll('input,textarea,select,[role="checkbox"],[role="radio"],[contenteditable="true"]'),
            ...(root.tagName === 'FORM' ? [...root.elements].filter(el => el.matches('input,textarea,select')) : [])])];
        const payload = control => root.tagName === 'FORM' ? JSON.stringify([...new FormData(root,
            control?.form === root && control.type === 'submit' ? control : undefined).entries()].map(([key,value]) =>
            [key,typeof value === 'string' ? value : [value.name,value.size,value.type,value.size ? value.lastModified : 0]])) : null;
        const fieldState = el => JSON.stringify([el.tagName,el.type,el.name,el.getAttribute('form'),el.disabled,
            el.value ?? el.textContent,el.checked,el.getAttribute('aria-label'),el.getAttribute('aria-checked'),
            [...(el.options || [])].map(o => [o.value,o.selected])]);
        if (pageIndex) {
            const current = fields();
            if (previous.fieldRecords.some(record => !currentPlanItems.has(record.item) &&
                    (!current.includes(record.el) || fieldState(record.el) !== record.state))) return false;
            if (current.some(el => !previous.fieldRecords.some(record => record.el === el) &&
                    ![...currentPlanItems].some(item => item.contains(el)))) return false;
        }
        const snapshot = () => JSON.stringify([[...root.attributes].map(a => [a.name,a.value]),payload(null),fields().map(fieldState)]);
        if (!valuesMatch()) return false;
        const originalFields = fields(), approved = snapshot(), url = location.href;
        const valid = () => {
            const current = fields(), active = currentItems();
            return root.isConnected && location.href === url && valuesMatch() &&
                active.length === items.length && active.every((el,i) => el === items[i]) &&
                current.length === originalFields.length && current.every((el,i) => el === originalFields[i]) && snapshot() === approved;
        };
        const controlState = el => JSON.stringify([el.innerText,el.name,el.value,el.type,el.getAttribute('form'),el.getAttribute('formaction')]);
        const fieldRecords = originalFields.map(el => ({el,item:el.closest('div[role="listitem"]'),state:fieldState(el)}));
        const approval = {root,plans,pageIndex,fieldRecords,valid,blocked:false};
        approval.bind = control => {
            if (!valid() || !control.isConnected || !(root.contains(control) || control.form === root)) return false;
            approval.control=control;approval.controlState=controlState(control);approval.payload=payload(control);
            return true;
        };
        approval.eventMatches = event => valid() && controlState(approval.control) === approval.controlState &&
            payload(event.type === 'submit' ? event.submitter : approval.control) === approval.payload;
        document.__googleFormApproval = approval;
        if (!document.__googleFormBoundary) {
            document.__googleFormBoundary = true;
            for (const name of ['click','submit']) document.addEventListener(name,event => {
                const approval=document.__googleFormApproval;
                if (!approval || (name === 'click' && !approval.control?.contains(event.target))) return;
                if ((name === 'click' || event.target === approval.root) && approval.eventMatches(event)) return;
                approval.blocked=true;event.preventDefault();event.stopImmediatePropagation();
            },true);
        }
        return true;
    }""", {'questions': questions, 'rows': rows, 'items': items}) is True


async def _click_google_form_submit(page, *, before_click=None) -> bool:
    button = await _find_google_form_button(page, _is_google_form_submit_button_text)
    if not button:
        return False
    try:
        await button.scroll_into_view_if_needed(timeout=5000)
    except Exception:
        pass
    if await button.evaluate(r"""el => {
        /* codex:google-form-bind */
        return !!document.__googleFormApproval && document.__googleFormApproval.bind(el);
    }""") is not True:
        return False
    if before_click is not None and not before_click():
        return False
    await button.click(timeout=10000)
    if await button.evaluate('/* codex:google-form-readback */ el => !document.__googleFormApproval?.blocked') is not True:
        return False
    with contextlib.suppress(Exception):
        await page.wait_for_load_state("networkidle", timeout=15000)
    await page.wait_for_timeout(1500)
    return True


async def _wait_google_form_submit_success(page, *, timeout_s: float = 15.0) -> tuple[bool, str]:
    last_text = ""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            last_text = await page.locator("body").inner_text(timeout=3000)
        except Exception:
            last_text = ""
        if _looks_like_google_form_submit_success(last_text):
            return True, last_text
        await page.wait_for_timeout(750)
    return False, last_text


async def _click_google_form_option(handle) -> None:
    try:
        await handle.click(timeout=5000)
        return
    except Exception as first_exc:
        try:
            await handle.click(timeout=5000, force=True)
            return
        except Exception:
            try:
                await handle.evaluate("el => el.click()")
                return
            except Exception:
                raise first_exc


async def fill_form(page, questions: list[dict], answers: list[dict]) -> dict:
    items = await page.locator('div[role="listitem"]:visible').element_handles()
    answer_map = _answers_by_index(answers)
    filled: list[dict] = []
    skipped: list[dict] = []
    for question in questions:
        idx = int(question.get("index", -1))
        dom_index = int(question.get("dom_index", idx))
        answer = answer_map.get(idx) or {}
        if answer.get("skip"):
            skipped.append({"index": idx, "reason": "skip requested", "question": question.get("question", "")})
            continue
        if dom_index < 0 or dom_index >= len(items):
            skipped.append({"index": idx, "reason": "dom item not found", "question": question.get("question", "")})
            continue
        item = items[dom_index]
        qtype = question.get("type") or "text"
        try:
            if qtype == "text":
                value = str(answer.get("answer") or "").strip()
                if not value:
                    skipped.append({"index": idx, "reason": "empty answer", "question": question.get("question", "")})
                    continue
                field = await item.query_selector('textarea, input[type="text"], input[type="email"], input[type="number"], input[type="url"], input[type="tel"]')
                if not field:
                    skipped.append({"index": idx, "reason": "text field not found", "question": question.get("question", "")})
                    continue
                await field.fill(value)
                actual = await field.input_value()
                if actual != value:
                    skipped.append({"index": idx, "reason": "text field value verification failed", "question": question.get("question", "")})
                    continue
                filled.append({"index": idx, "type": qtype, "answer": value})
                continue

            raw_options = answer.get("options") or []
            if isinstance(raw_options, list):
                desired_values = [str(value or "") for value in raw_options]
            else:
                desired_values = [str(raw_options or "")]
            if not any(value.strip() for value in desired_values):
                raw_answer = answer.get("answer")
                if isinstance(raw_answer, list):
                    desired_values = [str(value or "") for value in raw_answer]
                else:
                    desired_values = [str(raw_answer or "")]
            options = list(question.get("options") or [])
            desired_matches = []
            seen_matches = set()
            for desired in desired_values:
                match = next((option for option in options if _norm(option) == _norm(desired)), "")
                if not match or _is_bare_other_option(match):
                    raise ValueError("approved option unavailable or unsupported")
                key = _norm(match)
                if key not in seen_matches:
                    desired_matches.append(match)
                    seen_matches.add(key)
                if qtype == "radio":
                    break
            if qtype == 'radio' and len([value for value in desired_values if value.strip()]) != 1:
                raise ValueError('radio approval must select exactly one option')
            selected = []
            option_handles = await item.query_selector_all('[role="radio"], [role="checkbox"]')
            if qtype == "checkbox":
                desired_norms = {_norm(value) for value in desired_matches}
                for handle in option_handles:
                    label = (await handle.get_attribute("aria-label")) or (await handle.inner_text())
                    checked = ""
                    try:
                        checked = await handle.get_attribute("aria-checked") or ""
                    except Exception:
                        checked = ""
                    if checked.casefold() == "true" and _norm(label) not in desired_norms:
                        await _click_google_form_option(handle)
            for match in desired_matches:
                for handle in option_handles:
                    label = (await handle.get_attribute("aria-label")) or (await handle.inner_text())
                    if _norm(label) == _norm(match):
                        checked = ""
                        try:
                            checked = await handle.get_attribute("aria-checked") or ""
                        except Exception:
                            checked = ""
                        if checked.casefold() != "true":
                            await _click_google_form_option(handle)
                            try:
                                checked = await handle.get_attribute("aria-checked") or ""
                            except Exception:
                                checked = ""
                        if checked.casefold() == "true":
                            selected.append(match)
                        break
                if qtype == "radio" and selected:
                    break
            if desired_matches and len(selected) == len(desired_matches):
                filled.append({"index": idx, "type": qtype, "options": selected})
            else:
                skipped.append({"index": idx, "reason": "option not matched", "question": question.get("question", "")})
        except Exception as exc:
            skipped.append({"index": idx, "reason": str(exc), "question": question.get("question", "")})
    # Re-fetch the current DOM after the full fill: later fields can change
    # earlier controls. A cached handle/partial selection is not readback proof.
    verified = []
    rows = {row['index']: row for row in filled}
    current_items = await page.locator('div[role="listitem"]:visible').element_handles()
    if len(current_items) != len(items):
        return {'filled': [], 'skipped': skipped + [{'index': -1, 'blocking': True,
                                                     'reason': 'DOM question count changed'}]}
    for question in questions:
        idx = int(question.get('index', -1))
        row = rows.get(idx)
        dom_index = int(question.get('dom_index', idx))
        try:
            if not 0 <= dom_index < len(current_items):
                raise ValueError('readback DOM item missing')
            item = current_items[dom_index]
            qtype = question.get('type') or 'text'
            if qtype == 'text':
                field = await item.query_selector('textarea, input[type="text"], input[type="email"], input[type="number"], input[type="url"], input[type="tel"]')
                if not field or await field.input_value() != (row['answer'] if row else ''):
                    raise ValueError('text readback differs from approval')
            elif qtype in {'radio', 'checkbox'}:
                handles = await item.query_selector_all('[role="radio"], [role="checkbox"]')
                actual = []
                labels = []
                for handle in handles:
                    label = (await handle.get_attribute('aria-label')) or (await handle.inner_text())
                    labels.append(_norm(label))
                    checked = await handle.get_attribute('aria-checked')
                    if checked not in {'true', 'false'}:
                        raise ValueError('option readback unknown')
                    if checked == 'true': actual.append(_norm(label))
                desired = [_norm(label) for label in row['options']] if row else []
                if (not handles or len(labels) != len(set(labels)) or sorted(actual) != sorted(desired)):
                    raise ValueError('option readback differs from approval')
            else:
                raise ValueError('unsupported readback type')
            if row:
                verified.append(row)
        except Exception:
            skipped = [entry for entry in skipped if entry['index'] != idx]
            skipped.append({'index': idx, 'reason': 'exact DOM readback failed',
                            'question': question.get('question', ''), 'blocking': True})
    # An approved value which could not be filled must block even when optional.
    for entry in skipped:
        if not (answer_map.get(entry['index']) or {}).get('skip') and entry['reason'] != 'empty answer':
            entry['blocking'] = True
    try:
        armed = await _arm_google_form_submit_boundary(page, questions, verified, current_items)
    except Exception as exc:
        log.warning("Google Forms boundary could not be armed: %s", type(exc).__name__)
        armed = False
    if not armed:
        skipped.append({'index': -1, 'reason': 'approved DOM boundary unavailable', 'blocking': True})
    return {"filled": verified, "skipped": skipped}


async def _safe_screenshot(page, path: str) -> None:
    try:
        from private_artifacts import private_screenshot
        await private_screenshot(page, path, full_page=True)
    except Exception as exc:
        log.warning("google form screenshot failed: %s", type(exc).__name__)
