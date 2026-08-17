"""Helpers for the HH vacancy application flow."""

import os

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
        "button:has-text('Вы откликнулись')",
        "a:has-text('Вы откликнулись')",
        "text='Вы откликнулись'",
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
        body_text = await session._page_text(limit=8000)
        return looks_like_existing_response(body_text)
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
        "button:has-text('Вы откликнулись')",
        "a:has-text('Вы откликнулись')",
        "text='Вы откликнулись'",
        "text='Резюме доставлено'",
        "text='Отклик отправлен'",
        "text='Связаться с работодателем можно в чате'",
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

    page_text = await session._page_text(limit=12000)
    return looks_like_apply_success(page_text)


async def response_requires_questions(session, current_url: str = "", *, logger) -> bool:
    page_url = (current_url or session._page.url or "").lower()
    if "vacancy_response_question" in page_url:
        return True

    selectors = (
        "h1:has-text('Ответьте на вопросы')",
        "h2:has-text('Ответьте на вопросы')",
        "text='Ответьте на вопросы'",
        "text='Для отклика необходимо ответить на несколько вопросов работодателя'",
    )
    try:
        for selector in selectors:
            marker = await session._page.query_selector(selector)
            if marker:
                return True
    except Exception as exc:
        logger.debug("Question flow selector check failed: %s", exc)

    page_text = normalize_text(await session._page_text(limit=12000))
    return (
        "ответьте на вопросы" in page_text
        or "для отклика необходимо ответить на несколько вопросов работодателя" in page_text
    )


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
                    const form = document.querySelector("form[name='vacancy_response']");
                    if (form && typeof form.requestSubmit === 'function') {
                        form.requestSubmit();
                        return true;
                    }
                    const button = document.querySelector("[data-qa='vacancy-response-submit-popup']");
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
    submit_btn = await session._page.query_selector(
        "[data-qa='vacancy-response-submit-popup'], "
        "[data-qa='vacancy-response-letter-submit'], "
        "button[data-qa*='submit'], "
        "[data-qa='vacancy-response-link-top-again'], "
        "[data-qa='vacancy-response-link-bottom-again'], "
        "[data-qa='vacancy-response-link-top'], "
        "[data-qa='vacancy-response-link-bottom'], "
        "a[data-qa*='response-link']"
    )
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
