from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import async_playwright

try:
    from playwright_stealth import Stealth  # type: ignore
    _STEALTH_AVAILABLE = True
except ImportError:
    Stealth = None  # type: ignore
    _STEALTH_AVAILABLE = False

import config
import proxy_utils
from state_store.hh_cookies import HHCookieRepository, HHCookieStateError, validate_cookies

log = logging.getLogger("hh_client")
HH_AUTH_COOKIE_NAMES = {"hhtoken", "hhuid", "crypted_hhuid", "crypted_id"}


@dataclass(frozen=True)
class CookiePaths:
    cookies_file: str
    state_dir: str

    @classmethod
    def capture(cls, settings=config):
        return cls(os.path.abspath(os.fspath(getattr(settings, "HH_COOKIES_FILE", config.HH_COOKIES_FILE))),
                   os.path.abspath(os.fspath(getattr(settings, "HH_STATE_DIR", config.HH_STATE_DIR))))


@dataclass
class CookieBinding:
    repository: HHCookieRepository
    revision: object
    had_auth: bool
    context: object = None
    closing: bool = False
    revoked: bool = False


@dataclass(frozen=True)
class BoundCookieWriter:
    """Explicit native destination; arbitrary callbacks are not account proof."""
    repository: HHCookieRepository


def _ensure_dirs(paths: CookiePaths | None = None):
    paths = paths or CookiePaths.capture()
    Path(paths.cookies_file).parent.mkdir(parents=True, exist_ok=True)
    Path(paths.state_dir).mkdir(parents=True, exist_ok=True)


def _load_cookies(paths: CookiePaths | None = None) -> list[dict] | None:
    paths = paths or CookiePaths.capture()
    return HHCookieRepository(paths.cookies_file).snapshot()[0]


def _save_cookies(cookies: list[dict], paths: CookiePaths | None = None):
    """Explicit synchronous import; browser workflows use bound CAS instead."""
    paths = paths or CookiePaths.capture()
    _ensure_dirs(paths)
    HHCookieRepository(paths.cookies_file).save(cookies)


def _persist_captured(session, context, binding, nonce, revision, cookies, save_cookies, *, shutdown=False):
    if session._context is not context or getattr(session, "_cookie_write_nonce", None) is not nonce:
        raise HHCookieStateError("HH browser changed during cookie capture")
    if binding is not None and (binding is not getattr(session, "_cookie_binding", None)
            or binding.context is not context or binding.revoked
            or (binding.closing and not shutdown)):
        raise HHCookieStateError("HH cookie session ownership unavailable")
    if not isinstance(cookies, list):
        raise HHCookieStateError("Browser returned an invalid HH cookie collection")
    validate_cookies(cookies)
    if shutdown and binding is not None and binding.had_auth and not any(item.get("name", "").lower() == "hhtoken" for item in cookies):
        raise HHCookieStateError("Refusing to erase cached HH auth during shutdown")
    if binding is None:
        if save_cookies is _save_cookies or isinstance(save_cookies, BoundCookieWriter):
            raise HHCookieStateError("HH cookie session ownership unavailable")
        # Unbound synthetic adapters may retain their own in-memory callback.
        save_cookies(cookies)
        return
    if save_cookies is not _save_cookies:
        if (type(save_cookies) is not BoundCookieWriter
                or os.path.abspath(os.fspath(save_cookies.repository.path))
                != os.path.abspath(os.fspath(binding.repository.path))):
            raise HHCookieStateError("Injected HH writer has no captured native destination")
    try:
        binding.revision = binding.repository.save(cookies, expected_revision=revision)
        binding.had_auth = any(item.get("name", "").lower() == "hhtoken" for item in cookies)
    except BaseException:
        # Replacement may already be visible if directory fsync failed. Never
        # adopt an unknown new revision or retry from this old browser.
        binding.revoked = True
        raise


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
    terminate=None,
):
    if getattr(session, "_starting", False) or any(getattr(session, field, None) is not None
            for field in ("_pw", "_browser", "_context")):
        raise RuntimeError("HH browser is already active or starting")
    if terminate is None:
        from hh.termination import require_termination_capability
        require_termination_capability()
    paths = getattr(session, "_cookie_paths", None) or CookiePaths.capture(settings)
    session._cookie_paths = paths
    if ensure_dirs is _ensure_dirs:
        ensure_dirs(paths)
    else:
        ensure_dirs()
    if load_cookies is _load_cookies:
        repository = HHCookieRepository(paths.cookies_file)
        cookies, revision = repository.snapshot()
        binding = CookieBinding(repository, revision,
                                any(item.get("name", "").lower() == "hhtoken" for item in cookies or []))
    else:
        cookies, binding = load_cookies(), None
    session._cookie_binding = binding
    session._cookie_write_nonce = object()
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

    session._starting = True
    try:
        session._pw = await playwright_factory().start()
        session._browser = await session._pw.chromium.launch(**launch_opts)
        session._browser._hh_owner_session = session
        session._context = await session._browser.new_context(
            service_workers="block",
            viewport={"width": 1280, "height": 900},
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
            ),
            locale="ru-RU",
        )
        from hh.recovery import BLOCK_WORKER_CONSTRUCTORS
        await session._context.add_init_script(script=BLOCK_WORKER_CONSTRUCTORS)
        session._hh_blocked_worker_context = session._context
        session._context._hh_workers_owner = session
        if binding is not None:
            binding.context = session._context
        if cookies:
            await session._context.add_cookies(cookies)
            logger.info("Loaded %d cookies", len(cookies))
        session._page = await session._context.new_page()
        session._recovering = False
        session._hh_recovery_stop_reason = ""
        session._hh_recovery_uncertain = False
        from hh.recovery import monitor_page
        monitor_page(session, session._page)
        from browser_action_boundary import bootstrap_boundary
        await bootstrap_boundary(session._page)

        # Anti-bot patches belong to this captured context.
        if stealth_available:
            try:
                stealth = stealth_factory()
                await stealth.apply_stealth_async(session._context)
                logger.info("playwright-stealth applied to context")
            except Exception as exc:
                logger.warning("playwright-stealth failed: %s", type(exc).__name__)
    except BaseException:
        session._starting = False
        await stop_browser(session, save_cookies=None, terminate=terminate)
        raise
    finally:
        session._starting = False


async def stop_browser(session, *, save_cookies=_save_cookies, terminate=None):
    """Every caller awaits the complete shutdown of the same captured browser."""
    if getattr(session, "_starting", False):
        raise RuntimeError("HH browser startup is still in progress")
    context, browser = getattr(session, '_context', None), getattr(session, '_browser', None)
    pending = getattr(session, '_hh_shutdown_operation', None)
    if pending is None or pending[0] is not context or pending[1] is not browser:
        binding = getattr(session, '_cookie_binding', None)
        if binding is not None:
            binding.closing = True
        captured = (context, browser, getattr(session, '_pw', None), getattr(session, '_page', None), binding)
        operation = asyncio.create_task(_stop_browser_owned(session, captured, save_cookies=save_cookies, terminate=terminate))
        pending = (context, browser, operation)
        session._hh_shutdown_operation = pending
    from hh.termination import await_owned_completion
    return await await_owned_completion(pending[2])


async def _stop_browser_owned(session, captured, *, save_cookies, terminate):
    context, browser, playwright, page, binding = captured
    if browser is not None and terminate is None:
        if (getattr(browser, '_hh_owner_session', None) is not session
                or (context is not None and context.browser is not browser)):
            raise RuntimeError('HH captured browser ownership is unproven')
    nonce = object()
    session._cookie_write_nonce = nonce
    revision = binding.revision if binding is not None else None
    try:
        if context:
            try:
                if save_cookies is not None:
                    cookies = await asyncio.wait_for(context.cookies(), 5)
                    _persist_captured(session, context, binding, nonce, revision, cookies,
                                      save_cookies, shutdown=True)
            except Exception as exc:
                # Ctrl-C or an externally closed page may tear down the
                # Playwright context before the owning task reaches cleanup.
                log.debug("skip HH cookie save during browser shutdown: %s", type(exc).__name__)
    finally:
        # Graceful Browser.close also runs OOPIF unload/pagehide handlers.
        # Crash only the captured local browser and prove process death before
        # stopping its driver or releasing the session's resource ownership.
        if browser:
            from hh.termination import terminate_browser
            if await (terminate or terminate_browser)(browser) is not True:
                raise RuntimeError("HH browser termination was not proven")
        try:
            if playwright:
                await asyncio.wait_for(playwright.stop(), 5)
        finally:
            for field, captured in (("_context", context), ("_browser", browser),
                                    ("_pw", playwright), ("_page", page), ("_cookie_binding", binding)):
                if getattr(session, field, None) is captured:
                    setattr(session, field, None)


async def save_session(session, *, save_cookies=_save_cookies, logger=log):
    if getattr(session, "_starting", False):
        raise RuntimeError("HH browser startup is still in progress")
    context = session._context
    if context:
        binding = getattr(session, "_cookie_binding", None)
        if binding is not None and (binding.closing or binding.revoked):
            raise HHCookieStateError("HH cookie session is closed or superseded")
        nonce = object()
        session._cookie_write_nonce = nonce
        revision = binding.revision if binding is not None else None
        cookies = await context.cookies()
        _persist_captured(session, context, binding, nonce, revision, cookies, save_cookies)
        logger.info("Session saved (%d cookies)", len(cookies))


async def has_auth_cookies(
    session,
    *,
    base_url: str = config.HH_BASE_URL,
    auth_cookie_names=HH_AUTH_COOKIE_NAMES,
) -> bool:
    context = session._context
    if not context:
        return False
    try:
        cookies = await context.cookies([base_url])
    except TypeError:
        cookies = await context.cookies()
    if session._context is not context:
        return False
    names = {(item.get("name") or "").casefold() for item in cookies or []}
    return "hhtoken" in names and bool(names & auth_cookie_names)
