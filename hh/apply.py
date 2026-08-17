"""Helpers for the HH vacancy application flow."""

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
