import asyncio
from types import SimpleNamespace

import pytest
import httpx
from openai import AsyncOpenAI, APIStatusError

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


@pytest.mark.parametrize("status,code,expected", [
    (429, "rate_limit_exceeded", ["groq", "groq2"]),
    (413, "rate_limit_exceeded", ["groq", "groq2", "ollama"]),
    (413, "payload_too_large", ["groq"]),
])
def test_sdk_rate_limit_code_falls_through_without_retrying(monkeypatch, status, code, expected):
    calls = []
    monkeypatch.setattr(llm_client, "_record_usage", lambda *args, **kwargs: None)
    monkeypatch.setattr(llm_client, "_observe_provider_failure", lambda *args, **kwargs: None)
    providers = [
        llm_client.ProviderSpec("groq", "https://groq.test/v1", "synthetic-one"),
        llm_client.ProviderSpec("groq2", "https://groq.test/v1", "synthetic-two"),
        llm_client.ProviderSpec("ollama", "http://lan.test/v1", "synthetic-lan",
                                text_fallback_model="local-model", timeout_seconds=1),
    ]
    client = llm_client.FallbackLLMClient(providers)

    def transport(provider):
        def handle(request):
            calls.append(provider.name)
            if provider.name == "groq" or (status == 413 and provider.name == "groq2"):
                return httpx.Response(status, json={"error": {
                    "message": "Request too large", "type": "tokens", "code": code}})
            return httpx.Response(200, json={
                "id": "synthetic", "object": "chat.completion", "created": 0,
                "model": "synthetic-model", "choices": [{"index": 0,
                    "finish_reason": "stop", "message": {"role": "assistant", "content": "OK"}}]})
        return httpx.MockTransport(handle)

    for index, provider in enumerate(providers):
        client._clients[index] = AsyncOpenAI(
            api_key=provider.api_key, base_url=provider.base_url, max_retries=0,
            http_client=httpx.AsyncClient(transport=transport(provider)))

    async def run():
        try:
            if code == "payload_too_large":
                with pytest.raises(APIStatusError) as error:
                    await client.chat.completions.create(model="test-model", messages=[])
                assert error.value.status_code == 413
            else:
                response = await client.chat.completions.create(model="test-model", messages=[])
                assert response.choices[0].message.content == "OK"
        finally:
            await client.aclose()

    asyncio.run(run())
    assert calls == expected
