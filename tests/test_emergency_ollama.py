"""Temporary LAN compatibility path: synthetic providers only, never real HTTP."""
import asyncio
import json
import logging
from types import SimpleNamespace

import httpx
import pytest

import llm_client as llm
import matcher


class StatusError(Exception):
    def __init__(self, status):
        self.status_code = status
        super().__init__("Synthetic provider failure")


def response(content="Synthetic OK"):
    return SimpleNamespace(choices=[SimpleNamespace(
        message=SimpleNamespace(content=content), finish_reason="stop")])


def local(name="ollama", model="qwen3-coder:30b", timeout=0.1):
    return llm.ProviderSpec(name, "http://ollama.test/v1", "synthetic",
                            text_fallback_model=model, timeout_seconds=timeout)


def cloud(name="groq"):
    return llm.ProviderSpec(name, "https://cloud.test/v1", "synthetic",
                            {"gpt-oss:120b": "cloud-own-model"})


@pytest.fixture(autouse=True)
def isolated(monkeypatch):
    monkeypatch.delenv("LLM_PROVIDER_ORDER", raising=False)
    monkeypatch.setattr(llm.config, "LLM_API_KEY", "")
    monkeypatch.setattr(llm, "_load_provider_env", lambda: {})
    monkeypatch.setattr(llm, "_record_usage", lambda *args, **kwargs: None)


def fake_chain(monkeypatch, providers, outcomes):
    calls = []
    client = llm.FallbackLLMClient(providers)

    def sdk(index):
        async def create(**kwargs):
            calls.append((providers[index].name, kwargs["model"]))
            outcome = outcomes[index]
            if isinstance(outcome, Exception):
                raise outcome
            if outcome == "hang":
                await asyncio.Event().wait()
            return outcome
        return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)))

    monkeypatch.setattr(client, "_client_for", sdk)
    return client, calls


@pytest.mark.parametrize("failure", [StatusError(429), RuntimeError("quota exhausted"),
                                      RuntimeError("rate limit"), RuntimeError("model unavailable"),
                                      StatusError(503)])
def test_cloud_failure_uses_local_primary(monkeypatch, failure):
    client, calls = fake_chain(monkeypatch, [cloud(), local()], [failure, response()])
    result = asyncio.run(client.chat.completions.create(model="gpt-oss:120b", messages=[]))
    assert result.choices[0].message.content == "Synthetic OK"
    assert calls == [("groq", "cloud-own-model"), ("ollama", "qwen3-coder:30b")]


@pytest.mark.parametrize("failure", [ConnectionRefusedError(), TimeoutError(), StatusError(404),
                                      StatusError(503), httpx.ConnectError("offline"),
                                      httpx.ReadTimeout("offline"), "hang"])
def test_local_failure_uses_second_model(monkeypatch, failure):
    client, calls = fake_chain(monkeypatch, [local(timeout=0.01), local("ollama2", "qwen3:8b")],
                               [failure, response()])
    asyncio.run(client.chat.completions.create(model="gpt-oss:120b", messages=[]))
    assert calls == [("ollama", "qwen3-coder:30b"), ("ollama2", "qwen3:8b")]


def test_unavailable_locals_restore_original_cloud_mapping(monkeypatch):
    client, calls = fake_chain(monkeypatch, [local(), local("ollama2", "qwen3:8b"), cloud()],
                               [ConnectionRefusedError(), StatusError(404), response()])
    request = {"model": "gpt-oss:120b", "messages": []}
    asyncio.run(client.chat.completions.create(**request))
    assert calls == [("ollama", "qwen3-coder:30b"), ("ollama2", "qwen3:8b"),
                     ("groq", "cloud-own-model")]
    assert request == {"model": "gpt-oss:120b", "messages": []}


@pytest.mark.parametrize("requested", ["gpt-oss:120b", "gpt-oss:20b", "qwen3-coder:480b",
                                        "qwen3-coder-next", "custom-task-override"])
@pytest.mark.parametrize("name,actual", [("ollama", "qwen3-coder:30b"), ("ollama2", "qwen3:8b")])
def test_provider_local_wildcard_mapping(requested, name, actual):
    assert local(name, actual).model_for(requested) == actual
    assert cloud().model_for(requested) == ("cloud-own-model" if requested == "gpt-oss:120b" else requested)


def test_config_same_credentials_different_models_and_prefixes(monkeypatch):
    monkeypatch.setattr(llm, "_load_provider_env", lambda: {
        "OLLAMA_BASE_URL": "http://ollama.test/v1", "OLLAMA_API_KEY": "synthetic",
        "OLLAMA2_API_KEY": "synthetic", "OLLAMA3_API_KEY": "third",
        "OLLAMA_FALLBACK_MODEL": "qwen3-coder:30b", "OLLAMA2_FALLBACK_MODEL": "qwen3:8b",
        "OLLAMA3_FALLBACK_MODEL": "third-text-model", "OLLAMA2_TIMEOUT_SECONDS": "7",
        "LLM_PROVIDER_ORDER": "ollama,ollama2,ollama3"})
    providers = llm._build_provider_specs()
    assert [p.name for p in providers] == ["ollama", "ollama2", "ollama3"]
    assert [p.model_for("gpt-oss:120b") for p in providers] == [
        "qwen3-coder:30b", "qwen3:8b", "third-text-model"]
    assert providers[1].timeout_seconds == 7


def test_legacy_without_ollama_unchanged(monkeypatch):
    monkeypatch.setattr(llm, "_load_provider_env", lambda: {
        "GROQ_BASE_URL": "https://cloud.test/v1", "GROQ_API_KEY": "synthetic"})
    providers = llm._build_provider_specs()
    assert [p.name for p in providers] == ["groq"]
    assert providers[0].model_for("gpt-oss:120b") == "openai/gpt-oss-120b"
    assert providers[0].text_fallback_model == ""
    assert providers[0].timeout_seconds is None


def test_distinct_cloud_primary_does_not_inherit_lan_model(monkeypatch):
    monkeypatch.setattr(llm.config, "LLM_BASE_URL", "https://ollama.test/v1")
    monkeypatch.setattr(llm.config, "LLM_API_KEY", "synthetic-cloud")
    monkeypatch.setattr(llm, "_load_provider_env", lambda: {
        "OLLAMA_BASE_URL": "http://local.test/v1", "OLLAMA_API_KEY": "synthetic",
        "OLLAMA_FALLBACK_MODEL": "qwen3-coder:30b"})
    primary, emergency = llm._build_provider_specs()
    assert primary.model_for("qwen3-coder:480b") == "gpt-oss:120b"
    assert emergency.model_for("qwen3-coder:480b") == "qwen3-coder:30b"


@pytest.mark.parametrize("model,messages", [
    ("qwen3-vl:235b-instruct", []), ("llava:7b", []), ("provider/vision-special", []),
    ("custom-captcha-model", []), ("gpt-oss:120b", [{"role": "user", "content": [
        {"type": "image_url", "image_url": {"url": "data:image/png;base64,c3ludGhldGlj"}}]}])])
def test_vision_skips_text_only_slots(monkeypatch, model, messages):
    monkeypatch.setattr(llm.config, "HH_CAPTCHA_VISION_MODEL", "custom-captcha-model")
    vision_cloud = llm.ProviderSpec("groq", "https://cloud.test/v1", "synthetic",
                                  {model: "cloud-vision-model"})
    client, calls = fake_chain(monkeypatch, [local(), local("ollama2", "qwen3:8b"), vision_cloud],
                               [AssertionError("must skip"), AssertionError("must skip"), response()])
    asyncio.run(client.chat.completions.create(model=model, messages=messages))
    assert calls == [("groq", "cloud-vision-model")]
    if not messages:
        assert local().model_for(model) == model


def test_vision_error_exhausts_even_when_last_entries_skipped(monkeypatch):
    client, calls = fake_chain(monkeypatch, [cloud(), local(), local("ollama2", "qwen3:8b")],
                               [StatusError(429), response(), response()])
    with pytest.raises(llm.LLMProvidersExhaustedError) as error:
        asyncio.run(client.chat.completions.create(model="qwen3-vl:235b-instruct", messages=[]))
    assert error.value.provider_names == ("groq",)
    assert calls == [("groq", "qwen3-vl:235b-instruct")]


def test_text_sticky_fallback_does_not_skip_cloud_for_image(monkeypatch):
    client, calls = fake_chain(monkeypatch, [cloud(), local(), cloud("openrouter")],
                               [StatusError(429), response(), response()])
    async def run():
        await client.chat.completions.create(model="gpt-oss:120b", messages=[])
        await client.chat.completions.create(model="gpt-oss:120b", messages=[{
            "role": "user", "content": [{"type": "image_url"}]}])
    asyncio.run(run())
    assert calls == [("groq", "cloud-own-model"), ("ollama", "qwen3-coder:30b"),
                     ("groq", "cloud-own-model"), ("openrouter", "cloud-own-model")]


def test_actual_provider_model_analytics_and_logs(monkeypatch, caplog):
    usage = []
    monkeypatch.setattr(llm, "_record_usage", lambda p, m, **kw: usage.append((p, m, kw)))
    client, _ = fake_chain(monkeypatch, [cloud(), local()], [StatusError(429), response()])
    with caplog.at_level(logging.INFO, logger="llm_client"):
        asyncio.run(client.chat.completions.create(model="gpt-oss:120b", messages=[]))
    assert [(p, m) for p, m, _ in usage] == [("groq", "cloud-own-model"), ("ollama", "qwen3-coder:30b")]
    assert usage[0][2]["error_kind"] == "StatusError"
    assert "response" in usage[1][2]
    assert "ollama served mapped model gpt-oss:120b -> qwen3-coder:30b" in caplog.text


@pytest.mark.parametrize("bad", ["nan", "inf", "0", "-1", "invalid"])
def test_invalid_timeout_has_finite_safe_default(bad):
    assert llm._ollama_timeout({"OLLAMA2_TIMEOUT_SECONDS": bad}, "OLLAMA2") == 60


@pytest.mark.parametrize("outcomes", [
    [StatusError(429), ConnectionRefusedError(), StatusError(404)],
    [StatusError(429), response(""), response()],
    [StatusError(429), response("not JSON"), response("not JSON")]])
def test_matcher_infrastructure_and_malformed_responses_stay_deferred(monkeypatch, outcomes):
    monkeypatch.setattr(matcher, "_load_resume", lambda: "Synthetic candidate")
    monkeypatch.setattr(matcher, "_build_matcher_truth_block", lambda: "")
    monkeypatch.setattr(matcher.config, "VACANCY_FILTER_POLICY", "generic")
    client, _ = fake_chain(monkeypatch, [cloud(), local(), local("ollama2", "qwen3:8b")], outcomes)
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    result = asyncio.run(matcher.evaluate_vacancy({"id": "synthetic:1", "title": "Synthetic vacancy"}))
    assert result["score"] is None
    assert result["evaluation_status"] == "deferred_unscored"
    assert not result["should_apply"]
    assert result["error_kind"] in {"llm_limits_exhausted", "llm_error"}


def test_sdk_transport_direct_bounded_no_retries(monkeypatch):
    original_http_client = httpx.AsyncClient
    transport_settings, requests = [], []

    def handle(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"id": "synthetic", "object": "chat.completion", "created": 1,
            "model": "qwen3-coder:30b", "choices": [{"index": 0, "finish_reason": "stop",
            "message": {"role": "assistant", "content": "Synthetic OK"}}]})

    class MockHTTPClient(original_http_client):
        def __init__(self, **kwargs):
            transport_settings.append(kwargs)
            super().__init__(**kwargs, transport=httpx.MockTransport(handle))

    monkeypatch.setattr(llm.httpx, "AsyncClient", MockHTTPClient)
    monkeypatch.setattr(llm.proxy_utils, "llm_http_client", lambda: pytest.fail("LAN must bypass cloud proxy"))
    client = llm.FallbackLLMClient([local(timeout=7)])

    async def run():
        result = await client.chat.completions.create(model="gpt-oss:120b", messages=[])
        assert result.model == "qwen3-coder:30b"
        assert client._clients[0].max_retries == 0
        assert client._clients[0].timeout.connect == 3
        await client.aclose()

    asyncio.run(run())
    assert requests[0]["model"] == "qwen3-coder:30b"
    assert requests[0]["reasoning_effort"] == "none"
    assert transport_settings[0]["trust_env"] is False
    assert transport_settings[0]["timeout"].read == 7


def test_cancellation_is_not_retried(monkeypatch):
    client, calls = fake_chain(monkeypatch, [local(), local("ollama2", "qwen3:8b")],
                               ["hang", response()])
    async def run():
        task = asyncio.create_task(client.chat.completions.create(model="gpt-oss:120b", messages=[]))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    asyncio.run(run())
    assert calls == [("ollama", "qwen3-coder:30b")]


def test_matcher_can_score_via_local_model_after_cloud_exhaustion(monkeypatch):
    monkeypatch.setattr(matcher, "_load_resume", lambda: "Synthetic candidate")
    monkeypatch.setattr(matcher, "_build_matcher_truth_block", lambda: "")
    monkeypatch.setattr(matcher.config, "VACANCY_FILTER_POLICY", "generic")
    answer = response(json.dumps({"score": 80, "reason": "Synthetic semantic match",
                                  "should_apply": True, "red_flags": []}))
    client, calls = fake_chain(monkeypatch, [cloud(), local()], [StatusError(429), answer])
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    result = asyncio.run(matcher.evaluate_vacancy({"id": "synthetic:1", "title": "Synthetic vacancy"}))
    assert result["score"] == 80
    assert "error_kind" not in result
    assert calls[-1] == ("ollama", "qwen3-coder:30b")


@pytest.mark.parametrize("explicit", [None, "high", "none"])
def test_thinking_default_is_provider_local_and_explicit_effort_preserved(explicit):
    client = llm.FallbackLLMClient([local()])
    request = {"model": "gpt-oss:120b", "messages": []}
    if explicit is not None:
        request["reasoning_effort"] = explicit
    before = dict(request)
    local_request = client._kwargs_for_provider(local(), request)
    cloud_request = client._kwargs_for_provider(cloud(), request)
    assert local_request["reasoning_effort"] == (explicit or "none")
    assert cloud_request.get("reasoning_effort") == explicit
    assert request == before
