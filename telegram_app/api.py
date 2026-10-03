"""Low-level asynchronous client for the Telegram Bot API."""

from __future__ import annotations

import asyncio
import json
import logging
import time

import aiohttp

import config
from telegram_app.formatting import limit_telegram_text


_PROXY_DIRECT_FALLBACK_SECONDS = 60.0
_TRANSPORT_ERRORS = (aiohttp.ClientError, asyncio.TimeoutError, OSError)


class TelegramAPIError(RuntimeError):
    """Telegram returned a valid HTTP/API error response."""

    def __init__(self, method: str, status: int, payload: dict | None = None) -> None:
        self.method = method
        self.status = int(status)
        self.payload = payload or {}
        parameters = self.payload.get("parameters") or {}
        try:
            self.retry_after = max(0, int(parameters.get("retry_after") or 0))
        except (TypeError, ValueError):
            self.retry_after = 0
        super().__init__(f"{method} failed: {self.status} {self.payload}")


def telegram_error_summary(error: BaseException) -> str:
    """Safe diagnostics: request URLs and response payloads may contain secrets."""
    kind = type(error).__name__
    if isinstance(error, TelegramAPIError):
        return f"{kind} (HTTP {error.status})"
    return kind


class TelegramAPIClient:
    """Own Telegram HTTP sessions, proxy fallback, and API serialization."""

    def __init__(self, *, logger: logging.Logger | None = None) -> None:
        self._sessions: dict[bool, aiohttp.ClientSession] = {}
        self._force_direct = False
        self._proxy_retry_at = 0.0
        self._api_log = logger or logging.getLogger(__name__)

    def _should_use_proxy(self) -> bool:
        if not config.TELEGRAM_PROXY:
            return False
        if self._force_direct and time.monotonic() >= self._proxy_retry_at:
            self._force_direct = False
            self._proxy_retry_at = 0.0
        return not self._force_direct

    def _mark_proxy_transport_failure(self) -> None:
        self._force_direct = True
        self._proxy_retry_at = time.monotonic() + _PROXY_DIRECT_FALLBACK_SECONDS

    async def _call_with_rate_limit_retry(self, call):
        try:
            return await call()
        except TelegramAPIError as exc:
            if exc.status != 429 or not exc.retry_after:
                raise
            self._api_log.warning(
                "Telegram rate limit for %s; retrying in %ss",
                exc.method,
                exc.retry_after,
            )
            await asyncio.sleep(exc.retry_after)
            return await call()

    async def _get_session(
        self,
        use_proxy: bool,
        *,
        timeout: int = 70,
    ) -> aiohttp.ClientSession:
        session = self._sessions.get(use_proxy)
        if session and not session.closed:
            return session
        connector = None
        if use_proxy and config.TELEGRAM_PROXY:
            try:
                from aiohttp_socks import ProxyConnector

                connector = ProxyConnector.from_url(config.TELEGRAM_PROXY)
            except ImportError:
                self._api_log.warning(
                    "aiohttp-socks not installed, proxy disabled for bot"
                )
                use_proxy = False
        session = aiohttp.ClientSession(
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=timeout),
        )
        self._sessions[use_proxy] = session
        return session

    async def _api_request(
        self,
        method: str,
        payload: dict,
        *,
        timeout: int = 70,
    ) -> dict | list:
        use_proxy = self._should_use_proxy()
        try:
            return await self._call_with_rate_limit_retry(
                lambda: self._api_request_once(
                    method,
                    payload,
                    use_proxy=use_proxy,
                    timeout=timeout,
                )
            )
        except _TRANSPORT_ERRORS as exc:
            if not use_proxy:
                raise
            self._api_log.warning(
                "Telegram proxy failed for bot, retrying direct: %s",
                telegram_error_summary(exc),
            )
            self._mark_proxy_transport_failure()
            return await self._call_with_rate_limit_retry(
                lambda: self._api_request_once(
                    method,
                    payload,
                    use_proxy=False,
                    timeout=timeout,
                )
            )

    async def _api_request_once(
        self,
        method: str,
        payload: dict,
        *,
        use_proxy: bool,
        timeout: int,
    ) -> dict | list:
        session = await self._get_session(use_proxy, timeout=timeout)
        url = (
            f"https://api.telegram.org/"
            f"bot{config.TELEGRAM_CONTROL_BOT_TOKEN}/{method}"
        )
        async with session.post(url, json=payload) as response:
            data = await response.json(content_type=None)
            if response.status != 200 or not data.get("ok", False):
                raise TelegramAPIError(method, response.status, data)
            return data.get("result", {})

    async def _download_file(self, file_id: str) -> bytes:
        """Download a Telegram file referenced by file_id."""
        meta = await self._api_request("getFile", {"file_id": file_id}, timeout=30)
        file_path = str((meta or {}).get("file_path") or "").strip()
        if not file_path:
            raise RuntimeError("Telegram getFile returned no file_path")
        use_proxy = self._should_use_proxy()
        try:
            return await self._call_with_rate_limit_retry(
                lambda: self._download_file_once(file_path, use_proxy=use_proxy)
            )
        except _TRANSPORT_ERRORS as exc:
            if not use_proxy:
                raise
            self._api_log.warning("Telegram proxy failed for file download, retrying direct: %s", telegram_error_summary(exc))
            self._mark_proxy_transport_failure()
            return await self._call_with_rate_limit_retry(
                lambda: self._download_file_once(file_path, use_proxy=False)
            )

    async def _download_file_once(self, file_path: str, *, use_proxy: bool) -> bytes:
        session = await self._get_session(use_proxy, timeout=120)
        url = f"https://api.telegram.org/file/bot{config.TELEGRAM_CONTROL_BOT_TOKEN}/{file_path}"
        async with session.get(url) as response:
            if response.status != 200:
                raise TelegramAPIError("downloadFile", response.status)
            return await response.read()

    async def _send_document(
        self,
        chat_id: int,
        *,
        filename: str,
        content: bytes,
        caption: str = "",
        reply_markup: dict | None = None,
    ) -> dict:
        use_proxy = self._should_use_proxy()
        try:
            return await self._call_with_rate_limit_retry(
                lambda: self._send_document_once(
                    chat_id,
                    filename=filename,
                    content=content,
                    caption=caption,
                    reply_markup=reply_markup,
                    use_proxy=use_proxy,
                )
            )
        except _TRANSPORT_ERRORS as exc:
            if not use_proxy:
                raise
            self._api_log.warning(
                "Telegram proxy failed for bot document upload, retrying direct: %s",
                telegram_error_summary(exc),
            )
            self._mark_proxy_transport_failure()
            return await self._call_with_rate_limit_retry(
                lambda: self._send_document_once(
                    chat_id,
                    filename=filename,
                    content=content,
                    caption=caption,
                    reply_markup=reply_markup,
                    use_proxy=False,
                )
            )

    async def _send_document_once(
        self,
        chat_id: int,
        *,
        filename: str,
        content: bytes,
        caption: str,
        reply_markup: dict | None,
        use_proxy: bool,
    ) -> dict:
        session = await self._get_session(use_proxy, timeout=120)
        url = (
            f"https://api.telegram.org/"
            f"bot{config.TELEGRAM_CONTROL_BOT_TOKEN}/sendDocument"
        )
        form = aiohttp.FormData()
        form.add_field("chat_id", str(chat_id))
        if caption:
            form.add_field("caption", limit_telegram_text(caption, 1024))
        if reply_markup:
            form.add_field(
                "reply_markup",
                json.dumps(reply_markup, ensure_ascii=False),
            )
        form.add_field(
            "document",
            content,
            filename=filename,
            content_type="text/markdown; charset=utf-8",
        )
        async with session.post(url, data=form) as response:
            data = await response.json(content_type=None)
            if response.status != 200 or not data.get("ok", False):
                raise TelegramAPIError("sendDocument", response.status, data)
            return data.get("result", {})

    async def _close_sessions(self) -> None:
        for session in list(self._sessions.values()):
            if session and not session.closed:
                await session.close()
        self._sessions.clear()
