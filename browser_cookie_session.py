"""Cookie ownership/lifecycle shared by non-HH browser clients.

No browser/network await holds a filesystem lock. Login/submission decisions
remain source-specific; a cookie list does not prove authentication.
"""
from dataclasses import dataclass
from pathlib import Path

from state_store.browser_cookies import CookieRepository, CookieStateError, validate_cookies


@dataclass
class CookieBinding:
    revision: object
    had_cookies: bool
    context: object = None
    closing: bool = False
    revoked: bool = False


class BrowserCookieSession:
    def __init__(self, cookies_file, state_dir):
        self.repository = CookieRepository(cookies_file)
        self.state_dir = Path(state_dir).absolute()
        self.starting = False
        self.binding = None
        self.nonce = None

    async def start(self, client, *, playwright_factory, launch_options, logger):
        if self.starting:
            raise RuntimeError("Browser is already starting")
        if client._page is not None:
            if (self.binding is None or self.binding.context is not client._context
                    or self.binding.closing or self.binding.revoked):
                raise CookieStateError("Browser cookie ownership is closed or superseded")
            return
        if any(getattr(client, field, None) is not None for field in ("_pw", "_browser", "_context")):
            raise RuntimeError("Browser is already active")
        cookies, revision = self.repository.snapshot()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        binding = CookieBinding(revision, bool(cookies))
        self.binding = binding
        self.nonce = object()
        self.starting = True
        try:
            client._pw = await playwright_factory().start()
            client._browser = await client._pw.chromium.launch(**launch_options)
            client._context = await client._browser.new_context(
                viewport={"width": 1280, "height": 900},
                user_agent=("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"),
                locale="ru-RU",
            )
            binding.context = client._context
            if cookies:
                await client._context.add_cookies(cookies)
                logger.info("Loaded %d browser cookies", len(cookies))
            client._page = await client._context.new_page()
        except BaseException:
            self.starting = False
            try:
                await self.stop(client, logger=logger, persist=False)
            except BaseException as exc:
                logger.debug("Browser startup cleanup failed: %s", type(exc).__name__)
            raise
        finally:
            self.starting = False

    def _persist(self, client, context, binding, nonce, revision, cookies, *, shutdown=False):
        if (client._context is not context or self.binding is not binding or self.nonce is not nonce
                or binding is None or binding.context is not context or binding.revoked):
            raise CookieStateError("Browser cookie ownership changed during capture")
        if not isinstance(cookies, list):
            raise CookieStateError("Browser returned an invalid cookie collection")
        validate_cookies(cookies)
        if shutdown and binding.had_cookies and not cookies:
            raise CookieStateError("Refusing to erase a cached session during shutdown")
        try:
            binding.revision = self.repository.save(cookies, expected_revision=revision)
            binding.had_cookies = bool(cookies)
        except BaseException:
            binding.revoked = True
            raise

    async def save(self, client, *, logger):
        if self.starting:
            raise RuntimeError("Browser startup is still in progress")
        context = client._context
        if context is None:
            return
        binding = self.binding
        if binding is None or binding.closing or binding.revoked:
            raise CookieStateError("Browser cookie ownership is unavailable or superseded")
        nonce = self.nonce = object()
        revision = binding.revision
        cookies = await context.cookies()
        self._persist(client, context, binding, nonce, revision, cookies)
        logger.info("Browser session saved (%d cookies)", len(cookies))

    async def stop(self, client, *, logger, persist=True):
        if self.starting:
            raise RuntimeError("Browser startup is still in progress")
        context, browser, pw, page = client._context, client._browser, client._pw, client._page
        binding = self.binding
        if binding is not None:
            if binding.closing:
                return
            binding.closing = True
        nonce = self.nonce = object()
        revision = binding.revision if binding is not None else None
        try:
            if context is not None and persist:
                try:
                    cookies = await context.cookies()
                    self._persist(client, context, binding, nonce, revision, cookies, shutdown=True)
                except Exception as exc:
                    logger.debug("Skipping browser cookie save during shutdown: %s", type(exc).__name__)
        finally:
            try:
                if browser is not None:
                    await browser.close()
            finally:
                try:
                    if pw is not None:
                        await pw.stop()
                finally:
                    for field, captured in (("_context", context), ("_browser", browser), ("_pw", pw), ("_page", page)):
                        if getattr(client, field, None) is captured:
                            setattr(client, field, None)
                    if self.binding is binding:
                        self.binding = None
