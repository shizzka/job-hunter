"""Resume status and boost helpers for HH."""

import logging
import os

import config
from hh.text import normalize_text as _normalize_text

log = logging.getLogger("hh_client")


def _looks_like_resume_boost_action(value: str) -> bool:
    text = _normalize_text(value)
    if not text:
        return False
    if "поднять" in text:
        return "резюме" in text or len(text) <= 80
    return (
        "обновить дату" in text
        or "обновить резюме" in text
        or "обновить в поиске" in text
        or "поднять в поиске" in text
    )


def _looks_like_resume_boost_unavailable(value: str) -> bool:
    text = _normalize_text(value)
    return (
        "можно будет поднять" in text
        or "поднять можно" in text
        or "следующее поднятие" in text
        or "станет доступно" in text
        or "будет доступно" in text
        or "уже поднято" in text
    )


def _looks_like_resume_boost_success(value: str) -> bool:
    text = _normalize_text(value)
    return (
        "резюме поднято" in text
        or "резюме обновлено" in text
        or "поднято в поиске" in text
        or "обновлено в поиске" in text
    )


def _resume_matches_target(resume: dict, resume_id: str = "", resume_title: str = "") -> bool:
    target_id = str(resume_id or "").strip()
    target_title = _normalize_text(resume_title)
    current_id = str((resume or {}).get("id") or "").strip()
    current_title = _normalize_text(str((resume or {}).get("title") or ""))
    current_url = str((resume or {}).get("url") or "")
    return (
        bool(target_id and (target_id == current_id or target_id in current_url))
        or bool(target_title and (target_title in current_title or current_title in target_title))
    )


async def get_resume_ids(
    session,
    *,
    anti_bot_message,
    settings=config,
    logger=log,
) -> list[dict]:
    """Получить ID резюме пользователя."""
    try:
        await session._page.goto(
            f"{settings.HH_BASE_URL}/applicant/resumes",
            wait_until="domcontentloaded",
            timeout=30000,
        )
    except Exception as e:
        logger.warning("Resume page navigation issue: %s", e)

    await session._page.wait_for_timeout(4000)
    await session._dismiss_whats_new_modal()

    # Дебаг: скриншот и URL
    current_url = session._page.url
    logger.info("Resume page URL: %s", current_url)
    debug_screenshot = os.path.join(settings.HH_STATE_DIR, "debug_resumes.png")
    debug_html = os.path.join(settings.HH_STATE_DIR, "debug_resumes.html")
    try:
        await session._page.screenshot(path=debug_screenshot)
        html = await session._page.content()
        with open(debug_html, "w") as f:
            f.write(html)
        logger.info("Debug saved: %s, %s", debug_screenshot, debug_html)
    except Exception as e:
        logger.debug("Debug save failed: %s", e)

    anti_bot_kind = await session._detect_anti_bot_kind()
    if anti_bot_kind:
        message = anti_bot_message(anti_bot_kind, "на странице резюме")
        session._remember_antibot_signal(anti_bot_kind, "resume_page", message)
        logger.warning(
            "hh.ru anti-bot (%s) on resume page: %s",
            anti_bot_kind,
            session._page.url,
        )
        return []

    resumes = []

    # Стратегия 1: data-qa селекторы (новый дизайн: resume-card-link-*)
    cards = await session._page.query_selector_all(
        "[data-qa='resume'], [data-qa^='resume-card-link-']"
    )
    logger.info("Strategy 1 (data-qa='resume'/resume-card-link): %d cards", len(cards))

    # Стратегия 2: ссылки с /resume/ в href
    if not cards:
        cards = await session._page.query_selector_all("a[href*='/resume/']")
        logger.info("Strategy 2 (a[href*='/resume/']): %d links", len(cards))
        seen_ids = set()
        for link in cards:
            href = await link.get_attribute("href") or ""
            if "/resume/" not in href:
                continue
            resume_id = href.split("/resume/")[-1].split("?")[0].split("/")[0]
            if not resume_id or resume_id in seen_ids:
                continue
            seen_ids.add(resume_id)
            title = (await link.inner_text()).strip() or resume_id
            if not title or len(title) > 200:
                title = resume_id
            resumes.append({"id": resume_id, "title": title, "url": href})
        return resumes

    # Стратегия 1 продолжение: парсим карточки
    # Селектор хватает и контейнер [data-qa='resume'], и ссылку
    # [data-qa^='resume-card-link-'] внутри той же карточки → дедуп по id.
    seen_ids: set[str] = set()
    for card in cards:
        title_el = await card.query_selector(
            "[data-qa='resume-title'], "
            "a[data-qa*='title'], "
            "a[href*='/resume/']"
        )
        card_href = await card.get_attribute("href") or ""
        href = card_href
        title = ""
        if title_el:
            title = (await title_el.inner_text()).strip()
            href = (await title_el.get_attribute("href") or "") or href
        if not href:
            link_el = await card.query_selector("a[href*='/resume/']")
            if link_el:
                href = await link_el.get_attribute("href") or ""
        if not title:
            title = (await card.inner_text()).strip()
        resume_id = ""
        if "/resume/" in href:
            resume_id = href.split("/resume/")[-1].split("?")[0].split("/")[0]
        if not resume_id or resume_id in seen_ids:
            continue
        seen_ids.add(resume_id)
        title = " ".join((title or resume_id).split())
        if len(title) > 240:
            title = title[:237].rstrip() + "..."
        resumes.append({"id": resume_id, "title": title, "url": href})

    return resumes
