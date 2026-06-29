"""OpenAI-compatible LLM client with provider/account fallback."""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass, field

from openai import AsyncOpenAI

import config
import proxy_utils

log = logging.getLogger("llm_client")


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    base_url: str
    api_key: str
    model_aliases: dict[str, str] = field(default_factory=dict)
    default_headers: dict[str, str] = field(default_factory=dict)

    def model_for(self, requested_model: str) -> str:
        model = (requested_model or "").strip()
        if not model:
            return requested_model
        return self.model_aliases.get(model, model)


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


def _openrouter_model_aliases(provider_env: dict[str, str]) -> dict[str, str]:
    fast = provider_env.get("OPENROUTER_FAST_MODEL", "openai/gpt-oss-20b:free")
    strong = provider_env.get("OPENROUTER_STRONG_MODEL", "openai/gpt-oss-120b:free")
    coder = provider_env.get("OPENROUTER_CODER_MODEL", strong)
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
    }


def _groq_model_aliases(provider_env: dict[str, str]) -> dict[str, str]:
    fast = provider_env.get("GROQ_FAST_MODEL", "llama-3.1-8b-instant")
    strong = provider_env.get("GROQ_STRONG_MODEL", "llama-3.3-70b-versatile")
    return {
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
    strong = provider_env.get("GEMINI_STRONG_MODEL", "gemini-3.5-flash")
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
    ) -> None:
        base = (base_url or "").strip().rstrip("/")
        key = (api_key or "").strip()
        if not base or not key:
            return
        if any(item.base_url == base and item.api_key == key for item in specs):
            return
        specs.append(
            ProviderSpec(
                name=name,
                base_url=base,
                api_key=key,
                model_aliases=model_aliases or {},
                default_headers=default_headers or {},
            )
        )

    add("primary", config.LLM_BASE_URL, config.LLM_API_KEY)

    provider_env = _load_provider_env()
    for name in ("OLLAMA", "OLLAMA2", "OLLAMA3"):
        add(
            name.lower(),
            provider_env.get(f"{name}_BASE_URL") or provider_env.get("OLLAMA_BASE_URL", ""),
            provider_env.get(f"{name}_API_KEY", ""),
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

    if not specs:
        add("primary", config.LLM_BASE_URL, "no-key")
    return specs


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
        )
    )


def _is_retryable_provider_error(exc: Exception) -> bool:
    return _is_quota_or_rate_limit(exc) or _is_model_unavailable(exc)


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

    def __init__(self, providers: list[ProviderSpec] | None = None):
        self.providers = providers or _build_provider_specs()
        self.chat = _FallbackChat(self)
        self._clients: dict[int, AsyncOpenAI] = {}
        self._active_index = 0
        self._active_index_by_model: dict[str, int] = {}

    def _client_for(self, index: int) -> AsyncOpenAI:
        client = self._clients.get(index)
        if client is None:
            provider = self.providers[index]
            kwargs = {
                "base_url": provider.base_url,
                "api_key": provider.api_key or "no-key",
                "http_client": proxy_utils.llm_http_client(),
            }
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
        return provider_kwargs

    async def create_chat_completion(self, **kwargs):
        if not self.providers:
            raise RuntimeError("No LLM providers configured")

        last_exc: Exception | None = None
        requested_model = str(kwargs.get("model") or "")
        attempted: list[str] = []
        total = len(self.providers)
        default_start = self._active_index_by_model.get(requested_model, 0)
        start = min(max(default_start, 0), total - 1)

        for offset in range(total):
            index = (start + offset) % total
            provider = self.providers[index]
            provider_kwargs = self._kwargs_for_provider(provider, kwargs)
            attempted.append(provider.name)
            try:
                response = await self._client_for(index).chat.completions.create(**provider_kwargs)
                self._active_index = index
                if requested_model:
                    self._active_index_by_model[requested_model] = index
                mapped_model = str(provider_kwargs.get("model") or "")
                if mapped_model != requested_model:
                    log.info(
                        "LLM provider %s served mapped model %s -> %s",
                        provider.name,
                        requested_model,
                        mapped_model,
                    )
                return response
            except Exception as exc:
                last_exc = exc
                if not _is_retryable_provider_error(exc):
                    raise
                if offset == total - 1:
                    raise LLMProvidersExhaustedError(
                        attempted,
                        requested_model,
                        exc,
                    ) from exc
                next_provider = self.providers[(index + 1) % total]
                log.warning(
                    "LLM provider %s rejected model %s (%s), trying %s",
                    provider.name,
                    str(provider_kwargs.get("model") or requested_model),
                    type(exc).__name__,
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
