import asyncio

import habr_career_client
import hh_client
import matcher
import proxy_utils
import resume_analyzer


def test_browser_launch_env_strips_inherited_proxy_without_explicit_browser_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:7890")

    env = proxy_utils.browser_launch_env("")

    assert "HTTP_PROXY" not in env
    assert "HTTPS_PROXY" not in env
    assert "ALL_PROXY" not in env


def test_browser_launch_env_keeps_proxy_vars_when_explicit_browser_proxy_is_set(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")

    env = proxy_utils.browser_launch_env("http://127.0.0.1:7897")

    assert env["HTTP_PROXY"] == "http://127.0.0.1:7890"


def test_habr_client_retries_direct_when_env_proxy_fails(monkeypatch):
    created_sessions = []

    class FakeResponse:
        status = 200

        async def text(self):
            return "ok"

    class FakeRequestContext:
        def __init__(self, session):
            self.session = session

        async def __aenter__(self):
            if self.session.trust_env:
                raise RuntimeError(
                    "Cannot connect to host 127.0.0.1:7890 ssl:default "
                    "[Connect call failed ('127.0.0.1', 7890)]"
                )
            return FakeResponse()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    class FakeSession:
        def __init__(self, *, trust_env=False, **kwargs):
            self.trust_env = trust_env
            self.closed = False
            created_sessions.append(trust_env)

        async def close(self):
            self.closed = True

        def get(self, url):
            return FakeRequestContext(self)

    monkeypatch.setattr(habr_career_client.aiohttp, "ClientSession", FakeSession)

    client = habr_career_client.HabrCareerClient()
    text = asyncio.run(client._get_text("https://career.habr.com/vacancies/test"))

    assert text == "ok"
    assert created_sessions == [True, False]


def test_llm_http_client_ignores_env_proxy(monkeypatch):
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:7890")
    monkeypatch.setenv("HTTPS_PROXY", "http://127.0.0.1:7890")

    client = proxy_utils.llm_http_client()
    try:
        assert client._trust_env is False
    finally:
        asyncio.run(client.aclose())


def test_matcher_client_uses_direct_http_client(monkeypatch):
    captured = {}

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    matcher._client = None
    monkeypatch.setattr(matcher, "AsyncOpenAI", FakeAsyncOpenAI)

    matcher._get_client()

    try:
        assert captured["http_client"]._trust_env is False
    finally:
        asyncio.run(captured["http_client"].aclose())
        matcher._client = None


def test_hh_question_answer_client_uses_direct_http_client(monkeypatch):
    captured = {}

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    hh_client._question_answer_client = None
    monkeypatch.setattr(hh_client, "AsyncOpenAI", FakeAsyncOpenAI)

    hh_client._get_question_answer_client()

    try:
        assert captured["http_client"]._trust_env is False
    finally:
        asyncio.run(captured["http_client"].aclose())
        hh_client._question_answer_client = None


def test_resume_analyzer_client_uses_direct_http_client(monkeypatch):
    captured = {}

    class FakeAsyncOpenAI:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    resume_analyzer._client = None
    monkeypatch.setattr(resume_analyzer, "AsyncOpenAI", FakeAsyncOpenAI)

    resume_analyzer._get_client()

    try:
        assert captured["http_client"]._trust_env is False
    finally:
        asyncio.run(captured["http_client"].aclose())
        resume_analyzer._client = None
