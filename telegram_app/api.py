"""Low-level asynchronous client for the Telegram Bot API."""

from __future__ import annotations

import json
import logging

import aiohttp

import config


class TelegramAPIClient:
    """Own Telegram HTTP sessions, proxy fallback, and API serialization."""

    def __init__(self, *, logger: logging.Logger | None = None) -> None:
        self._sessions: dict[bool, aiohttp.ClientSession] = {}
        self._force_direct = False
        self._api_log = logger or logging.getLogger(__name__)

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
        use_proxy = bool(config.TELEGRAM_PROXY) and not self._force_direct
        try:
            return await self._api_request_once(
                method,
                payload,
                use_proxy=use_proxy,
                timeout=timeout,
            )
        except Exception as exc:
            if not use_proxy:
                raise
            self._api_log.warning(
                "Telegram proxy failed for bot, retrying direct: %s",
                exc,
            )
            self._force_direct = True
            return await self._api_request_once(
                method,
                payload,
                use_proxy=False,
                timeout=timeout,
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
                raise RuntimeError(f"{method} failed: {response.status} {data}")
            return data.get("result", {})

    async def _send_document(
        self,
        chat_id: int,
        *,
        filename: str,
        content: bytes,
        caption: str = "",
        reply_markup: dict | None = None,
    ) -> dict:
        use_proxy = bool(config.TELEGRAM_PROXY) and not self._force_direct
        try:
            return await self._send_document_once(
                chat_id,
                filename=filename,
                content=content,
                caption=caption,
                reply_markup=reply_markup,
                use_proxy=use_proxy,
            )
        except Exception as exc:
            if not use_proxy:
                raise
            self._api_log.warning(
                "Telegram proxy failed for bot document upload, retrying direct: %s",
                exc,
            )
            self._force_direct = True
            return await self._send_document_once(
                chat_id,
                filename=filename,
                content=content,
                caption=caption,
                reply_markup=reply_markup,
                use_proxy=False,
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
            form.add_field("caption", caption[:1024])
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
                raise RuntimeError(
                    f"sendDocument failed: {response.status} {data}"
                )
            return data.get("result", {})

    async def _close_sessions(self) -> None:
        for session in list(self._sessions.values()):
            if session and not session.closed:
                await session.close()
        self._sessions.clear()
