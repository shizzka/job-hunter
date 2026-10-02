import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import agent


class FakeTrace:
    trace_id = "hh:123:qa:test"
    trace_dir = "/tmp/trace-test"

    def __init__(self):
        self.events = []
        self.finished = False
        self.failure_stage = ""
        self.last_stage = "TRACE_STARTED"

    def event(self, stage, *, ok=None, **fields):
        self.events.append((stage, ok, fields))
        self.last_stage = stage

    async def capture(self, *args, **kwargs):
        return {}

    def finish(self, *, ok, message="", failure_stage=""):
        self.finished = True
        self.failure_stage = failure_stage or self.last_stage


class FakePage:
    url = "https://hh.ru/"

    async def query_selector(self, selector):
        return None


def test_trace_apply_fails_before_llm_when_hh_session_is_not_authenticated(monkeypatch):
    trace = FakeTrace()
    client = SimpleNamespace(
        _page=FakePage(),
        start=AsyncMock(),
        is_logged_in=AsyncMock(return_value=False),
        stop=AsyncMock(),
    )
    monkeypatch.setattr(agent, "HHClient", lambda: client)
    monkeypatch.setattr(agent.apply_orchestrator, "create_hh_apply_trace", lambda *a, **kw: trace)
    monkeypatch.setattr(
        agent,
        "generate_cover_letter",
        AsyncMock(side_effect=AssertionError("LLM must not run before auth passes")),
    )

    result = asyncio.run(agent.do_trace_apply("123"))

    assert result == {"ok": False, "message": "HH-сессия не авторизована"}
    assert trace.events[0][0] == "HH_SESSION_CHECK"
    assert trace.events[0][1] is False
    assert trace.finished is True
    client.stop.assert_awaited_once()


def test_trace_apply_passes_one_trace_through_cover_generation_and_dispatch(monkeypatch):
    trace = FakeTrace()
    client = SimpleNamespace(
        _page=FakePage(),
        start=AsyncMock(),
        is_logged_in=AsyncMock(return_value=True),
        get_vacancy_details=AsyncMock(return_value="Manual and API testing"),
        stop=AsyncMock(),
    )
    dispatch = AsyncMock(return_value={"ok": True, "message": "sent"})
    monkeypatch.setattr(agent, "HHClient", lambda: client)
    monkeypatch.setattr(agent.apply_orchestrator, "create_hh_apply_trace", lambda *a, **kw: trace)
    monkeypatch.setattr(agent.apply_orchestrator, "dispatch_apply", dispatch)
    monkeypatch.setattr(agent, "generate_cover_letter", AsyncMock(return_value="Cover letter"))

    result = asyncio.run(agent.do_trace_apply("https://hh.ru/vacancy/123"))

    assert result["ok"] is True
    assert [event[0] for event in trace.events] == [
        "HH_SESSION_CHECK",
        "VACANCY_PREFLIGHT",
        "COVER_LETTER_GENERATED",
    ]
    assert dispatch.await_args.kwargs["trace"] is trace
    assert dispatch.await_args.args[0]["id"] == "123"
    client.stop.assert_awaited_once()
