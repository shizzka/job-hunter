"""Role allowlists and quota-only stickiness, with synthetic providers only."""
import asyncio
import json
import logging
from types import SimpleNamespace

import httpx
import pytest
from openai import APIConnectionError, APITimeoutError

import answer_grounding
import llm_client as llm
import matcher
import prompt_blocks


class StatusError(Exception):
    def __init__(self, status, message="Synthetic provider failure"):
        self.status_code = status
        super().__init__(message)


def response(content="Synthetic answer"):
    return SimpleNamespace(choices=[SimpleNamespace(
        finish_reason="stop", message=SimpleNamespace(content=content))])


@pytest.fixture(autouse=True)
def isolated_settings(monkeypatch):
    for key in list(llm.os.environ):
        if key.startswith("LLM_PROVIDER_ORDER"):
            monkeypatch.delenv(key)
    monkeypatch.setattr(llm.config, "LLM_API_KEY", "")
    settings = {
        "LLM_PROVIDER_ORDER": "groq,groq2",
        "GROQ_BASE_URL": "https://groq.test/v1", "GROQ_API_KEY": "one",
        "GROQ2_API_KEY": "two", "GEMINI_API_KEY": "three",
    }
    monkeypatch.setattr(llm, "_load_provider_env", lambda: settings)
    monkeypatch.setattr(llm, "_record_usage", lambda *a, **kw: None)
    monkeypatch.setattr(llm, "_observe_provider_failure", lambda *a, **kw: None)
    return settings


def install_sdk(monkeypatch, outcome):
    calls, clients = [], []

    class SDK:
        def __init__(self, **kwargs):
            self.key, self.closed = kwargs["api_key"], 0
            assert kwargs["max_retries"] == 0
            self.chat = SimpleNamespace(completions=self)
            clients.append(self)

        async def create(self, **kwargs):
            assert "llm_role" not in kwargs
            calls.append((self.key, kwargs))
            result = outcome(self.key, kwargs)
            if isinstance(result, Exception):
                raise result
            return result

        async def close(self):
            self.closed += 1

    monkeypatch.setattr(llm, "AsyncOpenAI", SDK)
    monkeypatch.setattr(llm.proxy_utils, "llm_http_client", lambda: None)
    return calls, clients


@pytest.mark.parametrize("role", [
    "matcher", "cover_letter", "cover_grounding", "cover_letter_rewrite", "answer_grounding",
])
def test_role_order_replaces_global_allowlist(isolated_settings, monkeypatch, role):
    # Gemini is deliberately absent from the global allowlist.
    isolated_settings[f"LLM_PROVIDER_ORDER_{role.upper()}"] = "gemini,groq"
    calls, _ = install_sdk(monkeypatch, lambda *a: response())
    client = llm.FallbackLLMClient()

    async def run():
        try:
            await client.chat.completions.create(model="synthetic", llm_role=role)
            await client.chat.completions.create(model="synthetic", llm_role="unconfigured")
            await client.chat.completions.create(model="synthetic")
        finally:
            await client.aclose()

    asyncio.run(run())
    assert [key for key, _ in calls] == ["three", "one", "one"]


@pytest.mark.parametrize("value", ["", "  "])
def test_empty_role_order_inherits_global(isolated_settings, monkeypatch, value):
    isolated_settings["LLM_PROVIDER_ORDER_MATCHER"] = "gemini"
    monkeypatch.setenv("LLM_PROVIDER_ORDER_MATCHER", value)
    assert [p.name for p in llm._build_provider_specs("matcher")] == ["groq", "groq2"]
    assert llm.FallbackLLMClient()._role_clients == {}


def test_role_environment_overrides_file_and_normalizes_provider_names(isolated_settings, monkeypatch):
    isolated_settings["LLM_PROVIDER_ORDER_MATCHER"] = "groq"
    monkeypatch.setenv("LLM_PROVIDER_ORDER_MATCHER", " Gemini, GEMINI, groq2 ")
    assert [p.name for p in llm._build_provider_specs("matcher")] == ["gemini", "groq2"]


@pytest.mark.parametrize("order,error", [
    ("groq_typo", ValueError), (",", ValueError), ("ollama", RuntimeError),
])
def test_invalid_role_order_fails_clearly_at_startup(isolated_settings, order, error):
    isolated_settings["LLM_PROVIDER_ORDER_COVER_GROUNDING"] = order
    with pytest.raises(error, match="LLM_PROVIDER_ORDER_COVER_GROUNDING"):
        llm.FallbackLLMClient()


def test_role_order_deduplicates_credentials_after_selection(isolated_settings):
    isolated_settings["GROQ2_API_KEY"] = "one"
    isolated_settings["LLM_PROVIDER_ORDER_MATCHER"] = "groq2,groq"
    assert [p.name for p in llm._build_provider_specs("matcher")] == ["groq2"]


def test_role_quota_state_is_isolated_and_all_clients_are_closed(isolated_settings, monkeypatch):
    isolated_settings["LLM_PROVIDER_ORDER_MATCHER"] = "groq,gemini"
    calls, sdks = install_sdk(monkeypatch, lambda key, kw: StatusError(429) if key == "one" else response())
    now = [100.0]
    client = llm.FallbackLLMClient(fallback_ttl_seconds=30, clock=lambda: now[0])

    async def run():
        await client.chat.completions.create(model="same", llm_role="matcher")
        await client.chat.completions.create(model="same", llm_role="matcher")
        await client.chat.completions.create(model="same", llm_role="cover_letter")
        now[0] = 131.0
        await client.chat.completions.create(model="same", llm_role="matcher")
        await client.aclose()
        await client.aclose()

    asyncio.run(run())
    assert [key for key, _ in calls] == ["one", "three", "three", "one", "two", "one", "three"]
    assert all(sdk.closed == 1 for sdk in sdks)
    assert not client._active_index_by_model
    assert not client._role_clients["matcher"]._active_index_by_model


REQUEST = httpx.Request("POST", "https://provider.test/v1/chat/completions")


@pytest.mark.parametrize("error,reason,sticky", [
    (StatusError(429), "LLM_QUOTA_OR_RATE_LIMIT", True),
    # 413: this request is too large for the provider, not a quota — no stickiness.
    (StatusError(413, "rate_limit_exceeded"), "LLM_REQUEST_TOO_LARGE", False),
    (RuntimeError("quota exhausted"), "LLM_QUOTA_OR_RATE_LIMIT", True),
    (APIConnectionError(request=REQUEST), "LLM_TRANSPORT_ERROR", False),
    (APITimeoutError(request=REQUEST), "LLM_TIMEOUT", False),
    (httpx.ConnectError("offline"), "LLM_TRANSPORT_ERROR", False),
    (TimeoutError(), "LLM_TIMEOUT", False),
    (StatusError(408), "LLM_TIMEOUT", False),
    (StatusError(500), "LLM_SERVICE_ERROR", False),
    (StatusError(503, "quota service temporarily down"), "LLM_SERVICE_ERROR", False),
    (StatusError(503, "model unavailable"), "LLM_SERVICE_ERROR", False),
    (StatusError(410), "LLM_MODEL_UNAVAILABLE", False),
])
def test_only_limits_activate_stickiness(monkeypatch, caplog, error, reason, sticky):
    calls, _ = install_sdk(monkeypatch, lambda key, kw: error if key == "one" else response())
    client = llm.FallbackLLMClient(fallback_ttl_seconds=30)
    with caplog.at_level(logging.WARNING, logger="llm_client"):
        async def run():
            await client.chat.completions.create(model="synthetic", llm_role="matcher")
            await client.chat.completions.create(model="synthetic", llm_role="matcher")
            await client.aclose()
        asyncio.run(run())
    assert [key for key, _ in calls] == (["one", "two", "two"] if sticky else ["one", "two", "one", "two"])
    assert f"reason={reason}" in caplog.text
    assert f"sticky_activated={str(sticky).lower()}" in caplog.text
    assert "role=matcher" in caplog.text


def test_zero_ttl_never_pins_a_rate_limited_provider(monkeypatch, caplog):
    calls, _ = install_sdk(monkeypatch, lambda key, kw: StatusError(429) if key == "one" else response())
    client = llm.FallbackLLMClient(fallback_ttl_seconds=0)
    async def run():
        await client.chat.completions.create(model="synthetic")
        await client.chat.completions.create(model="synthetic")
        await client.aclose()
    asyncio.run(run())
    assert [key for key, _ in calls] == ["one", "two", "one", "two"]
    assert "sticky_activated=true" not in caplog.text


@pytest.mark.parametrize("first,second,expected", [
    (StatusError(503), StatusError(429), ["one", "two", "three", "one", "two", "three"]),
    (StatusError(429), StatusError(503), ["one", "two", "three", "two", "three"]),
])
def test_mixed_failures_never_pin_past_transient_provider(isolated_settings, monkeypatch, first, second, expected):
    isolated_settings["LLM_PROVIDER_ORDER"] = "groq,groq2,gemini"
    calls, _ = install_sdk(monkeypatch, lambda key, kw: {"one": first, "two": second}.get(key, response()))
    client = llm.FallbackLLMClient(fallback_ttl_seconds=30)
    async def run():
        await client.chat.completions.create(model="synthetic")
        await client.chat.completions.create(model="synthetic")
        await client.aclose()
    asyncio.run(run())
    assert [key for key, _ in calls] == expected


def test_transient_exhaustion_has_no_extra_calls_or_sticky_state(monkeypatch):
    calls, _ = install_sdk(monkeypatch, lambda *a: StatusError(503))
    client = llm.FallbackLLMClient()
    async def run():
        for _ in range(2):
            with pytest.raises(llm.LLMProvidersExhaustedError):
                await client.chat.completions.create(model="synthetic")
        assert not client._active_index_by_model
        await client.aclose()
    asyncio.run(run())
    assert [key for key, _ in calls] == ["one", "two", "one", "two"]


def capture_client(*contents):
    calls = []
    async def create(**kwargs):
        calls.append(kwargs)
        content = contents[len(calls) - 1]
        return response(content)
    return SimpleNamespace(chat=SimpleNamespace(completions=SimpleNamespace(create=create)), calls=calls)


def test_matcher_json_repair_and_full_retry_keep_the_matcher_role():
    client = capture_client("invalid", '{"score":80}')
    result = asyncio.run(matcher._parse_evaluation_json_with_repair(client, "invalid", "synthetic", "model"))
    assert result == {"score": 80}
    assert [call["llm_role"] for call in client.calls] == ["matcher", "matcher"]


def test_matcher_routes_real_client_to_role_allowlist(isolated_settings, monkeypatch):
    isolated_settings["LLM_PROVIDER_ORDER_MATCHER"] = "gemini"
    result_json = json.dumps({"score": 80, "reason": "Synthetic match", "should_apply": True, "red_flags": []})
    calls, _ = install_sdk(monkeypatch, lambda *a: response(result_json))
    client = llm.FallbackLLMClient()
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    monkeypatch.setattr(matcher, "_load_resume", lambda: "Synthetic candidate")
    monkeypatch.setattr(matcher, "_build_matcher_truth_block", lambda *args: "")
    monkeypatch.setattr(matcher.config, "VACANCY_FILTER_POLICY", "generic")
    async def run():
        try:
            return await matcher.evaluate_vacancy({"title": "Synthetic vacancy", "id": "synthetic"})
        finally:
            await client.aclose()
    result = asyncio.run(run())
    assert result["score"] == 80
    assert [key for key, _ in calls] == ["three"]


def test_cover_author_rewrite_and_verifiers_receive_explicit_roles(monkeypatch):
    letter = "Проверяю REST API. Обсудим детали?"
    sources = {"resume": "Проверяю REST API."}
    supported = json.dumps({"verdict": "supported", "sentences": [
        {"index": 0, "supported": True, "evidence": [{"source": "resume", "quote": sources["resume"]}]},
        {"index": 1, "supported": True, "evidence": []},
    ]})
    client = capture_client(letter, '{"verdict":"unsupported","sentences":[]}', letter, supported)
    monkeypatch.setattr(matcher, "_get_client", lambda: client)
    monkeypatch.setattr(matcher, "_load_resume", lambda: sources["resume"])
    for name in ("build_profile_note_block", "build_knowledge_base_block"):
        monkeypatch.setattr(prompt_blocks, name, lambda **kw: "")
    monkeypatch.setattr(prompt_blocks, "build_cover_facts_blocks", lambda: ("", ""))
    async def filtered(*a, **kw):
        return ""
    monkeypatch.setattr(prompt_blocks, "build_filtered_kb_block", filtered)
    result = asyncio.run(matcher.generate_cover_letter({"title": "QA", "id": "synthetic"}))
    assert result == letter
    assert [call["llm_role"] for call in client.calls] == [
        "cover_letter", "cover_grounding", "cover_letter_rewrite", "cover_grounding",
    ]


def test_answer_verifier_receives_explicit_role():
    client = capture_client('{"verdict":"unsupported","answers":[]}')
    asyncio.run(answer_grounding.verify_answers(
        [{"index": 0, "answer": "Synthetic answer"}], {"resume": "Synthetic candidate"}, client, "model"))
    assert client.calls[0]["llm_role"] == "answer_grounding"
