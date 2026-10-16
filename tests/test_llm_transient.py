"""Long-role transient recovery, using the real SDK over offline transports."""
import asyncio
import json
import logging
import time

import httpx
import pytest

import llm_client as llm


LONG_ROLES = ("cover_letter", "cover_grounding", "cover_letter_rewrite", "answer_grounding")


def completion(finish="stop"):
    return httpx.Response(200, json={
        "id": "synthetic", "object": "chat.completion", "created": 0, "model": "synthetic",
        "choices": [{"index": 0, "finish_reason": finish,
                     "message": {"role": "assistant", "content": "Synthetic answer"}}],
    })


def status(code, error_code="synthetic_error"):
    return httpx.Response(code, json={"error": {"message": error_code, "code": error_code}})


@pytest.fixture
def chain(monkeypatch):
    monkeypatch.setattr(llm.config, "LLM_GROUNDING_TIMEOUT_SECONDS", 120)
    monkeypatch.setattr(llm, "_record_usage", lambda *a, **kw: None)
    monkeypatch.setattr(llm, "_observe_provider_failure", lambda *a, **kw: None)

    def build(outcomes, elapsed=None):
        calls = []
        now = [0.0]
        real_http_client = httpx.AsyncClient

        async def handle(request):
            calls.append(request)
            if elapsed:
                now[0] += elapsed[len(calls) - 1]
            outcome = outcomes[len(calls) - 1]
            if isinstance(outcome, Exception):
                raise outcome
            if outcome == "hang":
                await asyncio.Event().wait()
            return outcome

        monkeypatch.setattr(llm.proxy_utils, "llm_http_client", lambda: real_http_client(
            timeout=httpx.Timeout(60, connect=10), transport=httpx.MockTransport(handle)))
        client = llm.FallbackLLMClient([
            llm.ProviderSpec(name, f"https://{name}.test/v1", "synthetic")
            for name in ("gemini", "backup", "local")
        ], clock=lambda: now[0])
        return client, calls

    return build


def run(client, role="cover_grounding"):
    async def invoke():
        try:
            return await client.chat.completions.create(
                llm_role=role, model="synthetic", messages=[{"role": "user", "content": "Synthetic"}])
        finally:
            await client.aclose()
    return asyncio.run(invoke())


@pytest.mark.parametrize("role", LONG_ROLES + ("matcher", None, "unconfigured"))
@pytest.mark.parametrize("configured", [97, 143])
def test_http_read_timeout_follows_role_without_changing_connect(chain, monkeypatch, role, configured):
    monkeypatch.setattr(llm.config, "LLM_GROUNDING_TIMEOUT_SECONDS", configured)
    client, calls = chain([completion()])
    run(client, role)
    timeout = calls[0].extensions["timeout"]
    assert timeout == {"connect": 10, "read": configured if role in LONG_ROLES else 60,
                       "write": 60, "pool": 60}
    assert client._clients == {}


@pytest.mark.parametrize("role", LONG_ROLES)
@pytest.mark.parametrize("error", [
    httpx.ConnectError("offline"), httpx.ReadError("connection reset"),
    httpx.ReadTimeout("read timeout"), httpx.ConnectTimeout("connect timeout"),
    status(500), status(502), status(503), status(599),
])
def test_transient_retries_same_provider_once(chain, caplog, role, error):
    client, calls = chain([error, completion()])
    with caplog.at_level(logging.INFO, logger="llm_client"):
        result = run(client, role)
    assert result.choices[0].message.content == "Synthetic answer"
    assert [request.url.host for request in calls] == ["gemini.test", "gemini.test"]
    assert json.loads(calls[0].content) == json.loads(calls[1].content)
    assert "transient_retry=started" in caplog.text
    assert "transient_retry=succeeded" in caplog.text
    assert "reason=LLM_" in caplog.text


def test_one_extra_call_budget_is_shared_across_provider_chain(chain, caplog):
    client, calls = chain([status(503), status(503), status(502), completion()])
    with caplog.at_level(logging.INFO, logger="llm_client"):
        run(client)
    assert [r.url.host for r in calls] == ["gemini.test", "gemini.test", "backup.test", "local.test"]
    assert "transient_retry=failed" in caplog.text
    assert "transient_retry=skipped" in caplog.text
    assert "retry_budget_exhausted" in caplog.text


@pytest.mark.parametrize("error", [status(429), status(413, "rate_limit_exceeded"),
                                  status(400, "insufficient_quota"), status(503, "rate_limit_exceeded"),
                                  status(503, "insufficient_quota"), status(404), status(410)])
def test_quota_and_unavailable_models_fall_through_without_retry(chain, error):
    client, calls = chain([error, completion()])
    run(client)
    assert [r.url.host for r in calls] == ["gemini.test", "backup.test"]


@pytest.mark.parametrize("code", [400, 401, 403, 408, 413, 422])
def test_other_4xx_are_never_retried(chain, code):
    from openai import APIStatusError
    client, calls = chain([status(code), completion()])
    if code == 408:  # Existing fallback policy is preserved; no same-provider retry.
        run(client)
        assert [r.url.host for r in calls] == ["gemini.test", "backup.test"]
    else:
        with pytest.raises(APIStatusError):
            run(client)
        assert len(calls) == 1


def test_truncated_response_is_returned_without_retry(chain):
    client, calls = chain([completion("length")])
    assert run(client).choices[0].finish_reason == "length"
    assert len(calls) == 1


@pytest.mark.parametrize("role", ("matcher", None, "unconfigured"))
def test_short_or_unknown_roles_keep_single_attempt(chain, role):
    client, calls = chain([httpx.ConnectError("offline"), completion()])
    run(client, role)
    assert [r.url.host for r in calls] == ["gemini.test", "backup.test"]


@pytest.mark.parametrize("elapsed", [61, 111])
def test_retry_not_started_without_useful_deadline_budget(chain, caplog, elapsed):
    client, calls = chain([httpx.ReadTimeout("offline"), completion()], elapsed=[elapsed, 0])
    run(client)
    assert [r.url.host for r in calls] == ["gemini.test", "backup.test"]
    assert "transient_retry=skipped" in caplog.text
    assert "deadline_budget" in caplog.text


def test_deadline_is_shared_when_fallback_provider_retries(chain, caplog):
    client, calls = chain([status(429), httpx.ReadTimeout("offline"), completion()], elapsed=[80, 25, 0])
    run(client)
    assert [r.url.host for r in calls] == ["gemini.test", "backup.test", "local.test"]
    assert "deadline_budget" in caplog.text


def test_long_call_has_hard_total_deadline(chain, monkeypatch):
    monkeypatch.setattr(llm.config, "LLM_GROUNDING_TIMEOUT_SECONDS", 1)
    client, calls = chain(["hang"])
    async def invoke():
        try:
            # The extra guard keeps this regression bounded on a broken client.
            await asyncio.wait_for(client.chat.completions.create(
                llm_role="cover_letter", model="synthetic", messages=[]), timeout=2)
        finally:
            await client.aclose()

    started = time.monotonic()
    with pytest.raises(TimeoutError):
        asyncio.run(invoke())
    assert time.monotonic() - started < 1.8
    assert len(calls) == 1


def test_external_cancellation_during_retry_never_starts_fallback(chain, caplog):
    client, calls = chain([httpx.ConnectError("offline"), "hang"])

    async def invoke():
        try:
            with pytest.raises(TimeoutError):
                await asyncio.wait_for(client.chat.completions.create(
                    llm_role="answer_grounding", model="synthetic", messages=[]), timeout=0.05)
        finally:
            await client.aclose()

    asyncio.run(invoke())
    assert [r.url.host for r in calls] == ["gemini.test", "gemini.test"]
    assert "transient_retry=cancelled" in caplog.text


def test_expired_deadline_stops_both_retry_and_fallback(chain, caplog):
    client, calls = chain([httpx.ReadTimeout("offline")], elapsed=[120])
    with pytest.raises(TimeoutError, match="deadline exhausted"):
        run(client)
    assert len(calls) == 1
    assert "deadline_budget" in caplog.text


def test_retry_budget_resets_for_each_request(chain):
    client, calls = chain([status(503), completion(), status(503), completion()])

    async def invoke():
        try:
            for _ in range(2):
                await client.chat.completions.create(llm_role="cover_letter", model="synthetic", messages=[])
        finally:
            await client.aclose()

    asyncio.run(invoke())
    assert [r.url.host for r in calls] == ["gemini.test"] * 4


def test_long_role_quota_still_pins_only_its_provider_prefix(chain):
    client, calls = chain([status(429), completion(), completion()])

    async def invoke():
        try:
            for _ in range(2):
                await client.chat.completions.create(llm_role="answer_grounding", model="synthetic", messages=[])
        finally:
            await client.aclose()

    asyncio.run(invoke())
    assert [r.url.host for r in calls] == ["gemini.test", "backup.test", "backup.test"]


def test_role_override_never_changes_cached_http_defaults(chain):
    client, calls = chain([completion(), completion(), completion()])

    async def invoke():
        try:
            await asyncio.gather(*(client.chat.completions.create(
                llm_role=role, model="synthetic", messages=[])
                for role in ("cover_letter", "matcher", None)))
            assert client._clients[0].timeout.read == 60
            assert client._clients[0].max_retries == 0
        finally:
            await client.aclose()

    asyncio.run(invoke())
    assert sorted(r.extensions["timeout"]["read"] for r in calls) == [60, 60, 120]
