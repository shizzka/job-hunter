import asyncio
import json
import os
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import apply_orchestrator
from debug_trace import ApplyTrace, cleanup_traces

SECRET_LIKE = "sk-" + ("x" * 24)


class FakePage:
    async def screenshot(self, *, path):
        Path(path).write_bytes(b"png")

    async def content(self):
        return (
            f"<html><body><script>const api_key='{SECRET_LIKE}'</script>"
            "<input name='password' value='hunter-secret'>"
            "<textarea>private cover letter</textarea><div data-qa='response-form'>DOM</div>"
            "<a href='https://example.test/path?session=private'>link</a>"
            "</body></html>"
        )


def test_apply_trace_writes_redacted_jsonl_and_human_summary(tmp_path, monkeypatch):
    monkeypatch.setattr("debug_trace._git_revision", lambda project_root: "abc1234")
    trace = ApplyTrace.create(
        home_dir=str(tmp_path),
        source="hh",
        vacancy_id="123456",
        profile="qa",
        mode="test",
    )

    trace.event(
        "PROFILE_CONTEXT",
        ok=True,
        cookies_file="/profiles/qa/hh_cookies.json",
        api_key=SECRET_LIKE,
        page_url="https://hh.ru/applicant/vacancy_response?vacancyId=123456&token=secret",
    )
    trace.event("RESULT_CHECK", ok=False, reason="submit_result_unknown")
    trace.finish(ok=False, message="Could not confirm application")

    rows = [json.loads(line) for line in trace.jsonl_path.read_text().splitlines()]
    serialized = trace.jsonl_path.read_text()
    summary = trace.summary_path.read_text()

    assert rows[0]["stage"] == "TRACE_STARTED"
    assert rows[1]["api_key"] == "[REDACTED]"
    assert rows[1]["cookies_file"].endswith("/hh_cookies.json")
    assert rows[1]["page_url"] == "https://hh.ru/applicant/vacancy_response?vacancyId=123456"
    assert SECRET_LIKE not in serialized
    assert "Failure stage: RESULT_CHECK" in summary
    assert "Result: FAIL" in summary
    assert oct(trace.jsonl_path.stat().st_mode & 0o777) == "0o600"
    assert oct(trace.summary_path.stat().st_mode & 0o777) == "0o600"


def test_apply_trace_captures_private_artifacts(tmp_path, monkeypatch):
    monkeypatch.setattr("debug_trace._git_revision", lambda project_root: "abc1234")
    trace = ApplyTrace.create(
        home_dir=str(tmp_path),
        source="hh",
        vacancy_id="42",
        profile="qa",
        mode="test",
    )

    saved = asyncio.run(trace.capture(FakePage(), "failure", screenshot=True, html=True))
    assert trace.last_stage == "TRACE_STARTED"
    trace.finish(ok=False, message="failed", failure_stage="RESULT_CHECK")

    assert set(saved) == {"screenshot", "html"}
    for path in saved.values():
        assert os.path.isfile(path)
        assert oct(os.stat(path).st_mode & 0o777) == "0o600"
    html = Path(saved["html"]).read_text()
    assert SECRET_LIKE not in html
    assert "hunter-secret" not in html
    assert "private cover letter" not in html
    assert "session=private" not in html
    assert "data-qa='response-form'" in html
    assert "failure.png" in trace.summary_path.read_text()
    assert "failure.html" in trace.summary_path.read_text()


def test_dispatch_apply_passes_and_finishes_existing_trace(tmp_path, monkeypatch):
    monkeypatch.setattr("debug_trace._git_revision", lambda project_root: "abc1234")
    monkeypatch.setattr(apply_orchestrator.analytics, "_append_event", lambda payload: None)
    monkeypatch.setattr(apply_orchestrator.company_blacklist, "is_blocked", lambda company: False)
    trace = ApplyTrace.create(
        home_dir=str(tmp_path),
        source="hh",
        vacancy_id="77",
        profile="qa",
        mode="test",
    )
    client = SimpleNamespace(
        _page=None,
        apply_to_vacancy=AsyncMock(return_value={"ok": True, "message": "sent"}),
    )

    result = asyncio.run(
        apply_orchestrator.dispatch_apply(
            {"id": "77", "source": "hh", "url": "https://hh.ru/vacancy/77"},
            "cover",
            hh_client=client,
            trace=trace,
            preferred_resume_id="synthetic-resume",
        )
    )

    assert result["ok"] is True
    assert result["trace_id"] == trace.trace_id
    assert result["trace_dir"] == str(trace.trace_dir)
    assert trace.finished is True
    assert client.apply_to_vacancy.await_args.kwargs["trace"] is trace
    assert "Result: OK" in trace.summary_path.read_text()


def test_trace_cleanup_caps_finished_runs_without_deleting_live_trace(tmp_path, monkeypatch):
    monkeypatch.setattr("debug_trace._git_revision", lambda project_root: "abc1234")
    live = ApplyTrace.create(
        home_dir=str(tmp_path), source="hh", vacancy_id="live", profile="qa", mode="test"
    )
    older = ApplyTrace.create(
        home_dir=str(tmp_path), source="hh", vacancy_id="old", profile="qa", mode="test"
    )
    older.finish(ok=True, message="done")
    newest = ApplyTrace.create(
        home_dir=str(tmp_path), source="hh", vacancy_id="new", profile="qa", mode="test"
    )
    newest.finish(ok=True, message="done")
    os.utime(older.trace_dir, (1, 1))

    cleanup_traces(tmp_path / "traces", retention_days=14, max_runs=1)

    assert live.trace_dir.is_dir()
    assert newest.trace_dir.is_dir()
    assert not older.trace_dir.exists()
