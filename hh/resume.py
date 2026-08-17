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


async def _save_resume_boost_debug(
    session,
    stage: str,
    *,
    settings=config,
    logger=log,
) -> dict:
    paths = {
        "debug_screenshot": os.path.join(
            settings.HH_STATE_DIR,
            f"debug_resume_boost_{stage}.png",
        ),
        "debug_html": os.path.join(
            settings.HH_STATE_DIR,
            f"debug_resume_boost_{stage}.html",
        ),
    }
    try:
        await session._page.screenshot(path=paths["debug_screenshot"], full_page=True)
        html = await session._page.content()
        with open(paths["debug_html"], "w", encoding="utf-8") as f:
            f.write(html)
    except Exception as e:
        logger.debug("Resume boost debug save failed: %s", e)
    return paths


async def _element_text_summary(element) -> str:
    parts = []
    for getter in (
        lambda: element.inner_text(),
        lambda: element.get_attribute("aria-label"),
        lambda: element.get_attribute("title"),
        lambda: element.get_attribute("data-qa"),
    ):
        try:
            value = await getter()
            if value:
                parts.append(str(value))
        except Exception:
            pass
    return " ".join(" ".join(parts).split())


async def _element_is_disabled(element) -> bool:
    try:
        disabled = await element.get_attribute("disabled")
        aria_disabled = await element.get_attribute("aria-disabled")
        return disabled is not None or str(aria_disabled or "").casefold() == "true"
    except Exception:
        return False


async def _find_resume_boost_scope(
    session,
    resume_id: str = "",
    resume_title: str = "",
    *,
    normalize_text=_normalize_text,
):
    cards = await session._page.query_selector_all(
        "[data-qa='resume'], [data-qa^='resume-card'], [data-qa*='resume-card']"
    )
    if not cards:
        return None

    if not resume_id and not resume_title:
        return cards[0]

    target_id = str(resume_id or "").strip()
    target_title = normalize_text(resume_title)
    for card in cards:
        try:
            text = normalize_text(await card.inner_text())
            links = await card.query_selector_all("a[href*='/resume/']")
            hrefs = []
            for link in links:
                hrefs.append(await link.get_attribute("href") or "")
            href_text = " ".join(hrefs)
            if target_id and target_id in href_text:
                return card
            if target_title and (target_title in text or text in target_title):
                return card
        except Exception:
            continue
    return None


async def _find_resume_boost_action(
    session,
    resume_id: str = "",
    resume_title: str = "",
    *,
    looks_like_action=_looks_like_resume_boost_action,
) -> dict:
    scopes = []
    scope = await session._find_resume_boost_scope(resume_id, resume_title)
    if scope:
        scopes.append(scope)
    scopes.append(session._page)

    for current_scope in scopes:
        try:
            elements = await current_scope.query_selector_all("button, a, [role='button']")
        except Exception:
            continue
        for element in elements:
            summary = await session._element_text_summary(element)
            if not looks_like_action(summary):
                continue
            return {
                "element": element,
                "button_text": summary[:240],
                "disabled": await session._element_is_disabled(element),
            }
    return {}


async def _inspect_resume_boost(
    session,
    resume_id: str = "",
    resume_title: str = "",
    *,
    resume_matches_target=_resume_matches_target,
    looks_like_unavailable=_looks_like_resume_boost_unavailable,
) -> dict:
    resumes = await session.get_resume_ids()
    target = None
    target_requested = bool(resume_id or resume_title)
    if target_requested:
        target = next(
            (r for r in resumes if resume_matches_target(r, resume_id, resume_title)),
            None,
        )
    elif resumes:
        target = resumes[0]

    target_id = str((target or {}).get("id") or resume_id or "").strip()
    target_title = str((target or {}).get("title") or resume_title or "").strip()
    body_text = await session._page_text(limit=20000)
    debug_paths = await session._save_resume_boost_debug("status")

    detail = {
        "ok": True,
        "can_boost": False,
        "reason": "boost_action_not_found",
        "resume_id": target_id,
        "title": target_title,
        "button_text": "",
        "resumes_found": len(resumes),
        "url": session._page.url,
        **debug_paths,
    }
    if not resumes and not target_requested:
        detail["ok"] = False
        detail["reason"] = "resume_not_found"
        return detail
    if target_requested and not target:
        detail["ok"] = False
        detail["reason"] = "resume_target_not_found"
        return detail

    action = await session._find_resume_boost_action(target_id, target_title)
    if action:
        detail["button_text"] = action.get("button_text", "")
        detail["_element"] = action.get("element")
        if action.get("disabled"):
            detail["reason"] = "boost_action_disabled"
        else:
            detail["can_boost"] = True
            detail["reason"] = "boost_action_available"
        return detail
    if looks_like_unavailable(body_text):
        detail["reason"] = "boost_unavailable_or_cooldown"
    return detail


async def get_resume_boost_status(
    session,
    resume_id: str = "",
    resume_title: str = "",
) -> dict:
    """Проверить наличие кнопки поднятия резюме без клика."""
    detail = await session._inspect_resume_boost(
        resume_id=resume_id,
        resume_title=resume_title,
    )
    detail.pop("_element", None)
    return detail


async def boost_resume(
    session,
    resume_id: str = "",
    resume_title: str = "",
    confirm: str = "",
    *,
    settings=config,
    looks_like_success=_looks_like_resume_boost_success,
) -> dict:
    """Поднять резюме вручную. Требует env-флаг и явное слово подтверждения."""
    detail = await session._inspect_resume_boost(
        resume_id=resume_id,
        resume_title=resume_title,
    )
    element = detail.pop("_element", None)
    if not settings.HH_RESUME_BOOST_ENABLED:
        detail.update({"ok": False, "can_boost": False, "reason": "boost_disabled_by_config"})
        return detail
    if confirm != settings.HH_RESUME_BOOST_CONFIRM_TEXT:
        detail.update({"ok": False, "can_boost": False, "reason": "boost_confirmation_required"})
        return detail
    if not (resume_id or resume_title) and int(detail.get("resumes_found") or 0) > 1:
        detail.update({"ok": False, "can_boost": False, "reason": "boost_target_required"})
        return detail
    if not detail.get("can_boost") or not element:
        return detail

    clicked = await session._click_with_fallbacks(element, "resume_boost")
    await session._page.wait_for_timeout(2500)
    post_text = await session._page_text(limit=20000)
    debug_paths = await session._save_resume_boost_debug("after_click")
    detail.update(debug_paths)
    if clicked and looks_like_success(post_text):
        detail.update({"ok": True, "can_boost": False, "reason": "boost_success"})
    elif clicked:
        detail.update({"ok": True, "can_boost": False, "reason": "boost_clicked_check_debug"})
    else:
        detail.update({"ok": False, "can_boost": False, "reason": "boost_click_failed"})
    return detail
