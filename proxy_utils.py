"""Helpers for proxy-aware browser and HTTP client behavior."""
from __future__ import annotations

import os

import aiohttp
import httpx

_PROXY_ENV_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)


def browser_launch_env(explicit_proxy_url: str = "") -> dict[str, str]:
    """Return browser env without inherited system proxy when no browser proxy is set."""
    env = os.environ.copy()
    if explicit_proxy_url:
        return env
    for key in _PROXY_ENV_VARS:
        env.pop(key, None)
    return env


def is_proxy_error(exc: BaseException) -> bool:
    if isinstance(exc, (aiohttp.ClientProxyConnectionError, aiohttp.ClientHttpProxyError)):
        return True

    text = str(exc).casefold()
    return any(
        marker in text
        for marker in (
            "err_proxy_connection_failed",
            "proxy connection failed",
            "cannot connect to host 127.0.0.1",
            "cannot connect to host localhost",
        )
    )


def llm_http_client() -> httpx.AsyncClient:
    """Return an HTTP client for LLM calls that ignores inherited env proxies."""
    return httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, connect=10.0),
        trust_env=False,
    )
