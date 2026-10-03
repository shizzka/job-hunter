"""OpenAI-compatible LLM client with provider/account fallback."""
from __future__ import annotations

import asyncio
import logging
import math
import os
import re
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field

from openai import AsyncOpenAI, APIConnectionError
import httpx
from httpx import TransportError

import config
import analytics
import proxy_utils
import ollama_quality_log

log = logging.getLogger("llm_client")


def _record_usage(provider, model, **kwargs):
    try:
        analytics.record_llm_call(provider, model, **kwargs)
    except Exception:
        log.warning("Could not record LLM usage metadata")


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    base_url: str
    api_key: str
    model_aliases: dict[str, str] = field(default_factory=dict)
    default_headers: dict[str, str] = field(default_factory=dict)
    # Emergency temporary compatibility path; remove after AI Gateway migration.
    text_fallback_model: str = ""
    timeout_seconds: float | None = None
    quality_log_file: str = ""

    def model_for(self, requested_model: str) -> str:
        model = (requested_model or "").strip()
        if not model:
            return requested_model
        if self.text_fallback_model and not _is_vision_model(model):
            return self.text_fallback_model
        return self.model_aliases.get(model, model)


def _is_vision_model(model: str) -> bool:
    normalized = model.strip().casefold()
    configured = str(getattr(config, "HH_CAPTCHA_VISION_MODEL", "") or "").strip().casefold()
    return bool(normalized and (normalized == configured or re.search(
        r"vision|llava|omni|(?:^|[-_/])vl(?:$|[:\-_/])", normalized)))


def _has_multimodal_input(kwargs: dict) -> bool:
    for message in kwargs.get("messages") or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, list) and any(isinstance(part, dict) and part.get("type") in {
                "image_url", "input_image", "image", "input_audio", "video_url", "file", "input_file"}
                for part in content):
            return True
    return False


class LLMProvidersExhaustedError(RuntimeError):
    """All configured LLM providers rejected the request."""

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


def _openrouter_headers(provider_env: dict[str, str]) -> dict[str, str]:
    return {
        "HTTP-Referer": provider_env.get("OPENROUTER_HTTP_REFERER", "https://localhost/job-hunter"),
        "X-Title": provider_env.get("OPENROUTER_APP_TITLE", "job-hunter"),
    }


def _ollama_model_aliases(provider_env: dict[str, str], prefix: str = "OLLAMA") -> dict[str, str]:
    """Map aliases unavailable in Ollama Cloud to the supported free model."""
    fallback = (provider_env.get(f"{prefix}_FALLBACK_MODEL")
                or provider_env.get("OLLAMA_FALLBACK_MODEL") or "gpt-oss:120b").strip()
    return {
        "qwen3-coder:480b": fallback,
        "qwen3-coder-next": fallback,
    }


def _ollama_timeout(provider_env: dict[str, str], prefix: str) -> float:
    try:
        value = float(provider_env.get(f"{prefix}_TIMEOUT_SECONDS", "60"))
        if not math.isfinite(value) or value <= 0:
            raise ValueError
        return value
    except (TypeError, ValueError, OverflowError):
        log.warning("Invalid %s_TIMEOUT_SECONDS; using 60 seconds", prefix)
        return 60.0


def _openrouter_model_aliases(provider_env: dict[str, str]) -> dict[str, str]:
    # Free variants are retired independently of their paid counterparts.
    # The free router selects an available zero-cost model by request capability.
    fast = provider_env.get("OPENROUTER_FAST_MODEL", "openrouter/free")
    strong = provider_env.get("OPENROUTER_STRONG_MODEL", "openrouter/free")
    coder = provider_env.get("OPENROUTER_CODER_MODEL", strong)
    # The general free router can also select a moderation model for images.
    vision = provider_env.get(
        "OPENROUTER_VISION_MODEL",
        "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",
    )
    return {
        "qwen3-vl:235b-instruct": vision,
        "gpt-oss:20b": fast,
        "gpt-oss:120b": strong,
        "qwen3-coder:480b": coder,
        "qwen3-coder-next": coder,
        "cogito-2.1:671b": strong,
        "deepseek-v3.1:671b": strong,
        "deepseek-v3.2": strong,
        "deepseek-v4-flash": strong,
        "deepseek-v4-pro": strong,
        "gemini-3-flash-preview": strong,
    }


def _groq_model_aliases(provider_env: dict[str, str]) -> dict[str, str]:
    fast = provider_env.get("GROQ_FAST_MODEL", "openai/gpt-oss-20b")
    strong = provider_env.get("GROQ_STRONG_MODEL", "openai/gpt-oss-120b")
    vision = provider_env.get("GROQ_VISION_MODEL", "qwen/qwen3.8-27b")
    return {
        "qwen3-vl:235b-instruct": vision,
        "gpt-oss:20b": fast,
        "gpt-oss:120b": strong,
        "qwen3-coder:480b": strong,
        "qwen3-coder-next": strong,
        "cogito-2.1:671b": strong,
        "deepseek-v3.1:671b": strong,
        "deepseek-v3.2": strong,
        "deepseek-v4-flash": strong,
        "deepseek-v4-pro": strong,
        "gemini-3-flash-preview": strong,
    }


def _deepseek_model_aliases(provider_env: dict[str, str]) -> dict[str, str]:
    default = provider_env.get("DEEPSEEK_FALLBACK_MODEL", "deepseek-v4-flash")
    return {
        "gpt-oss:20b": default,
        "gpt-oss:120b": default,
        "qwen3-coder:480b": default,
        "qwen3-coder-next": default,
        "cogito-2.1:671b": default,
        "deepseek-v3.1:671b": default,
        "deepseek-v3.2": default,
        "gemini-3-flash-preview": default,
    }


def _cerebras_model_aliases(provider_env: dict[str, str]) -> dict[str, str]:
    fast = provider_env.get("CEREBRAS_FAST_MODEL", "gpt-oss-120b")
    strong = provider_env.get("CEREBRAS_STRONG_MODEL", "zai-glm-4.7")
    coder = provider_env.get("CEREBRAS_CODER_MODEL", strong)
    return {
        "gpt-oss:20b": fast,
        "gpt-oss:120b": fast,
        "qwen3-coder:480b": coder,
        "qwen3-coder-next": coder,
        "cogito-2.1:671b": strong,
        "deepseek-v3.1:671b": strong,
        "deepseek-v3.2": strong,
        "deepseek-v4-flash": strong,
        "deepseek-v4-pro": strong,
        "gemini-3-flash-preview": strong,
        "gemini-3.5-flash": strong,
        "gemini-3.1-flash-lite": fast,
    }


def _sambanova_model_aliases(provider_env: dict[str, str]) -> dict[str, str]:
    fast = provider_env.get("SAMBANOVA_FAST_MODEL", "Meta-Llama-3.3-70B-Instruct")
    strong = provider_env.get("SAMBANOVA_STRONG_MODEL", "gpt-oss-120b")
    coder = provider_env.get("SAMBANOVA_CODER_MODEL", strong)
    deepseek = provider_env.get("SAMBANOVA_DEEPSEEK_MODEL", "DeepSeek-V3.1")
    return {
        "gpt-oss:20b": fast,
        "gpt-oss:120b": strong,
        "qwen3-coder:480b": coder,
        "qwen3-coder-next": coder,
        "cogito-2.1:671b": strong,
        "deepseek-v3.1:671b": deepseek,
        "deepseek-v3.2": provider_env.get("SAMBANOVA_DEEPSEEK_PREVIEW_MODEL", "DeepSeek-V3.2"),
        "deepseek-v4-flash": deepseek,
        "deepseek-v4-pro": deepseek,
        "gemini-3-flash-preview": provider_env.get("SAMBANOVA_GEMMA_MODEL", "gemma-4-31B-it"),
        "gemini-3.5-flash": provider_env.get("SAMBANOVA_GEMMA_MODEL", "gemma-4-31B-it"),
        "gemini-3.1-flash-lite": fast,
    }


def _gemini_model_aliases(provider_env: dict[str, str]) -> dict[str, str]:
    fast = provider_env.get("GEMINI_FAST_MODEL", "gemini-3.1-flash-lite")
    strong = provider_env.get("GEMINI_STRONG_MODEL", "gemini-3.1-flash-lite")
    coder = provider_env.get("GEMINI_CODER_MODEL", strong)
    return {
        "gpt-oss:20b": fast,
        "gpt-oss:120b": strong,
        "qwen3-coder:480b": coder,
        "qwen3-coder-next": coder,
        "cogito-2.1:671b": strong,
        "deepseek-v3.1:671b": strong,
        "deepseek-v3.2": strong,
        "deepseek-v4-flash": strong,
        "deepseek-v4-pro": strong,
        "gemini-3-flash-preview": strong,
        "gemini-3.5-flash": strong,
        "gemini-3.1-flash-lite": fast,
    }


def _cloudflare_base_url(provider_env: dict[str, str]) -> str:
    explicit = provider_env.get("CLOUDFLARE_BASE_URL", "").strip()
    if explicit:
        return explicit
    account_id = provider_env.get("CLOUDFLARE_ACCOUNT_ID", "").strip()
    if not account_id:
        return ""
    return f"https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1"


def _cloudflare_model_aliases(provider_env: dict[str, str]) -> dict[str, str]:
    fast = provider_env.get("CLOUDFLARE_FAST_MODEL", "@cf/meta/llama-3.1-8b-instruct")
    strong = provider_env.get("CLOUDFLARE_STRONG_MODEL", "@cf/openai/gpt-oss-120b")
    coder = provider_env.get("CLOUDFLARE_CODER_MODEL", strong)
    return {
        "gpt-oss:20b": fast,
        "gpt-oss:120b": strong,
        "qwen3-coder:480b": coder,
        "qwen3-coder-next": coder,
        "cogito-2.1:671b": strong,
        "deepseek-v3.1:671b": strong,
        "deepseek-v3.2": strong,
        "deepseek-v4-flash": strong,
        "deepseek-v4-pro": strong,
        "gemini-3-flash-preview": strong,
        "gemini-3.5-flash": strong,
        "gemini-3.1-flash-lite": fast,
    }


def _huggingface_model_aliases(provider_env: dict[str, str]) -> dict[str, str]:
    fast = provider_env.get("HF_FAST_MODEL", provider_env.get("HUGGINGFACE_FAST_MODEL", "openai/gpt-oss-20b:fastest"))
    strong = provider_env.get("HF_STRONG_MODEL", provider_env.get("HUGGINGFACE_STRONG_MODEL", "openai/gpt-oss-120b:fastest"))
    coder = provider_env.get("HF_CODER_MODEL", provider_env.get("HUGGINGFACE_CODER_MODEL", strong))
    deepseek = provider_env.get("HF_DEEPSEEK_MODEL", provider_env.get("HUGGINGFACE_DEEPSEEK_MODEL", "deepseek-ai/DeepSeek-V3.2:fastest"))
    return {
        "gpt-oss:20b": fast,
        "gpt-oss:120b": strong,
        "qwen3-coder:480b": coder,
        "qwen3-coder-next": coder,
        "cogito-2.1:671b": strong,
        "deepseek-v3.1:671b": deepseek,
        "deepseek-v3.2": deepseek,
        "deepseek-v4-flash": deepseek,
        "deepseek-v4-pro": deepseek,
        "gemini-3-flash-preview": strong,
        "gemini-3.5-flash": strong,
        "gemini-3.1-flash-lite": fast,
    }


def _siliconflow_model_aliases(provider_env: dict[str, str]) -> dict[str, str]:
    default = provider_env.get("SILICONFLOW_FREE_MODEL", "THUDM/GLM-Z1-9B-0414")
    strong = provider_env.get("SILICONFLOW_STRONG_MODEL", default)
    coder = provider_env.get("SILICONFLOW_CODER_MODEL", strong)
    return {
        "gpt-oss:20b": default,
        "gpt-oss:120b": strong,
        "qwen3-coder:480b": coder,
        "qwen3-coder-next": coder,
        "cogito-2.1:671b": strong,
        "deepseek-v3.1:671b": strong,
        "deepseek-v3.2": strong,
        "deepseek-v4-flash": strong,
        "deepseek-v4-pro": strong,
        "gemini-3-flash-preview": strong,
        "gemini-3.5-flash": strong,
        "gemini-3.1-flash-lite": default,
    }


def _build_provider_specs() -> list[ProviderSpec]:
    specs: list[ProviderSpec] = []

    def add(
        name: str,
        base_url: str,
        api_key: str,
        *,
        model_aliases: dict[str, str] | None = None,
        default_headers: dict[str, str] | None = None,
        text_fallback_model: str = "",
        timeout_seconds: float | None = None,
        quality_log_file: str = "",
    ) -> None:
        base = (base_url or "").strip().rstrip("/")
        key = (api_key or "").strip()
        if not base or not key:
            return
        specs.append(
            ProviderSpec(
                name=name,
                base_url=base,
                api_key=key,
                model_aliases=model_aliases or {},
                default_headers=default_headers or {},
                text_fallback_model=text_fallback_model,
                timeout_seconds=timeout_seconds,
                quality_log_file=os.path.abspath(os.path.expanduser(quality_log_file)) if quality_log_file else "",
            )
        )

    provider_env = _load_provider_env()
    primary_base = config.LLM_BASE_URL
    # A separately configured LAN slot must not rename an Ollama Cloud primary.
    primary_env = provider_env
    if (provider_env.get("OLLAMA_BASE_URL", primary_base).rstrip("/") != primary_base.rstrip("/")
            and provider_env.get("OLLAMA_FALLBACK_MODEL")):
        primary_env = {**provider_env, "OLLAMA_FALLBACK_MODEL": "gpt-oss:120b"}
    primary_aliases = _ollama_model_aliases(primary_env) if "ollama" in primary_base.lower() else {}
    add("primary", primary_base, config.LLM_API_KEY, model_aliases=primary_aliases)

    for name in ("OLLAMA", "OLLAMA2", "OLLAMA3"):
        local_model = provider_env.get(f"{name}_FALLBACK_MODEL", "").strip()
        add(
            name.lower(),
            provider_env.get(f"{name}_BASE_URL") or provider_env.get("OLLAMA_BASE_URL", ""),
            provider_env.get(f"{name}_API_KEY", ""),
            model_aliases=_ollama_model_aliases(provider_env, name),
            text_fallback_model=local_model,
            timeout_seconds=_ollama_timeout(provider_env, name) if local_model else None,
            quality_log_file=(provider_env.get(f"{name}_QUALITY_LOG_FILE")
                              or provider_env.get("OLLAMA_QUALITY_LOG_FILE", "")) if local_model else "",
        )

    add(
        "cerebras",
        provider_env.get("CEREBRAS_BASE_URL", "https://api.cerebras.ai/v1"),
        provider_env.get("CEREBRAS_API_KEY", ""),
        model_aliases=_cerebras_model_aliases(provider_env),
    )

    openrouter_aliases = _openrouter_model_aliases(provider_env)
    openrouter_headers = _openrouter_headers(provider_env)
    add(
        "openrouter",
        provider_env.get("OPENROUTER_BASE_URL", ""),
        provider_env.get("OPENROUTER_API_KEY", ""),
        model_aliases=openrouter_aliases,
        default_headers=openrouter_headers,
    )
    add(
        "openrouter2",
        provider_env.get("OPENROUTER2_BASE_URL") or provider_env.get("OPENROUTER_BASE_URL", ""),
        provider_env.get("OPENROUTER2_API_KEY") or provider_env.get("OPENROUTER_API_KEY_2", ""),
        model_aliases=openrouter_aliases,
        default_headers=openrouter_headers,
    )

    add(
        "groq",
        provider_env.get("GROQ_BASE_URL", ""),
        provider_env.get("GROQ_API_KEY", ""),
        model_aliases=_groq_model_aliases(provider_env),
    )
    add(
        "groq2",
        provider_env.get("GROQ2_BASE_URL") or provider_env.get("GROQ_BASE_URL", ""),
        provider_env.get("GROQ2_API_KEY", ""),
        model_aliases=_groq_model_aliases(provider_env),
    )
    add(
        "sambanova",
        provider_env.get("SAMBANOVA_BASE_URL", "https://api.sambanova.ai/v1"),
        provider_env.get("SAMBANOVA_API_KEY", ""),
        model_aliases=_sambanova_model_aliases(provider_env),
    )
    add(
        "gemini",
        provider_env.get("GEMINI_BASE_URL", "https://generativelanguage.googleapis.com/v1beta/openai"),
        provider_env.get("GEMINI_API_KEY") or provider_env.get("GOOGLE_API_KEY", ""),
        model_aliases=_gemini_model_aliases(provider_env),
    )
    add(
        "deepseek",
        provider_env.get("DEEPSEEK_BASE_URL", ""),
        provider_env.get("DEEPSEEK_API_KEY", ""),
        model_aliases=_deepseek_model_aliases(provider_env),
    )
    add(
        "cloudflare",
        _cloudflare_base_url(provider_env),
        provider_env.get("CLOUDFLARE_API_KEY") or provider_env.get("CLOUDFLARE_API_TOKEN", ""),
        model_aliases=_cloudflare_model_aliases(provider_env),
    )
    add(
        "huggingface",
        provider_env.get("HF_BASE_URL", provider_env.get("HUGGINGFACE_BASE_URL", "https://router.huggingface.co/v1")),
        provider_env.get("HF_TOKEN") or provider_env.get("HUGGINGFACE_API_KEY", ""),
        model_aliases=_huggingface_model_aliases(provider_env),
    )
    add(
        "siliconflow",
        provider_env.get("SILICONFLOW_BASE_URL", "https://api.siliconflow.cn/v1"),
        provider_env.get("SILICONFLOW_API_KEY", ""),
        model_aliases=_siliconflow_model_aliases(provider_env),
    )

    def unique(providers):
        result = []
        credentials = set()
        for provider in providers:
            identity = (provider.base_url, provider.api_key, provider.text_fallback_model)
            if identity not in credentials:
                credentials.add(identity)
                result.append(provider)
        return result

    order = os.getenv("LLM_PROVIDER_ORDER", provider_env.get("LLM_PROVIDER_ORDER", "")).strip()
    if order:
        requested = list(dict.fromkeys(name.strip().lower() for name in order.split(",") if name.strip()))
        known = {
            "primary", "ollama", "ollama2", "ollama3", "cerebras", "openrouter", "openrouter2",
            "groq", "groq2", "sambanova", "gemini", "deepseek", "cloudflare", "huggingface", "siliconflow",
        }
        if not requested or any(name not in known for name in requested):
            raise ValueError("Invalid LLM_PROVIDER_ORDER; use configured provider names separated by commas")
        available = {provider.name: provider for provider in specs}
        selected = [available[name] for name in requested if name in available]
        if not selected:
            raise RuntimeError("No configured LLM providers match LLM_PROVIDER_ORDER")
        return unique(selected)

    if not specs:
        add("primary", config.LLM_BASE_URL, "no-key")
    return unique(specs)


def _status_code(exc: Exception) -> int | None:
    status = getattr(exc, "status_code", None)
    response = getattr(exc, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    try:
        return int(status) if status is not None else None
    except (TypeError, ValueError):
        return None


def _is_quota_or_rate_limit(exc: Exception) -> bool:
    status = _status_code(exc)
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


def _is_model_unavailable(exc: Exception) -> bool:
    status = _status_code(exc)
    if status in (404, 410):
        return True

    text = str(exc).casefold()
    if status == 403 and any(marker in text for marker in ("model", "permission", "blocked")):
        return True
    return any(
        marker in text
        for marker in (
            "model_not_found",
            "model permission",
            "model_permission_blocked",
            "model is blocked",
            "blocked at the organization level",
            "does not exist",
            "no endpoints found",
            "not found",
            "retired",
            "gone",
            "unsupported model",
            "model unavailable",
            "model is unavailable",
        )
    )


def _is_retryable_provider_error(exc: Exception) -> bool:
    status = _status_code(exc)
    return (
        _is_quota_or_rate_limit(exc) or _is_model_unavailable(exc)
        or status == 408 or (status is not None and 500 <= status <= 599)
        or isinstance(exc, (APIConnectionError, TransportError, ConnectionError, TimeoutError))
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
    """Adapter exposing ``client.chat.completions.create`` with provider fallback."""

    def __init__(
        self,
        providers: list[ProviderSpec] | None = None,
        *,
        fallback_ttl_seconds: float | None = None,
        clock: Callable[[], float] | None = None,
    ):
        self.providers = providers or _build_provider_specs()
        self.chat = _FallbackChat(self)
        self._clients: dict[int, AsyncOpenAI] = {}
        self._active_index = 0
        self._active_index_by_model: dict[str, int] = {}
        if fallback_ttl_seconds is None:
            raw_ttl = os.getenv("LLM_PROVIDER_FALLBACK_TTL_SECONDS", "300")
            try:
                fallback_ttl_seconds = float(raw_ttl)
            except ValueError:
                log.warning(
                    "Invalid LLM_PROVIDER_FALLBACK_TTL_SECONDS=%r; using 300 seconds",
                    raw_ttl,
                )
                fallback_ttl_seconds = 300.0
        self._fallback_ttl_seconds = max(0.0, fallback_ttl_seconds)
        self._clock = clock or time.monotonic
        self._fallback_until_by_model: dict[str, float] = {}

    def _start_index_for_model(self, requested_model: str, total: int) -> int:
        index = self._active_index_by_model.get(requested_model, 0)
        if index <= 0:
            return 0
        deadline = self._fallback_until_by_model.get(requested_model)
        if deadline is None or self._clock() >= deadline:
            self._active_index_by_model.pop(requested_model, None)
            self._fallback_until_by_model.pop(requested_model, None)
            return 0
        return min(index, total - 1)

    def _client_for(self, index: int) -> AsyncOpenAI:
        client = self._clients.get(index)
        if client is None:
            provider = self.providers[index]
            kwargs = {
                "base_url": provider.base_url,
                "api_key": provider.api_key or "no-key",
                "http_client": (httpx.AsyncClient(
                    timeout=httpx.Timeout(provider.timeout_seconds or 60, connect=min(3, provider.timeout_seconds or 60)),
                    trust_env=False,
                ) if provider.text_fallback_model else proxy_utils.llm_http_client()),
                "max_retries": 0,
            }
            if provider.timeout_seconds is not None:
                kwargs["timeout"] = httpx.Timeout(provider.timeout_seconds, connect=min(3, provider.timeout_seconds))
            if provider.default_headers:
                kwargs["default_headers"] = dict(provider.default_headers)
            client = AsyncOpenAI(**kwargs)
            self._clients[index] = client
        return client

    def _kwargs_for_provider(self, provider: ProviderSpec, kwargs: dict) -> dict:
        provider_kwargs = dict(kwargs)
        requested_model = str(provider_kwargs.get("model") or "")
        mapped_model = provider.model_for(requested_model)
        if mapped_model:
            provider_kwargs["model"] = mapped_model
        if provider.text_fallback_model:
            # Qwen thinking can consume a small task's entire output budget.
            # Ollama /v1 uses reasoning_effort, not the native /api/chat think flag.
            provider_kwargs.setdefault("reasoning_effort", "none")
        return provider_kwargs

    async def create_chat_completion(self, **kwargs):
        if not self.providers:
            raise RuntimeError("No LLM providers configured")

        last_exc: Exception | None = None
        requested_model = str(kwargs.get("model") or "")
        vision = _is_vision_model(requested_model) or _has_multimodal_input(kwargs)
        cache_key = ("vision:" + requested_model) if vision and any(
            provider.text_fallback_model for provider in self.providers) else requested_model
        attempted: list[str] = []
        total = len(self.providers)
        start = self._start_index_for_model(cache_key, total)

        for offset in range(total):
            index = (start + offset) % total
            provider = self.providers[index]
            if provider.text_fallback_model and vision:
                continue  # Never send CAPTCHA/images to emergency text-only models.
            provider_kwargs = self._kwargs_for_provider(provider, kwargs)
            attempted.append(provider.name)
            call_id = uuid.uuid4().hex if provider.quality_log_file else ""
            attempt_started = time.monotonic()
            quality_meta = {"call_id": call_id, "requested_model": requested_model,
                            "actual_model": str(provider_kwargs.get("model") or "")}
            ollama_quality_log.record(provider, event="request", request=provider_kwargs, **quality_meta)
            try:
                call = self._client_for(index).chat.completions.create(**provider_kwargs)
                response = (await asyncio.wait_for(call, timeout=provider.timeout_seconds)
                            if provider.timeout_seconds is not None else await call)
                ollama_quality_log.record(provider, event="response", response=response,
                    latency_seconds=time.monotonic() - attempt_started, **quality_meta)
                if provider.text_fallback_model and not kwargs.get("stream"):
                    choices = getattr(response, "choices", None)
                    content = getattr(getattr(choices[0], "message", None), "content", None) if choices else None
                    if not isinstance(content, str) or not content.strip():
                        raise ValueError("Invalid emergency Ollama response envelope")
                self._active_index = index
                if requested_model:
                    if index == 0 or self._fallback_ttl_seconds == 0:
                        self._active_index_by_model.pop(cache_key, None)
                        self._fallback_until_by_model.pop(cache_key, None)
                    else:
                        self._active_index_by_model[cache_key] = index
                        if start == 0 or cache_key not in self._fallback_until_by_model:
                            self._fallback_until_by_model[cache_key] = (
                                self._clock() + self._fallback_ttl_seconds
                            )
                mapped_model = str(provider_kwargs.get("model") or "")
                if mapped_model != requested_model:
                    log.info(
                        "LLM provider %s served mapped model %s -> %s",
                        provider.name,
                        requested_model,
                        mapped_model,
                    )
                _record_usage(provider.name, str(provider_kwargs.get("model") or ""), response=response)
                return response
            except asyncio.CancelledError:
                ollama_quality_log.record(provider, event="cancelled",
                    latency_seconds=time.monotonic() - attempt_started, **quality_meta)
                raise
            except Exception as exc:
                ollama_quality_log.record(provider, event="error", error=exc,
                    latency_seconds=time.monotonic() - attempt_started, **quality_meta)
                _record_usage(provider.name, str(provider_kwargs.get("model") or ""), error_kind=type(exc).__name__)
                last_exc = exc
                if not _is_retryable_provider_error(exc):
                    raise
                log.warning(
                    "LLM provider %s rejected model %s (%s); continuing allowed fallback chain",
                    provider.name,
                    str(provider_kwargs.get("model") or requested_model),
                    type(exc).__name__,
                )

        if last_exc is not None:
            raise LLMProvidersExhaustedError(attempted, requested_model, last_exc) from last_exc
        raise LLMProvidersExhaustedError(attempted, requested_model,
                                       RuntimeError("No eligible providers for request capability"))

    async def aclose(self) -> None:
        """Release every lazily created SDK/HTTP client, even if one close fails."""
        clients, self._clients = self._clients, {}
        self._active_index_by_model.clear()
        self._fallback_until_by_model.clear()
        for client in clients.values():
            try:
                await client.close()
            except Exception as exc:
                log.warning("LLM client close failed: %s", type(exc).__name__)


_client_singleton: FallbackLLMClient | None = None


def get_llm_client() -> FallbackLLMClient:
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = FallbackLLMClient()
    return _client_singleton


def reset_llm_client() -> None:
    global _client_singleton
    _client_singleton = None


async def close_llm_client() -> None:
    """Close the shared client at process shutdown without creating a new one."""
    global _client_singleton
    client, _client_singleton = _client_singleton, None
    if client is not None:
        await client.aclose()
