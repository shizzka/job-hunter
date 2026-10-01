"""Helpers for the HH vacancy application flow."""

import hashlib
import os
import re

from hh.text import compact_text, normalize_text


CLOSED_OR_ARCHIVED_HH_TEXT_MARKERS = (
    "вакансия в архиве",
    "вакансия находится в архиве",
    "вакансия уже в архиве",
    "вакансия перемещена в архив",
    "вакансия закрыта",
    "вакансия уже закрыта",
    "закрыта и не принимает отклики",
    "не принимает отклики",
    "прием откликов закрыт",
    "приём откликов закрыт",
    "отклики больше не принимаются",
    "вакансия неактивна",
    "страница вакансии удалена",
)
CLOSED_OR_ARCHIVED_HH_COMPACT_MARKERS = (
    '"archived":"true"',
    '"archived":true',
    "'archived':'true'",
    "'archived':true",
    "&quot;archived&quot;:&quot;true&quot;",
    "&quot;archived&quot;:true",
)


def has_archived_hh_state(value: str) -> bool:
    compact = compact_text(value)
    return any(marker in compact for marker in CLOSED_OR_ARCHIVED_HH_COMPACT_MARKERS)


def looks_like_closed_or_archived_hh(value: str) -> bool:
    if has_archived_hh_state(value):
        return True

    compact = compact_text(value)
    if "<html" in compact or "<template" in compact:
        return False

    text = normalize_text(value)
    return any(marker in text for marker in CLOSED_OR_ARCHIVED_HH_TEXT_MARKERS)


def looks_like_existing_hh_response(value: str) -> bool:
    text = normalize_text(value)
    return (
        "вы откликнулись" in text
        or "уже отклик" in text
        or "отклик другим резюме" in text
        or "откликнуться повторно" in text
    )


def looks_like_hh_apply_success(value: str) -> bool:
    text = normalize_text(value)
    return (
        looks_like_existing_hh_response(value)
        or "резюме доставлено" in text
        or "отклик отправлен" in text
        or "связаться с работодателем можно в чате" in text
    )


async def click_with_fallbacks(session, element, label: str, *, logger) -> bool:
    """Надёжный клик по элементу с fallback-стратегиями."""
    if not element:
        return False

    try:
        await element.evaluate(
            "el => el.scrollIntoView({block: 'center', inline: 'center'})"
        )
        await session._page.wait_for_timeout(300)
    except Exception:
        pass

    strategies = (
        ("normal", lambda: element.click(timeout=5000)),
        ("force", lambda: element.click(timeout=5000, force=True)),
        (
            "js",
            lambda: element.evaluate(
                "el => { el.scrollIntoView({block: 'center', inline: 'center'}); el.click(); }"
            ),
        ),
    )

    for strategy_name, action in strategies:
        try:
            logger.info("Clicking %s via %s strategy", label, strategy_name)
            await action()
            await session._page.wait_for_timeout(1000)
            return True
        except Exception as e:
            logger.warning("%s click via %s failed: %s", label, strategy_name, e)

    return False


async def has_existing_response_ui(session, *, looks_like_existing_response, logger) -> bool:
    """Проверить UI hh.ru на признак уже отправленного отклика."""
    selectors = (
        "[data-qa*='responded']",
        "[data-qa='already-responded-text']",
        "[data-qa='vacancy-response-link-top-again']",
        "[data-qa='vacancy-response-link-bottom-again']",
        "button:has-text('Вы откликнулись')",
        "a:has-text('Вы откликнулись')",
        "button:has-text('Отклик другим резюме')",
        "a:has-text('Отклик другим резюме')",
        "button:has-text('Откликнуться повторно')",
        "a:has-text('Откликнуться повторно')",
    )
    try:
        for selector in selectors:
            marker = await session._page.query_selector(selector)
            if marker:
                return True
        return False
    except Exception as e:
        logger.debug("Existing response UI check failed: %s", e)
        return False


async def page_text(session, limit: int = 12000) -> str:
    try:
        return await session._page.evaluate(
            f"() => document.body.innerText.slice(0, {int(limit)})"
        )
    except Exception:
        return ""


async def page_closed_or_archived(session, *, looks_like_closed_or_archived, has_archived_state) -> bool:
    """Detect closed/archived hh vacancy pages before trying to respond."""
    body_text = await session._page_text(limit=20000)
    if looks_like_closed_or_archived(body_text):
        return True

    try:
        page_html = await session._page.content()
    except Exception:
        page_html = ""
    return has_archived_state(page_html)


async def apply_success_detected(session, *, looks_like_apply_success, logger) -> bool:
    selectors = (
        "[data-qa*='responded']",
        "[data-qa='already-responded-text']",
        "[data-qa='vacancy-response-success-standard-notification']",
        "[data-qa*='success-standard-notification']",
        "[data-qa='vacancy-response-link-view-topic']",
        "button:has-text('Вы откликнулись')",
        "a:has-text('Вы откликнулись')",
    )
    try:
        for selector in selectors:
            marker = await session._page.query_selector(selector)
            if marker:
                return True
    except Exception as e:
        logger.debug("Apply success selector check failed: %s", e)

    current_url = session._page.url or ""
    if "/negotiations" in current_url:
        return True

    return False


async def response_error_detected(session, *, logger) -> bool:
    """Detect a visible response-specific error, not words in vacancy copy."""
    selectors = (
        "[data-qa='vacancy-response-error']",
        "[data-qa='vacancy-response-popup-error']",
        "[data-qa='vacancy-response-popup-form-error']",
        "[data-qa*='vacancy-response'][data-qa*='error']",
        "[role='alert'][data-qa*='vacancy-response']",
    )
    try:
        for selector in selectors:
            marker = await session._page.query_selector(selector)
            if not marker:
                continue
            is_visible = getattr(marker, "is_visible", None)
            if is_visible is None or await is_visible():
                return True
    except Exception as exc:
        logger.debug("Apply error selector check failed: %s", exc)
    return False


async def first_visible_element(page, selectors):
    """Вернуть первый видимый элемент по приоритетному списку селекторов."""
    for selector in selectors:
        try:
            elements = await page.query_selector_all(selector)
        except Exception:
            continue
        for element in elements:
            try:
                if await element.is_visible():
                    return element
            except Exception:
                continue

    # Совместимость с простыми page-адаптерами и тестовыми doubles, где
    # query_selector_all отсутствует или не реализован полностью.
    for selector in selectors:
        try:
            element = await page.query_selector(selector)
        except Exception:
            continue
        if element is None:
            continue
        try:
            if await element.is_visible():
                return element
        except Exception:
            return element
    return None


async def response_submit_button(session):
    """Найти submit активной формы, не захватывая кнопку под modal overlay."""
    return await first_visible_element(
        session._page,
        (
            "[data-qa='modal-overlay'] [data-qa='vacancy-response-submit-popup']",
            "[role='dialog'] [data-qa='vacancy-response-submit-popup']",
            "form[name='vacancy_response'] [data-qa='vacancy-response-submit-popup']",
            "[data-qa='vacancy-response-submit-popup']",
            "[data-qa='vacancy-response-letter-submit']",
            "form[name='vacancy_response'] button[type='submit']",
            "button[data-qa*='submit']",
            "[data-qa='vacancy-response-link-top-again']",
            "[data-qa='vacancy-response-link-bottom-again']",
            "[data-qa='vacancy-response-link-top']",
            "[data-qa='vacancy-response-link-bottom']",
            "a[data-qa*='response-link']",
        ),
    )


async def response_form_signature(session, current_url: str = "") -> dict:
    """Return a stable, value-free structural fingerprint of the active HH form."""
    page_url = (current_url or session._page.url or "").lower()
    result = await session._page.evaluate(
        """() => {
            /* codex:response-form-signature */
            const visible = (el) => {
                if (!el) return false;
                const style = window.getComputedStyle(el);
                const rect = el.getBoundingClientRect();
                return style.display !== "none" && style.visibility !== "hidden"
                    && rect.width > 0 && rect.height > 0;
            };
            const form = [...document.querySelectorAll('form[name="vacancy_response"]')]
                .find(visible);
            if (!form) return null;
            const controls = [...form.querySelectorAll("input, textarea, select, button")].filter(visible);
            const describe = (el) => [
                (el.tagName || "").toLowerCase(),
                (el.getAttribute("type") || "").toLowerCase(),
                el.getAttribute("name") || "",
                el.getAttribute("data-qa") || "",
                el.getAttribute("role") || "",
                el.required || el.getAttribute("aria-required") === "true" ? "required" : "optional",
            ].join(":");
            const isLetter = (el) => el.matches(
                '[name="letter"], [data-qa="vacancy-response-popup-form-letter-input"], textarea[data-qa*="letter"]'
            );
            const isIgnored = (el) => {
                const type = (el.getAttribute("type") || "").toLowerCase();
                return ["hidden", "submit", "button", "image", "file"].includes(type)
                    || el.tagName.toLowerCase() === "button"
                    || isLetter(el)
                    || el.closest('[data-qa="resume-select"]') !== null;
            };
            const letterCount = controls.filter(isLetter).length;
            const submitCount = controls.filter((el) =>
                (el.getAttribute("type") || "").toLowerCase() === "submit"
                || (el.getAttribute("data-qa") || "").includes("response-submit")
            ).length;
            const otherFieldCount = controls.filter((el) =>
                ["input", "textarea", "select"].includes(el.tagName.toLowerCase()) && !isIgnored(el)
            ).length;
            return {
                controls: controls.map(describe).sort(),
                letter_count: letterCount,
                submit_count: submitCount,
                other_field_count: otherFieldCount,
            };
        }"""
    )
    if not result:
        return {}
    payload = "|".join(result.get("controls") or [])
    result["fingerprint"] = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]
    result["path"] = page_url.split("?", 1)[0]
    return result


async def response_requires_questions(session, current_url: str = "", *, logger) -> bool:
    page_url = (current_url or session._page.url or "").lower()
    if "vacancy_response_question" in page_url:
        return True

    try:
        signature = await response_form_signature(session, current_url)
        if signature:
            fingerprint = signature.get("fingerprint", "")
            if fingerprint and fingerprint != getattr(session, "_last_response_form_fingerprint", ""):
                logger.info(
                    "HH response form DOM: fingerprint=%s letter=%d submit=%d other=%d",
                    fingerprint,
                    int(signature.get("letter_count") or 0),
                    int(signature.get("submit_count") or 0),
                    int(signature.get("other_field_count") or 0),
                )
                session._last_response_form_fingerprint = fingerprint
            return int(signature.get("other_field_count") or 0) > 0
    except Exception as exc:
        logger.debug("Response form fingerprint failed: %s", exc)

    # HH now shows the generic copy about "несколько вопросов работодателя"
    # even when the only required control is the cover-letter textarea. Treat
    # actual questionnaire controls as evidence; page copy alone is not enough.
    try:
        inspect = getattr(session, "_inspect_employer_questions", None)
        if inspect is not None:
            result = await inspect()
            if result.get("fields") or int(result.get("unsupported_fields") or 0) > 0:
                return True
    except Exception as exc:
        logger.debug("Question flow structural check failed: %s", exc)

    return False


async def dismiss_magritte_dropdowns(session) -> None:
    popup_selectors = (
        "[data-magritte-drop-base-direction]",
        "[data-qa='drop-base']",
    )
    for _ in range(3):
        popup = None
        for selector in popup_selectors:
            popup = await session._page.query_selector(selector)
            if popup:
                break
        if popup is None:
            return

        try:
            await session._page.keyboard.press("Escape")
        except Exception:
            pass
        await session._page.wait_for_timeout(200)

        popup = None
        for selector in popup_selectors:
            popup = await session._page.query_selector(selector)
            if popup:
                break
        if popup is None:
            return

        try:
            await session._page.evaluate(
                "() => document.activeElement && typeof document.activeElement.blur === 'function' && document.activeElement.blur()"
            )
        except Exception:
            pass
        await session._page.wait_for_timeout(100)


async def expand_cover_letter_input(session) -> bool:
    selectors = (
        "[data-qa='vacancy-response-letter-toggle']",
        "[data-qa='add-cover-letter']",
        "button[data-qa='add-cover-letter']",
        "button:has-text('Добавить сопроводительное')",
        "button:has-text('Приложить письмо')",
        "button:has-text('Добавить письмо')",
    )
    for selector in selectors:
        try:
            button = await session._page.query_selector(selector)
        except Exception:
            continue
        if not button:
            continue
        if await session._click_with_fallbacks(button, f"cover_letter_toggle:{selector}"):
            await session._page.wait_for_timeout(500)
            return True
    return False


async def submit_response_form_via_dom(session, *, logger) -> bool:
    try:
        result = await session._page.evaluate(
            """() => {
                    const visible = (element) => {
                        if (!element || element.getClientRects().length === 0) {
                            return false;
                        }
                        const style = window.getComputedStyle(element);
                        return style.visibility !== 'hidden' && style.display !== 'none';
                    };
                    const buttonSelector = [
                        "[data-qa='vacancy-response-submit-popup']",
                        "[data-qa='vacancy-response-letter-submit']",
                        "button[type='submit']"
                    ].join(",");
                    const roots = [
                        ...document.querySelectorAll(
                            "[data-qa='modal-overlay'], [role='dialog']"
                        )
                    ].filter(visible);
                    let button = null;
                    for (const root of roots) {
                        button = [...root.querySelectorAll(buttonSelector)].find(visible);
                        if (button) {
                            break;
                        }
                    }
                    if (!button) {
                        button = [...document.querySelectorAll(buttonSelector)].find(visible);
                    }
                    const form = button?.form
                        || button?.closest("form[name='vacancy_response']")
                        || [...document.querySelectorAll("form[name='vacancy_response']")].find(visible);
                    if (form && typeof form.requestSubmit === 'function') {
                        if (button && button.form === form) {
                            form.requestSubmit(button);
                        } else {
                            form.requestSubmit();
                        }
                        return true;
                    }
                    if (button) {
                        button.click();
                        return true;
                    }
                    return false;
                }"""
        )
    except Exception as exc:
        logger.debug("DOM submit fallback failed: %s", exc)
        return False
    return bool(result)


async def save_debug_snapshot(session, prefix: str, *, state_dir: str) -> None:
    """Сохранить скриншот + HTML текущей страницы в state-dir (для отладки)."""
    try:
        debug_path = os.path.join(state_dir, f"{prefix}.png")
        debug_html = os.path.join(state_dir, f"{prefix}.html")
        await session._page.screenshot(path=debug_path)
        with open(debug_html, "w") as f:
            f.write(await session._page.content())
    except Exception:
        pass


async def detect_response_controls(session):
    """Обнаружить элементы формы отклика на текущей странице.

        Возвращает кортеж (current_url, response_header, questions_required,
        resume_select, letter_field, submit_btn). Используется в apply_to_vacancy
        чтобы определить состояние страницы после очередного шага.
        """
    current_url = session._page.url
    response_header = await session._page.query_selector(
        "h1:has-text('Отклик на вакансию'), "
        "h2:has-text('Отклик на вакансию')"
    )
    questions_required = await session._response_requires_questions(current_url)
    resume_select = await session._page.query_selector(
        "[data-qa='resume-select'], "
        "[data-qa*='resume-item'], "
        "[data-qa='vacancy-response-popup-form-resume']"
    )
    letter_field = await session._page.query_selector(
        "[data-qa='vacancy-response-popup-form-letter-input'], "
        "textarea[name='letter'], "
        "textarea[data-qa*='letter'], "
        ".vacancy-response-popup textarea, "
        "textarea"
    )
    submit_btn = await response_submit_button(session)
    if not submit_btn:
        # Some hh flows collapse back to the vacancy page after resume selection
        # and expose only a link-style "Откликнуться" control.
        submit_btn = await session._page.query_selector(
            "button:has-text('Откликнуться'), "
            "button:has-text('Отправить'), "
            "a:has-text('Откликнуться'), "
            "a:has-text('Отправить')"
        )
    return (
        current_url,
        response_header,
        questions_required,
        resume_select,
        letter_field,
        submit_btn,
    )


async def _cover_letter_visible(surface, cover_letter):
    # Exclude drafts: text in textarea/contenteditable is not proof of delivery.
    text = await surface.evaluate("""() => {
        if (!document.body) return '';
        const copy = document.body.cloneNode(true);
        copy.querySelectorAll('textarea,input,[contenteditable],script,style').forEach(el => el.remove());
        return copy.innerText || copy.textContent || '';
    }""")
    snippet = normalize_text(cover_letter[:120])
    return bool(snippet) and snippet in normalize_text(text)


async def _send_response_letter_form(session, cover_letter, *, logger):
    """Submit the separate HH letter form; never substitute Enter for submit."""
    form = await session._page.query_selector("form[action*='/applicant/vacancy_response/edit_ajax']")
    if not form:
        return None
    field = await form.query_selector("textarea[data-qa='vacancy-response-popup-form-letter-input']")
    button = await form.query_selector("button[data-qa='vacancy-response-letter-submit']")
    if not field or not button:
        logger.warning("HH post-apply letter form is incomplete")
        return False
    try:
        await field.fill(cover_letter)
        if normalize_text(await field.input_value()) != normalize_text(cover_letter):
            logger.warning("HH post-apply letter field did not retain text")
            return False
        await button.click(timeout=10000)
        for _ in range(6):
            await session._page.wait_for_timeout(500)
            if await _cover_letter_visible(session._page, cover_letter):
                logger.info("HH post-apply letter delivery confirmed")
                return True
        logger.warning("HH post-apply letter form submitted, delivery NOT confirmed")
    except Exception as exc:
        logger.warning("HH post-apply letter form failed: %s", type(exc).__name__)
    return False


async def fill_cover_letter_post_apply(session, cover_letter: str, *, logger):
    """Заполнить сопроводительное письмо на странице после успешного отклика."""
    try:
        try:
            if await _cover_letter_visible(session._page, cover_letter):
                logger.info("Cover letter already visible after apply; skipping duplicate send")
                return True
        except Exception:
            pass
        exact_result = await _send_response_letter_form(session, cover_letter, logger=logger)
        if exact_result is not None:
            if not exact_result:
                await session._save_debug_snapshot("debug_cover_letter_unconfirmed")
            return exact_result
        letter_selectors = (
            "textarea[placeholder*='Сопроводительное']",
            "textarea[placeholder*='сопроводительное']",
            "textarea[placeholder*='Сообщение']",
            "textarea[placeholder*='сообщение']",
            "textarea[name='letter']",
            "textarea",
            "input[placeholder*='Сообщение']",
            "[contenteditable='true'][role='textbox']",
            "[contenteditable='true']",
        )
        send_selectors = (
            "button:has-text('Отправить')",
            "[data-qa*='send']",
            "[type='submit']",
        )

        surfaces = [session._page]
        page_frames = getattr(session._page, "frames", None)
        if page_frames:
            surfaces.extend(frame for frame in page_frames if frame is not session._page.main_frame)

        snippet = normalize_text(cover_letter[:120])
        await session._expand_cover_letter_input()
        for surface in surfaces:
            try:
                if await _cover_letter_visible(surface, cover_letter):
                    logger.info("Cover letter already visible after apply; skipping duplicate send")
                    return True
            except Exception:
                pass

            for selector in letter_selectors:
                try:
                    letter_field = await surface.query_selector(selector)
                except Exception:
                    continue
                if not letter_field:
                    continue

                try:
                    await letter_field.scroll_into_view_if_needed()
                except Exception:
                    pass

                try:
                    await letter_field.click()
                except Exception:
                    pass

                await session._page.wait_for_timeout(300)

                filled = False
                try:
                    await letter_field.fill("")
                    await letter_field.type(cover_letter, delay=20)
                    filled = True
                except Exception:
                    try:
                        await letter_field.evaluate(
                            """(el, value) => {
                                    el.focus();
                                    if ('value' in el) {
                                        el.value = '';
                                        el.dispatchEvent(new Event('input', { bubbles: true }));
                                        el.value = value;
                                        el.dispatchEvent(new Event('input', { bubbles: true }));
                                        el.dispatchEvent(new Event('change', { bubbles: true }));
                                        return;
                                    }
                                    if (el.isContentEditable) {
                                        el.textContent = value;
                                        el.dispatchEvent(new InputEvent('input', { bubbles: true, data: value }));
                                    }
                                }""",
                            cover_letter,
                        )
                        filled = True
                    except Exception:
                        filled = False

                if not filled:
                    continue

                await session._page.wait_for_timeout(500)

                sent = False
                for send_selector in send_selectors:
                    try:
                        send_btn = await surface.query_selector(send_selector)
                    except Exception:
                        continue
                    if not send_btn:
                        continue
                    try:
                        await send_btn.scroll_into_view_if_needed()
                    except Exception:
                        pass
                    try:
                        await send_btn.click()
                        await session._page.wait_for_timeout(2000)
                        sent = True
                        break
                    except Exception:
                        continue


                if sent:
                    try:
                        confirmed = await _cover_letter_visible(surface, cover_letter)
                    except Exception:
                        confirmed = False
                    if confirmed:
                        logger.info("Cover letter delivery confirmed after apply")
                    else:
                        logger.warning("Cover letter send attempted, delivery NOT confirmed")
                    return confirmed

        logger.debug("No cover letter field found after apply")
    except Exception as e:
        logger.warning("Failed to fill cover letter post-apply: %s", e)


async def selected_resume_matches(page, resume_id: str, title: str) -> bool:
    """Read selected controls only; a title elsewhere in the page is not evidence."""
    try:
        script = r"""() => {
            const root = document.querySelector('form[name="vacancy_response"], [role="dialog"]');
            if (!root) return {ids: [], titles: []};
            const ids = Array.from(root.querySelectorAll(
                'input[name="resume_id"], input[name="resumeId"], input[name="resumeHash"], input[type="radio"]:checked'
            )).map(el => el.value).filter(Boolean);
            root.querySelectorAll('[data-qa="resume-title"] a[href*="/resume/"]').forEach(el => {
                const match = el.getAttribute('href').match(/\/resume\/([a-zA-Z0-9]+)/);
                if (match) ids.push(match[1]);
            });
            const titles = Array.from(root.querySelectorAll('[data-qa="resume-title"]')).map(el => el.innerText.trim());
            // Magritte renders the resume listbox in a portal outside the form.
            const options = Array.from(document.querySelectorAll('[role="listbox"] [data-magritte-select-option]'))
                .filter(el => el.querySelector('[data-qa="resume-title"]') && el.getClientRects().length);
            const checked = options.filter(el => el.getAttribute('aria-selected') === 'true' && el.querySelector('input[type="radio"]:checked'));
            if (options.length) {
                return {ids: checked.map(el => el.querySelector('input[type="radio"]:checked').value),
                    titles: checked.map(el => el.querySelector('[data-qa="resume-title"]').innerText.trim())};
            }
            return {ids, titles};
        }"""
        selected = await page.evaluate(script)
        if isinstance(selected, dict) and resume_id and not selected.get('ids'):
            # Open only the response form's resume control, never the submit button.
            toggle = await page.query_selector('form[name="vacancy_response"] [data-qa="resume-title"]')
            if toggle:
                await toggle.click()
                await page.wait_for_timeout(300)
                selected = await page.evaluate(script)
    except Exception:
        return False
    if not isinstance(selected, dict):
        return False
    if resume_id:
        return resume_id in selected.get('ids', [])
    return bool(title) and any(normalize_text(value) == normalize_text(title) for value in selected.get('titles', []))


async def apply_to_vacancy(
    session,
    vacancy_url: str,
    cover_letter: str = "",
    response_url: str = "",
    preferred_resume_title: str = "",
    preferred_resume_id: str = "",
    vacancy_context: str = "",
    *,
    absolute_hh_url,
    anti_bot_message,
    logger,
) -> dict:
    """
        Откликнуться на вакансию.
        Возвращает {"ok": bool, "message": str}
        """
    vacancy_url = absolute_hh_url(vacancy_url)
    response_url = absolute_hh_url(response_url)

    save_debug_snapshot = session._save_debug_snapshot

    cover_letter_filled = False
    auto_answer_notes: list[str] = []
    auto_answer_question_answers: list[dict] = []

    async def finalize_success(
        message: str,
        *,
        already_applied: bool = False,
        notes: list[str] | None = None,
    ) -> dict:
        delivery = "not_requested"
        if cover_letter and not already_applied:
            if cover_letter_filled:
                delivery = "submitted_with_application"
            else:
                confirmed = await session._fill_cover_letter_post_apply(cover_letter)
                delivery = "confirmed" if confirmed is True else "unconfirmed"
        result = {"ok": True, "message": message, "cover_letter_status": delivery}
        if not already_applied and wants_specific_resume:
            result["resume_selection_verified"] = resume_verified
            result["selected_resume_id"] = preferred_resume_id if resume_verified else ""
        if delivery == "unconfirmed":
            notes = list(notes or []) + ["Отклик отправлен, но доставка сопроводительного письма не подтверждена"]
        if already_applied:
            result["already_applied"] = True
        if notes:
            result["notes"] = notes
        if auto_answer_question_answers:
            result["question_answers"] = list(auto_answer_question_answers)
        return result

    async def answer_questions_with_verified_resume():
        nonlocal cover_letter_filled
        if wants_specific_resume and not await selected_resume_matches(session._page, preferred_resume_id, preferred_resume_title):
            return {"ok": False, "message": "Резюме в анкете не подтверждено — нужна ручная проверка"}
        if cover_letter:
            await session._dismiss_magritte_dropdowns()
            await session._expand_cover_letter_input()
            controls = await session._detect_response_controls()
            field = controls[4]
            if not field and not cover_letter_filled:
                await save_debug_snapshot("debug_questionnaire_letter_missing")
                return {"ok": False, "message": "В анкете нет поля сопроводительного — нужна ручная проверка до отправки"}
            if field:
                await field.fill(cover_letter)
                if normalize_text(await field.input_value()) != normalize_text(cover_letter):
                    return {"ok": False, "message": "Сопроводительное в анкете не сохранилось — отклик остановлен"}
                cover_letter_filled = True
        async def verify_before_submit():
            if wants_specific_resume and not await selected_resume_matches(session._page, preferred_resume_id, preferred_resume_title):
                return False
            await session._dismiss_magritte_dropdowns()
            if cover_letter:
                current = (await session._detect_response_controls())[4]
                if current:
                    return normalize_text(await current.input_value()) == normalize_text(cover_letter)
                return cover_letter_filled
            return True
        return await session._try_auto_answer_questions(vacancy_context=vacancy_context, before_submit=verify_before_submit)

    detect_response_controls = session._detect_response_controls

    async def refetch_response_controls():
        return await session._detect_response_controls()

    async def refetch_letter_field():
        for _ in range(3):
            (
                _current_url,
                _response_header,
                _questions_required,
                _resume_select,
                fresh_letter_field,
                fresh_submit_btn,
            ) = await refetch_response_controls()
            if fresh_letter_field is not None:
                return fresh_letter_field, fresh_submit_btn
            await session._page.wait_for_timeout(400)
        return None, None

    async def select_preferred_resume() -> bool:
        title_norm = normalize_text(preferred_resume_title)
        id_norm = (preferred_resume_id or "").strip()

        if not resume_select and not title_norm and not id_norm:
            return True

        async def collect_resume_items():
            return await session._page.query_selector_all(
                "[data-magritte-select-option], "
                "[data-qa^='magritte-select-option-'], "
                "[data-qa*='resume-item'], "
                "[data-qa='vacancy-response-popup-form-resume'], "
                "[data-qa='resume-select'] [role='button'][tabindex='0'], "
                "[data-qa='resume-select'] [data-qa='cell'], "
                "[data-qa='resume-select'] label, "
                "label[data-qa='cell'], "
                "[data-qa='resume-title'], "
                "[data-qa='resume-detail'], "
                "[data-qa='cell-text-content']"
            )

        async def has_resume_choices() -> bool:
            resume_items = await collect_resume_items()
            seen_texts = set()
            meaningful = 0
            for item in resume_items:
                try:
                    text = normalize_text(await item.inner_text())
                except Exception:
                    continue
                if not text or text in seen_texts:
                    continue
                seen_texts.add(text)
                if len(text) >= 6:
                    meaningful += 1
                if meaningful >= 2:
                    return True
            return False

        async def expand_resume_picker():
            toggles = [
                "[data-qa='resume-select'] [role='button'][tabindex='0']",
                "[role='dialog'] [role='button'][tabindex='0']",
                "form[name='vacancy_response'] [role='button'][tabindex='0']",
                "[data-qa='vacancy-response-popup-form-resume']",
                "[data-qa='resume-select'] [data-qa='cell']",
                "[data-qa='resume-title']",
                "[data-qa='resume-detail']",
                "[data-qa='cell']",
            ]
            for selector in toggles:
                handle = await session._page.query_selector(selector)
                if not handle:
                    continue
                if await session._click_with_fallbacks(handle, f"resume_toggle:{selector}"):
                    await session._page.wait_for_timeout(1000)
                    if await has_resume_choices():
                        return True
            return False

        if resume_select:
            await session._click_with_fallbacks(resume_select, "resume_select")
            await session._page.wait_for_timeout(1000)

        resume_items = await collect_resume_items()

        if await selected_resume_matches(session._page, preferred_resume_id, preferred_resume_title):
            return True

        if not title_norm and not id_norm:
            if resume_items:
                return await session._click_with_fallbacks(resume_items[0], "resume_item_default")
            return True

        async def find_matching_item(items):
            for item in items:
                try:
                    text = normalize_text(await item.inner_text())
                except Exception:
                    continue
                if not text:
                    continue
                if id_norm:
                    try:
                        identity = await item.evaluate("el => ({value: el.getAttribute('value'), id: el.getAttribute('data-resume-id') || el.getAttribute('data-magritte-select-option') || el.querySelector('input[type=radio]')?.value, href: el.getAttribute('href')})")
                    except Exception:
                        identity = {}
                    if isinstance(identity, dict) and (id_norm in [identity.get('value'), identity.get('id')] or re.search(r'/resume/' + re.escape(id_norm) + r'(?:[/?#]|$)', identity.get('href') or '')):
                        return item
                if not id_norm and title_norm and title_norm == text:
                    return item
            return None

        best_item = await find_matching_item(resume_items)

        if best_item is None:
            expanded = await expand_resume_picker()
            if expanded:
                resume_items = await collect_resume_items()
                best_item = await find_matching_item(resume_items)

        if best_item is None:
            return False
        if not await session._click_with_fallbacks(best_item, "resume_item_preferred"):
            return False
        await session._page.wait_for_timeout(500)
        return await selected_resume_matches(session._page, preferred_resume_id, preferred_resume_title)

    try:
        await session._page.goto(vacancy_url, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        logger.warning("Vacancy page nav issue: %s", e)

    await session._page.wait_for_timeout(3000)
    await save_debug_snapshot("debug_apply_page")

    anti_bot_kind = await session._detect_anti_bot_kind()
    if anti_bot_kind:
        logger.warning("hh.ru anti-bot (%s) encountered on vacancy page: %s", anti_bot_kind, session._page.url)
        if response_url and response_url != vacancy_url:
            logger.info("Retrying apply flow via direct response URL: %s", response_url)
            try:
                await session._page.goto(
                    response_url,
                    wait_until="domcontentloaded",
                    timeout=30000,
                )
            except Exception as e:
                logger.warning("Direct response page nav issue: %s", e)
            await session._page.wait_for_timeout(3000)
            await save_debug_snapshot("debug_apply_response_page")
    anti_bot_kind = await session._detect_anti_bot_kind()
    anti_bot_kind = await session._handle_anti_bot_with_solver(anti_bot_kind, stage="vacancy_page")
    if anti_bot_kind:
        message = anti_bot_message(anti_bot_kind, "на странице вакансии")
        session._remember_antibot_signal(anti_bot_kind, "vacancy_page", message)
        return {"ok": False, "message": message, "anti_bot_kind": anti_bot_kind}

    if await session._page_closed_or_archived():
        return {
            "ok": False,
            "message": "Вакансия закрыта или находится в архиве",
            "closed_or_archived": True,
        }

    wants_specific_resume = bool(preferred_resume_title or preferred_resume_id)
    resume_verified = False
    if await session._has_existing_response_ui() and not wants_specific_resume:
        return await finalize_success("Уже откликались ранее", already_applied=True)

    if wants_specific_resume:
        match = re.search(r"/vacancy/(\d+)", vacancy_url)
        target_url = f"https://hh.ru/applicant/vacancy_response?vacancyId={match.group(1)}" if match else ''
        if not target_url:
            return {"ok": False, "message": "Не удалось открыть форму для проверки резюме"}
        try:
            await session._page.goto(target_url, wait_until="domcontentloaded", timeout=30000)
            await session._page.wait_for_timeout(1000)
            current_url, response_header, questions_required, resume_select, letter_field, submit_btn = await detect_response_controls()
            if await session._apply_success_detected():
                return await finalize_success("Уже откликались ранее", already_applied=True)
            resume_verified = await select_preferred_resume()
        except Exception as exc:
            logger.warning("Resume preflight failed: %s", type(exc).__name__)
        if not resume_verified:
            await save_debug_snapshot("debug_resume_unverified")
            return {"ok": False, "message": "Нужное резюме не подтверждено — отклик не отправлен", "resume_selection_verified": False}
        logger.info("Requested resume verified before application")

    # Ищем кнопку "Откликнуться" — собираем все data-qa для дебага
    apply_btn = None if resume_verified else await session._page.query_selector(
        "[data-qa='vacancy-response-link-top-again'], "
        "[data-qa='vacancy-response-link-bottom-again'], "
        "[data-qa='vacancy-response-link-top'], "
        "[data-qa='vacancy-response-link-bottom'], "
        "a[data-qa*='response-link'], "
        "button[data-qa*='vacancy-response']"
    )

    if not apply_btn and not resume_verified:
        reapply_btn = await session._page.query_selector(
            "button:has-text('Отклик другим резюме'), "
            "a:has-text('Отклик другим резюме'), "
            "button:has-text('Откликнуться повторно'), "
            "a:has-text('Откликнуться повторно')"
        )
        if reapply_btn:
            if wants_specific_resume:
                logger.info("Found reapply button for preferred resume flow")
                apply_btn = reapply_btn
            else:
                return {"ok": True, "message": "Уже откликались ранее", "already_applied": True}

    if not apply_btn and not resume_verified:
        # Попробуем найти по тексту
        apply_btn = await session._page.query_selector(
            "button:has-text('Откликнуться'), "
            "a:has-text('Откликнуться')"
        )

    if not apply_btn:
        # Возможно уже откликались. Но если задан preferred resume, продолжаем:
        # на hh повторный отклик может быть доступен отдельной кнопкой/формой.
        if await session._has_existing_response_ui() and not wants_specific_resume:
            return {"ok": True, "message": "Уже откликались ранее", "already_applied": True}

        (
            current_url,
            response_header,
            questions_required,
            resume_select,
            letter_field,
            submit_btn,
        ) = await detect_response_controls()
        direct_response_flow = (
            "/applicant/vacancy_response" in current_url
            or response_header is not None
            or resume_select is not None
            or letter_field is not None
            or submit_btn is not None
        )

        if questions_required:
            logger.info("Vacancy requires employer questions — trying auto-answer")
            auto_question_result = await answer_questions_with_verified_resume()
            auto_answer_notes.extend(auto_question_result.get("notes") or [])
            auto_answer_question_answers.extend(auto_question_result.get("question_answers") or [])
            if auto_question_result.get("ok"):
                return await finalize_success(
                    auto_question_result.get("message", "Отклик отправлен"),
                    notes=auto_answer_notes,
                )
            return {
                "ok": False,
                "message": auto_question_result.get(
                    "message",
                    "Требуются доп. вопросы работодателя — пропускаем",
                ),
                "notes": auto_answer_notes,
                "question_answers": list(auto_answer_question_answers),
                "risky_question": auto_question_result.get("risky_question", ""),
            }

        if direct_response_flow:
            logger.info("Direct response flow detected without initial vacancy button")
        else:
            # Дебаг: какие data-qa есть на странице
            qa_attrs = await session._page.evaluate(
                "() => [...document.querySelectorAll('[data-qa]')].map(el => el.getAttribute('data-qa')).filter(a => a.includes('response') || a.includes('vacanc')).slice(0, 20)"
            )
            logger.warning("Apply button not found. Relevant data-qa: %s", qa_attrs)
            return {"ok": False, "message": f"Кнопка не найдена. qa={qa_attrs[:5]}"}
    else:
        direct_response_flow = False

    if not direct_response_flow:
        logger.info("Found apply button, clicking...")
        await apply_btn.scroll_into_view_if_needed()
        await session._page.wait_for_timeout(300)
        await apply_btn.click()
        await session._page.wait_for_timeout(3000)
        await save_debug_snapshot("debug_apply_after_click")

    if await session._apply_success_detected():
        return await finalize_success("Отклик отправлен")

    (
        current_url,
        response_header,
        questions_required,
        resume_select,
        letter_field,
        submit_btn,
    ) = await detect_response_controls()

    if questions_required:
        logger.info("Vacancy requires employer questions — trying auto-answer")
        auto_question_result = await answer_questions_with_verified_resume()
        auto_answer_notes.extend(auto_question_result.get("notes") or [])
        auto_answer_question_answers.extend(auto_question_result.get("question_answers") or [])
        if auto_question_result.get("ok"):
            return await finalize_success(
                auto_question_result.get("message", "Отклик отправлен"),
                notes=auto_answer_notes,
            )
        return {
            "ok": False,
            "message": auto_question_result.get(
                "message",
                "Требуются доп. вопросы работодателя — пропускаем",
            ),
            "notes": auto_answer_notes,
            "question_answers": list(auto_answer_question_answers),
                "risky_question": auto_question_result.get("risky_question", ""),
        }

    if (resume_select or preferred_resume_title or preferred_resume_id) and not resume_verified:
        logger.info(
            "Selecting resume in hh apply flow (title=%r, id=%r)",
            preferred_resume_title,
            preferred_resume_id,
        )
        selected = await select_preferred_resume()
        if not selected:
            return {"ok": False, "message": "Не удалось выбрать нужное резюме"}
        await session._page.wait_for_timeout(1000)
        await session._dismiss_magritte_dropdowns()
        (
            current_url,
            response_header,
            questions_required,
            resume_select,
            letter_field,
            submit_btn,
        ) = await refetch_response_controls()

    if not letter_field and cover_letter:
        await session._expand_cover_letter_input()
        letter_field, refreshed_submit_btn = await refetch_letter_field()
        if refreshed_submit_btn is not None:
            submit_btn = refreshed_submit_btn

    if not letter_field:
        (
            _,
            _,
            _,
            _,
            letter_field,
            submit_btn,
        ) = await detect_response_controls()

    if cover_letter and not letter_field:
        await save_debug_snapshot("debug_cover_letter_missing")
        return {"ok": False, "message": "Поле сопроводительного не найдено — отклик остановлен до отправки"}

    if letter_field and cover_letter:
        logger.info("Filling cover letter...")
        for attempt in range(2):
            try:
                await letter_field.scroll_into_view_if_needed()
                await session._page.wait_for_timeout(300)
                await letter_field.fill("")
                await letter_field.type(cover_letter, delay=20)
                break
            except Exception as e:
                if "not attached to the DOM" not in str(e):
                    raise
                logger.warning("Cover letter field detached from DOM, refetching controls (attempt %d)", attempt + 1)
                letter_field, refreshed_submit_btn = await refetch_letter_field()
                if refreshed_submit_btn is not None:
                    submit_btn = refreshed_submit_btn
                if not letter_field:
                    raise
        await session._page.wait_for_timeout(500)
        if normalize_text(await letter_field.input_value()) != normalize_text(cover_letter):
            return {"ok": False, "message": "Сопроводительное не сохранилось в поле — отклик остановлен"}
        cover_letter_filled = True
        await session._dismiss_magritte_dropdowns()

    if wants_specific_resume and not await selected_resume_matches(session._page, preferred_resume_id, preferred_resume_title):
        return {"ok": False, "message": "Выбранное резюме изменилось — отклик остановлен"}

    if submit_btn:
        (
            _,
            _,
            _,
            _,
            _,
            refreshed_submit_btn,
        ) = await refetch_response_controls()
        if refreshed_submit_btn is not None:
            submit_btn = refreshed_submit_btn
        await session._dismiss_magritte_dropdowns()
        if cover_letter:
            final_letter = (await refetch_response_controls())[4]
            if not final_letter or normalize_text(await final_letter.input_value()) != normalize_text(cover_letter):
                return {"ok": False, "message": "Сопроводительное изменилось перед отправкой — отклик остановлен"}
            logger.info("Cover letter verified before submit (chars=%d)", len(cover_letter))
            await save_debug_snapshot("debug_apply_before_submit")
        clicked = await session._click_with_fallbacks(submit_btn, "submit_button")
        if not clicked:
            clicked = await session._submit_response_form_via_dom()
        if not clicked:
            return {"ok": False, "message": "Не удалось нажать кнопку подтверждения"}
        await session._page.wait_for_timeout(4000)

    await save_debug_snapshot("debug_apply_after_submit")

    anti_bot_kind = await session._detect_anti_bot_kind()
    anti_bot_kind = await session._handle_anti_bot_with_solver(anti_bot_kind, stage="apply_submit")
    if anti_bot_kind:
        message = anti_bot_message(anti_bot_kind, "после отклика")
        logger.warning("HH anti-bot (%s) appeared after apply submit", anti_bot_kind)
        session._remember_antibot_signal(anti_bot_kind, "apply_submit", message)
        return {"ok": False, "message": message, "anti_bot_kind": anti_bot_kind}

    if await session._response_requires_questions():
        logger.info("Vacancy requires employer questions after submit — trying auto-answer")
        auto_question_result = await answer_questions_with_verified_resume()
        auto_answer_notes.extend(auto_question_result.get("notes") or [])
        auto_answer_question_answers.extend(auto_question_result.get("question_answers") or [])
        if auto_question_result.get("ok"):
            return await finalize_success(
                auto_question_result.get("message", "Отклик отправлен"),
                notes=auto_answer_notes,
            )
        return {
            "ok": False,
            "message": auto_question_result.get(
                "message",
                "Требуются доп. вопросы работодателя — пропускаем",
            ),
            "notes": auto_answer_notes,
            "question_answers": list(auto_answer_question_answers),
                "risky_question": auto_question_result.get("risky_question", ""),
        }

    if await session._apply_success_detected():
        return await finalize_success("Отклик отправлен", notes=auto_answer_notes)

    (
        current_url,
        response_header,
        questions_required,
        _,
        _,
        submit_btn_retry,
    ) = await detect_response_controls()
    if not questions_required and submit_btn_retry is not None:
        await session._dismiss_magritte_dropdowns()
        retried = await session._submit_response_form_via_dom()
        if retried:
            logger.info("Retrying hh submit via active DOM form after inconclusive response state")
            await session._page.wait_for_timeout(4000)
            anti_bot_kind = await session._detect_anti_bot_kind()
            anti_bot_kind = await session._handle_anti_bot_with_solver(anti_bot_kind, stage="apply_submit_retry")
            if anti_bot_kind:
                message = anti_bot_message(anti_bot_kind, "после отклика")
                logger.warning("HH anti-bot (%s) appeared after DOM submit fallback", anti_bot_kind)
                session._remember_antibot_signal(anti_bot_kind, "apply_submit_retry", message)
                return {"ok": False, "message": message, "anti_bot_kind": anti_bot_kind}
            if await session._response_requires_questions():
                logger.info("Vacancy requires employer questions after retry — trying auto-answer")
                auto_question_result = await answer_questions_with_verified_resume()
                auto_answer_notes.extend(auto_question_result.get("notes") or [])
                auto_answer_question_answers.extend(auto_question_result.get("question_answers") or [])
                if auto_question_result.get("ok"):
                    return await finalize_success(
                        auto_question_result.get("message", "Отклик отправлен"),
                        notes=auto_answer_notes,
                    )
                return {
                    "ok": False,
                    "message": auto_question_result.get(
                        "message",
                        "Требуются доп. вопросы работодателя — пропускаем",
                    ),
                    "notes": auto_answer_notes,
                    "question_answers": list(auto_answer_question_answers),
                    "risky_question": auto_question_result.get("risky_question", ""),
                }
            if await session._apply_success_detected():
                return await finalize_success("Отклик отправлен", notes=auto_answer_notes)

    anti_bot_kind = await session._detect_anti_bot_kind()
    if anti_bot_kind:
        message = anti_bot_message(anti_bot_kind, "после отклика")
        logger.warning("HH anti-bot (%s) detected while verifying apply", anti_bot_kind)
        session._remember_antibot_signal(anti_bot_kind, "apply_verify", message)
        return {"ok": False, "message": message, "anti_bot_kind": anti_bot_kind}

    if await response_error_detected(session, logger=logger):
        logger.warning("hh.ru error page after apply. URL: %s", session._page.url)
        return {"ok": False, "message": "hh.ru показал ошибку после отклика"}

    logger.warning("Apply verification failed. URL: %s", session._page.url)
    await save_debug_snapshot("debug_apply_verification_failed")
    return {"ok": False, "message": "Не удалось подтвердить отклик"}
