import asyncio
from types import SimpleNamespace

import pytest

import config
import llm_client
import proxy_utils


@pytest.mark.parametrize("order", ["groq,groq2", "groq2,groq", "groq,groq,groq2"])
def test_explicit_provider_order_never_adds_ollama(monkeypatch, order):
    monkeypatch.delenv("LLM_PROVIDER_ORDER", raising=False)
    monkeypatch.setattr(config, "LLM_BASE_URL", "https://groq.test/v1")
    monkeypatch.setattr(config, "LLM_API_KEY", "groq-key")
    monkeypatch.setattr(llm_client, "_load_provider_env", lambda: {
        "LLM_PROVIDER_ORDER": order,
        "OLLAMA_BASE_URL": "https://ollama.test/v1", "OLLAMA_API_KEY": "ollama-key",
        "GROQ_BASE_URL": "https://groq.test/v1", "GROQ_API_KEY": "groq-key",
        "GROQ2_API_KEY": "groq2-key",
    })

    providers = llm_client._build_provider_specs()

    assert [p.name for p in providers] == list(dict.fromkeys(order.split(",")))
    assert all("groq" in p.base_url for p in providers)


@pytest.mark.parametrize("order,error", [("groq_typo", ValueError), ("groq", RuntimeError), (",", ValueError)])
def test_explicit_provider_order_fails_closed(monkeypatch, order, error):
    monkeypatch.setenv("LLM_PROVIDER_ORDER", order)
    monkeypatch.setattr(llm_client, "_load_provider_env", lambda: {})
    with pytest.raises(error):
        llm_client._build_provider_specs()


def test_env_provider_order_overrides_file(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER_ORDER", "groq2")
    monkeypatch.setattr(llm_client, "_load_provider_env", lambda: {
        "LLM_PROVIDER_ORDER": "groq", "GROQ_BASE_URL": "https://groq.test/v1",
        "GROQ_API_KEY": "one", "GROQ2_API_KEY": "two",
    })
    assert [p.name for p in llm_client._build_provider_specs()] == ["groq2"]


def test_groq_alias_defaults_use_available_text_and_vision_models():
    aliases = llm_client._groq_model_aliases({})
    assert aliases["gpt-oss:20b"] == "openai/gpt-oss-20b"
    assert aliases["gpt-oss:120b"] == "openai/gpt-oss-120b"
    assert aliases["qwen3-vl:235b-instruct"] == "qwen/qwen3.8-27b"


@pytest.mark.parametrize("proxy", ["", "http://127.0.0.1:8080", "socks5://127.0.0.1:1080"])
def test_llm_proxy_is_explicit_and_does_not_inherit_env(monkeypatch, proxy):
    captured = {}
    monkeypatch.setenv("HTTPS_PROXY", "http://unrelated.test:8080")
    monkeypatch.setattr(config, "LLM_PROXY", proxy)
    monkeypatch.setattr(proxy_utils.httpx, "AsyncClient", lambda **kwargs: captured.update(kwargs))
    proxy_utils.llm_http_client()
    assert captured["trust_env"] is False
    assert captured["proxy"] == (proxy or None)


def test_client_lifecycle_closes_all_providers_and_disables_hidden_retries(monkeypatch):
    created = []

    class Client:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            self.close_calls = 0
            created.append(self)

        async def close(self):
            self.close_calls += 1
            if self.kwargs["api_key"] == "one":
                raise RuntimeError("close failure")

    monkeypatch.setattr(llm_client, "AsyncOpenAI", Client)
    monkeypatch.setattr(proxy_utils, "llm_http_client", lambda: None)
    client = llm_client.FallbackLLMClient([
        llm_client.ProviderSpec("groq", "https://groq.test/v1", "one"),
        llm_client.ProviderSpec("groq2", "https://groq.test/v1", "two"),
    ])
    client._client_for(0)
    client._client_for(1)

    asyncio.run(client.aclose())
    asyncio.run(client.aclose())

    assert [c.close_calls for c in created] == [1, 1]
    assert all(c.kwargs["max_retries"] == 0 for c in created)
    assert client._clients == {}


def test_shared_client_shutdown_does_not_create_a_new_client(monkeypatch):
    calls = []

    async def close():
        calls.append("closed")

    monkeypatch.setattr(llm_client, "_client_singleton", SimpleNamespace(aclose=close))
    asyncio.run(llm_client.close_llm_client())
    asyncio.run(llm_client.close_llm_client())
    assert llm_client._client_singleton is None
    assert calls == ["closed"]
