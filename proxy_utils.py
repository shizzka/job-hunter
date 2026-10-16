"""Helpers for proxy-aware browser and HTTP client behavior."""
from __future__ import annotations

import os

import aiohttp
import httpx
import config

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


LONG_LLM_ROLES = frozenset({
    "cover_letter", "cover_grounding", "cover_letter_rewrite", "answer_grounding",
})


def llm_http_timeout(llm_role: str | None = None) -> httpx.Timeout:
    """Allow long cloud reads within the role deadline; keep connects short."""
    read = (max(1, int(getattr(config, "LLM_GROUNDING_TIMEOUT_SECONDS", 40)))
            if llm_role in LONG_LLM_ROLES else 60.0)
    return httpx.Timeout(60.0, connect=10.0, read=read)


def llm_http_client() -> httpx.AsyncClient:
    """Use only the explicit LLM proxy, never inherited system proxies."""
    return httpx.AsyncClient(
        timeout=llm_http_timeout(),
        trust_env=False,
        proxy=config.LLM_PROXY or None,
    )
