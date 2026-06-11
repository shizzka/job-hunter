"""OpenAI-compatible LLM client with account fallback for quota/rate limits."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass

from openai import AsyncOpenAI

import config
import proxy_utils

log = logging.getLogger("llm_client")


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    base_url: str
    api_key: str


class LLMProvidersExhaustedError(RuntimeError):
    """All configured LLM providers rejected the request due to quota/rate limits."""

    def __init__(self, provider_names: list[str], model: str, last_error: Exception):
        self.provider_names = tuple(provider_names)
        self.model = model
        self.last_error = str(last_error)
        providers = ", ".join(provider_names) if provider_names else "none"
        super().__init__(
            f"All LLM providers exhausted for model {model or '?'}: {providers}"
        )


def _read_env_file(path: str) -> dict[str, str]:
    if not path or not os.path.exists(path):
        return {}

    values: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip().strip('"').strip("'")
    except Exception as exc:
        log.warning("Failed to read LLM providers file %s: %s", path, exc)
    return values


def _providers_file_candidates() -> list[str]:
    candidates = []
    explicit = os.getenv("JOB_HUNTER_LLM_PROVIDERS_FILE", "").strip()
    if explicit:
        candidates.append(os.path.expanduser(explicit))

    candidates.append(os.path.expanduser("~/.job-hunter/llm-providers.env"))

    home = os.path.abspath(config.JOB_HUNTER_HOME)
    parent = os.path.dirname(home)
    if os.path.basename(parent) == "profiles":
        candidates.append(os.path.join(os.path.dirname(parent), "llm-providers.env"))

    out = []
    for path in candidates:
        if path and path not in out:
            out.append(path)
    return out


def _load_provider_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for path in _providers_file_candidates():
        values.update(_read_env_file(path))
    return values


def _build_provider_specs() -> list[ProviderSpec]:
    specs: list[ProviderSpec] = []

    def add(name: str, base_url: str, api_key: str) -> None:
        base = (base_url or "").strip().rstrip("/")
        key = (api_key or "").strip()
        if not base or not key:
            return
        if any(item.base_url == base and item.api_key == key for item in specs):
            return
        specs.append(ProviderSpec(name=name, base_url=base, api_key=key))

    add("primary", config.LLM_BASE_URL, config.LLM_API_KEY)

    provider_env = _load_provider_env()
    for name in ("OLLAMA", "OLLAMA2", "OLLAMA3"):
        add(
            name.lower(),
            provider_env.get(f"{name}_BASE_URL") or provider_env.get("OLLAMA_BASE_URL", ""),
            provider_env.get(f"{name}_API_KEY", ""),
        )

    if not specs:
        add("primary", config.LLM_BASE_URL, "no-key")
    return specs


def _is_quota_or_rate_limit(exc: Exception) -> bool:
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    if status == 429:
        return True

    text = str(exc).casefold()
    return any(
        marker in text
        for marker in (
            "429",
            "too many requests",
            "weekly usage limit",
            "rate limit",
            "quota",
            "insufficient_quota",
        )
    )


class _FallbackCompletions:
    def __init__(self, owner: "FallbackLLMClient"):
        self._owner = owner

    async def create(self, **kwargs):
        return await self._owner.create_chat_completion(**kwargs)


class _FallbackChat:
    def __init__(self, owner: "FallbackLLMClient"):
        self.completions = _FallbackCompletions(owner)


class FallbackLLMClient:
    """Small adapter exposing ``client.chat.completions.create``.

    It keeps using the last successful provider first and only rotates to the
    next Ollama account for quota/rate-limit failures.
    """

    def __init__(self, providers: list[ProviderSpec] | None = None):
        self.providers = providers or _build_provider_specs()
        self.chat = _FallbackChat(self)
        self._clients: dict[int, AsyncOpenAI] = {}
        self._active_index = 0

    def _client_for(self, index: int) -> AsyncOpenAI:
        client = self._clients.get(index)
        if client is None:
            provider = self.providers[index]
            client = AsyncOpenAI(
                base_url=provider.base_url,
                api_key=provider.api_key or "no-key",
                http_client=proxy_utils.llm_http_client(),
            )
            self._clients[index] = client
        return client

    async def create_chat_completion(self, **kwargs):
        if not self.providers:
            raise RuntimeError("No LLM providers configured")

        last_exc: Exception | None = None
        total = len(self.providers)
        start = min(max(self._active_index, 0), total - 1)

        for offset in range(total):
            index = (start + offset) % total
            provider = self.providers[index]
            try:
                response = await self._client_for(index).chat.completions.create(**kwargs)
                self._active_index = index
                return response
            except Exception as exc:
                last_exc = exc
                if not _is_quota_or_rate_limit(exc):
                    raise
                if offset == total - 1:
                    raise LLMProvidersExhaustedError(
                        [item.name for item in self.providers],
                        str(kwargs.get("model") or ""),
                        exc,
                    ) from exc
                next_provider = self.providers[(index + 1) % total]
                log.warning(
                    "LLM provider %s hit quota/rate limit, trying %s",
                    provider.name,
                    next_provider.name,
                )

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("No LLM providers configured")


_client_singleton: FallbackLLMClient | None = None


def get_llm_client() -> FallbackLLMClient:
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = FallbackLLMClient()
    return _client_singleton


def reset_llm_client() -> None:
    global _client_singleton
    _client_singleton = None
