from __future__ import annotations

import json
import logging
import os

from playwright.async_api import async_playwright

try:
    from playwright_stealth import Stealth  # type: ignore
    _STEALTH_AVAILABLE = True
except ImportError:
    Stealth = None  # type: ignore
    _STEALTH_AVAILABLE = False

import config
import proxy_utils

log = logging.getLogger("hh_client")
HH_AUTH_COOKIE_NAMES = {"hhtoken", "hhuid", "crypted_hhuid", "crypted_id"}


def _ensure_dirs():
    os.makedirs(os.path.dirname(config.HH_COOKIES_FILE), exist_ok=True)
    os.makedirs(config.HH_STATE_DIR, exist_ok=True)


def _load_cookies() -> list[dict] | None:
    if os.path.exists(config.HH_COOKIES_FILE):
        with open(config.HH_COOKIES_FILE) as f:
            return json.load(f)
    return None


def _save_cookies(cookies: list[dict]):
    _ensure_dirs()
    with open(config.HH_COOKIES_FILE, "w") as f:
        json.dump(cookies, f, ensure_ascii=False, indent=2)


async def start_browser(
    session,
    headless: bool | None = None,
    *,
    settings=config,
    playwright_factory=async_playwright,
    stealth_available: bool = _STEALTH_AVAILABLE,
    stealth_factory=Stealth,
    proxy_env_builder=proxy_utils.browser_launch_env,
    ensure_dirs=_ensure_dirs,
    load_cookies=_load_cookies,
    logger=log,
):
    ensure_dirs()
    session._pw = await playwright_factory().start()

    launch_opts = {
        "headless": headless if headless is not None else settings.HEADLESS,
        "slow_mo": settings.SLOW_MO,
    }
    # Прокси (Mihomo на 127.0.0.1:7897) — если hh.ru не грузится напрямую
    proxy_url = os.environ.get("HH_PROXY", settings.BROWSER_PROXY)
    if proxy_url:
        launch_opts["proxy"] = {"server": proxy_url}
        logger.info("Using proxy: %s", proxy_url)
    launch_opts["env"] = proxy_env_builder(proxy_url)

    session._browser = await session._pw.chromium.launch(**launch_opts)
    session._context = await session._browser.new_context(
        viewport={"width": 1280, "height": 900},
        user_agent=(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
        ),
        locale="ru-RU",
    )
    cookies = load_cookies()
    if cookies:
        await session._context.add_cookies(cookies)
        logger.info("Loaded %d cookies", len(cookies))
    session._page = await session._context.new_page()

    # Anti-bot: применяем stealth-патчи к контексту/странице, чтобы hh.ru
    # не палил navigator.webdriver и прочие headless-маркеры.
    if stealth_available:
        try:
            stealth = stealth_factory()
            await stealth.apply_stealth_async(session._context)
            logger.info("playwright-stealth applied to context")
        except Exception as exc:
            logger.warning("playwright-stealth failed: %s", exc)


async def stop_browser(session, *, save_cookies=_save_cookies):
    context = getattr(session, "_context", None)
    browser = getattr(session, "_browser", None)
    playwright = getattr(session, "_pw", None)
    try:
        if context:
            try:
                cookies = await context.cookies()
                save_cookies(cookies)
            except Exception as exc:
                # Ctrl-C or an externally closed page may tear down the
                # Playwright context before the owning task reaches cleanup.
                log.debug("skip HH cookie save during browser shutdown: %s", exc)
    finally:
        try:
            if browser:
                await browser.close()
        finally:
            if playwright:
                await playwright.stop()
            session._context = None
            session._browser = None
            session._pw = None
            session._page = None


async def save_session(session, *, save_cookies=_save_cookies, logger=log):
    if session._context:
        cookies = await session._context.cookies()
        save_cookies(cookies)
        logger.info("Session saved (%d cookies)", len(cookies))


async def has_auth_cookies(
    session,
    *,
    base_url: str = config.HH_BASE_URL,
    auth_cookie_names=HH_AUTH_COOKIE_NAMES,
) -> bool:
    if not session._context:
        return False
    try:
        cookies = await session._context.cookies([base_url])
    except TypeError:
        cookies = await session._context.cookies()
    names = {(item.get("name") or "").casefold() for item in cookies or []}
    return "hhtoken" in names and bool(names & auth_cookie_names)
