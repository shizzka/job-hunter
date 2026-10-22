"""Offline lifecycle/CLI checks: audit only after final persisted observations."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import agent
import analytics
import config
from state_store.private_journal import read_json_records


@pytest.fixture
def auditor(monkeypatch):
    module = SimpleNamespace(enqueue_completed_run=Mock(return_value=True))
    monkeypatch.setitem(sys.modules, "run_auditor", module)
    return module


@pytest.mark.parametrize("dry_run", [False, True])
@pytest.mark.parametrize("exit_kind", ["success", "early", "exception", "cancelled"])
def test_search_audited_after_cleanup_and_final_history(monkeypatch, auditor, dry_run, exit_kind):
    order = []

    def audit(run_id, **destination):
        final = read_json_records(config.RUN_HISTORY_FILE)[-1]
        events = read_json_records(config.ANALYTICS_EVENTS_FILE)
        assert final["run_id"] == run_id and final["status"] == "finished"
        assert final["mode"] == ("dry-run" if dry_run else "search")
        assert events[-1]["event"] == "search_finished"
        assert order == ["cleanup"]
        order.append("audit")

    auditor.enqueue_completed_run.side_effect = audit

    @agent._observe_search
    async def search(dry_run=False):
        try:
            observation = analytics.current_search()
            if exit_kind == "exception":
                raise ValueError("private exception text")
            if exit_kind == "cancelled":
                raise asyncio.CancelledError()
            if exit_kind == "early":
                return observation.result
            agent._record_search_run(observation.result, dry_run, True)
            return observation.result
        finally:
            order.append("cleanup")

    if exit_kind in {"exception", "cancelled"}:
        with pytest.raises(ValueError if exit_kind == "exception" else asyncio.CancelledError):
            asyncio.run(search(dry_run=dry_run))
    else:
        asyncio.run(search(dry_run=dry_run))
    assert order == ["cleanup", "audit"]
    auditor.enqueue_completed_run.assert_called_once()


def test_auditor_failure_does_not_replace_result_or_exception(monkeypatch, auditor, caplog):
    auditor.enqueue_completed_run.side_effect = RuntimeError("PRIVATE_AUDIT_ERROR")

    @agent._observe_search
    async def search(dry_run=False):
        return {"ok": "original"}

    assert asyncio.run(search()) == {"ok": "original"}
    assert "Run audit handoff failed: error_kind=RuntimeError" in caplog.text
    assert "PRIVATE" not in caplog.text

    @agent._observe_search
    async def broken(dry_run=False):
        raise LookupError("original")

    with pytest.raises(LookupError, match="original"):
        asyncio.run(broken())


def test_auditor_runs_even_if_search_finalization_fails(monkeypatch, auditor):
    monkeypatch.setattr(analytics, "finish_unclassified", lambda observation: (_ for _ in ()).throw(OSError()))

    @agent._observe_search
    async def search(dry_run=False):
        return {"found": 2}

    assert asyncio.run(search()) == {"found": 2}
    auditor.enqueue_completed_run.assert_called_once()


def test_audit_destination_is_captured_before_profile_switch(monkeypatch, auditor, tmp_path):
    home = str(tmp_path / "first")
    monkeypatch.setattr(config, "JOB_HUNTER_HOME", home)
    monkeypatch.setattr(agent.profile_mod, "_active_profile", SimpleNamespace(name="first"))

    @agent._observe_search
    async def search(dry_run=False):
        monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path / "second"))
        monkeypatch.setattr(agent.profile_mod, "_active_profile", SimpleNamespace(name="second"))
        return {}

    asyncio.run(search())
    assert auditor.enqueue_completed_run.call_args.kwargs == {"home": home, "profile": "first"}


@pytest.mark.parametrize("exit_kind", ["success", "exception", "cancelled"])
def test_manual_audit_has_correlated_final_entry_after_cleanup(auditor, exit_kind):
    order = []
    run_ids = []

    @agent._observe_manual_apply("manual-ai-apply")
    async def apply():
        run_ids.append(analytics.current_context()["run_id"])
        try:
            if exit_kind == "exception":
                raise ValueError("PRIVATE_EXCEPTION")
            if exit_kind == "cancelled":
                raise asyncio.CancelledError()
            return {"ok": True, "message": "PRIVATE_RESULT"}
        finally:
            order.append("cleanup")

    def audit(run_id, **destination):
        assert order == ["cleanup"]
        final = read_json_records(config.RUN_HISTORY_FILE)[-1]
        assert final["run_id"] == run_id == run_ids[0]
        assert final["kind"] == "manual-apply" and final["status"] == "finished"
        assert final["ok"] is (exit_kind == "success")
        assert "PRIVATE" not in json.dumps(final)
        assert read_json_records(config.ANALYTICS_EVENTS_FILE)[-1]["event"] == "manual_finished"

    auditor.enqueue_completed_run.side_effect = audit
    if exit_kind == "success":
        assert asyncio.run(apply())["ok"] is True
    else:
        with pytest.raises(ValueError if exit_kind == "exception" else asyncio.CancelledError):
            asyncio.run(apply())
    auditor.enqueue_completed_run.assert_called_once()


def test_manual_missing_token_and_trace_unconfirmed_still_audited(monkeypatch, auditor):
    monkeypatch.setattr(agent.manual_apply_queue, "get_candidate", lambda token: None)
    assert asyncio.run(agent.do_manual_apply_token("PRIVATE_TOKEN"))["ok"] is False
    assert asyncio.run(agent.do_trace_apply("123"))["ok"] is False
    assert auditor.enqueue_completed_run.call_count == 2
    records = read_json_records(config.RUN_HISTORY_FILE)
    assert [entry["mode"] for entry in records if entry["status"] == "finished"] == ["manual-ai-apply", "trace-apply"]
    assert "PRIVATE_TOKEN" not in json.dumps(records)


def test_breaker_records_first_stop_at_its_actual_position_in_run_events():
    with analytics.event_context(run_id="synthetic-run"):
        breaker = agent.HHRecoveryBreaker()
        analytics.record_event({"event": "before_stop"})
        breaker.hard_stop("startup authentication blocked")
        breaker.hard_stop("updated diagnostic reason")
        analytics.record_event({"event": "after_stop"})
    events = read_json_records(config.ANALYTICS_EVENTS_FILE)
    assert [item["event"] for item in events] == ["before_stop", "hh_breaker_stop", "after_stop"]
    assert events[1]["run_id"] == "synthetic-run"
    assert events[1]["source"] == "hh" and events[1]["reason"] == "startup authentication blocked"


def test_trace_dispatch_reuses_audited_run_id(monkeypatch, auditor):
    from tests.test_agent_trace_apply import FakePage, FakeTrace
    client = SimpleNamespace(_page=FakePage(), start=AsyncMock(), is_logged_in=AsyncMock(return_value=True),
                             get_vacancy_details=AsyncMock(return_value="Manual and API testing"), stop=AsyncMock())
    dispatch = AsyncMock(return_value={"ok": True})
    monkeypatch.setattr(agent, "HHClient", lambda: client)
    monkeypatch.setattr(agent.apply_orchestrator, "create_hh_apply_trace", lambda *a, **kw: FakeTrace())
    monkeypatch.setattr(agent.apply_orchestrator, "dispatch_apply", dispatch)
    monkeypatch.setattr(agent, "generate_cover_letter", AsyncMock(return_value="Cover letter"))
    assert asyncio.run(agent.do_trace_apply("123", confirm_real=True))["ok"] is True
    assert dispatch.call_args.args[0]["_analytics_run_id"] == auditor.enqueue_completed_run.call_args.args[0]


@pytest.mark.parametrize("selector", [[], ["--run", "run-123"], ["--since", "2026-10-01T00:00:00+03:00"]])
def test_audit_cli_activates_selected_profile_without_lock(monkeypatch, auditor, selector):
    order = []
    monkeypatch.setattr(sys, "argv", ["agent.py", "--profile", "synthetic", "--audit", *selector])
    monkeypatch.setattr(agent.profile_mod, "activate_no_lock", lambda name: order.append(name))
    monkeypatch.setattr(agent, "_configure_logging", lambda **kwargs: None)
    for name in ("close_office_session", "close_notify_session", "close_llm_client"):
        monkeypatch.setattr(agent, name, AsyncMock())

    async def main(argv):
        assert order == ["synthetic"]
        assert argv == selector

    auditor.main = main
    asyncio.run(agent.main())


def test_run_sh_forwards_audit_profile_and_selector(tmp_path):
    wrapper = tmp_path / "python"
    wrapper.write_text("#!/bin/sh\nprintf '%s\\n' \"$@\"\n")
    wrapper.chmod(0o700)
    env = {**os.environ, "JOB_HUNTER_PYTHON": str(wrapper),
           "JOB_HUNTER_ENV_FILE": str(tmp_path / "absent.env")}
    result = subprocess.run([str(Path(agent.__file__).with_name("run.sh")), "--profile", "synthetic",
                             "audit", "--since", "2026-10-01T00:00:00+03:00"],
                            env=env, capture_output=True, text=True, check=True)
    assert result.stdout.splitlines() == ["agent.py", "--profile", "synthetic", "--audit", "--since",
                                          "2026-10-01T00:00:00+03:00"]
