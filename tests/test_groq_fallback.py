import asyncio
from types import SimpleNamespace

import pytest

import llm_client


@pytest.mark.parametrize(
    "second_key,second_base,expected_names",
    [
        ("", "", ["groq"]),
        ("second-test-key", "", ["groq", "groq2"]),
        ("first-test-key", "", ["groq"]),
        ("second-test-key", "https://second.test/v1", ["groq", "groq2"]),
    ],
)
def test_second_groq_account_configuration(monkeypatch, second_key, second_base, expected_names):
    monkeypatch.setattr(llm_client.config, "LLM_API_KEY", "")
    monkeypatch.setattr(llm_client, "_load_provider_env", lambda: {
        "GROQ_BASE_URL": "https://groq.test/v1",
        "GROQ_API_KEY": "first-test-key",
        "GROQ2_API_KEY": second_key,
        "GROQ2_BASE_URL": second_base,
        "GROQ_FAST_MODEL": "test-fast",
        "GROQ_STRONG_MODEL": "test-strong",
    })

    providers = llm_client._build_provider_specs()

    assert [p.name for p in providers] == expected_names
    for provider in providers:
        assert provider.model_for("gpt-oss:20b") == "test-fast"
        assert provider.model_for("gpt-oss:120b") == "test-strong"
    if len(providers) == 2:
        assert providers[1].base_url == (second_base or "https://groq.test/v1")
        assert providers[1].api_key == second_key


def test_groq_rate_limit_switches_account_on_same_endpoint(monkeypatch):
    calls = []
    usage = []

    class RateLimitError(Exception):
        status_code = 429

    class FakeAsyncOpenAI:
        def __init__(self, *, api_key, **kwargs):
            self.api_key = api_key
            self.chat = SimpleNamespace(completions=self)

        async def create(self, **kwargs):
            calls.append((self.api_key, kwargs["model"]))
            if self.api_key == "first-test-key":
                raise RateLimitError("Too Many Requests")
            return SimpleNamespace(choices=[])

    monkeypatch.setattr(llm_client, "AsyncOpenAI", FakeAsyncOpenAI)
    monkeypatch.setattr(llm_client.proxy_utils, "llm_http_client", lambda: None)
    monkeypatch.setattr(llm_client, "_record_usage", lambda provider, model, **kwargs: usage.append(provider))
    aliases = {"gpt-oss:120b": "test-strong"}
    client = llm_client.FallbackLLMClient([
        llm_client.ProviderSpec("groq", "https://groq.test/v1", "first-test-key", aliases),
        llm_client.ProviderSpec("groq2", "https://groq.test/v1", "second-test-key", aliases),
    ])

    asyncio.run(client.chat.completions.create(model="gpt-oss:120b", messages=[]))

    assert calls == [("first-test-key", "test-strong"), ("second-test-key", "test-strong")]
    assert usage == ["groq", "groq2"]
    assert client._active_index_by_model["gpt-oss:120b"] == 1
