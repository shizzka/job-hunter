"""B1/NB1/NB2: read-only diagnostics, isolated fake actions and real SIGKILL."""
import asyncio
import json
import os
from pathlib import Path
import subprocess
import sys
import time
from types import SimpleNamespace

import pytest

import agent
import analytics
import config
import notifier
import reporting
import telegram_bot_ui as ui
from state_store.private_journal import read_json_records
from telegram_bot import TelegramBot
from tests.test_observability import configure_synthetic_search, search_home


def checkpoint(run_id="receipt-run", **extra):
    return {"run_id": run_id, "status": "incomplete", "ok": False,
            "applied": 0, "funnel": {}, **extra}


def write_records(path, records):
    Path(path).write_text("".join(json.dumps(record) + "\n" for record in records))


def readers(monkeypatch):
    bot = TelegramBot.__new__(TelegramBot)
    monkeypatch.setattr(bot, "_profile", lambda name: SimpleNamespace(
        run_history_file=config.RUN_HISTORY_FILE, analytics_events_file=config.ANALYTICS_EVENTS_FILE))
    return [reporting.load_recent_run_history(1)[0], bot._recent_runs("isolated", 1)[0]]


def test_B1_exact_reader_invariant_confirmed_receipt_not_zero(search_home, monkeypatch):
    write_records(config.RUN_HISTORY_FILE, [checkpoint()])
    analytics.record_event({"event": "application_result", "run_id": "receipt-run", "outcome": "sent"})
    original = Path(config.RUN_HISTORY_FILE).read_bytes()
    for record in readers(monkeypatch):
        text = ui.format_run_summary(record)
        assert ">=1" in text and "не завершён" in text
        assert "Отклики: 0" not in text and "Откликов: 0" not in text
    assert Path(config.RUN_HISTORY_FILE).read_bytes() == original


@pytest.mark.parametrize("journal", ["missing", "truncated", "unreadable", "wrong-run", "unconfirmed"])
def test_B1_incomplete_without_confirmed_receipts_is_unknown(search_home, monkeypatch, journal):
    write_records(config.RUN_HISTORY_FILE, [checkpoint(found=5, funnel={"new": 5, "applied": 0, "apply_attempt": 0},
        source_stats={"hh": {"applied": 0, "manual": 0}})])
    if journal == "truncated":
        Path(config.ANALYTICS_EVENTS_FILE).write_text('{"event":"application_result"')
    elif journal == "unreadable":
        Path(config.ANALYTICS_EVENTS_FILE).mkdir()
    elif journal == "wrong-run":
        write_records(config.ANALYTICS_EVENTS_FILE, [{"event": "application_result", "run_id": "other", "outcome": "sent"}])
    elif journal == "unconfirmed":
        write_records(config.ANALYTICS_EVENTS_FILE, [{"event": "application_result", "run_id": "receipt-run", "outcome": outcome}
            for outcome in ("failed", "error", "already_applied", "blocked", "uncertain")])
    for record in readers(monkeypatch):
        text = ui.format_run_summary(record)
        assert "неизвестно" in text and "0 откликов" not in text
        assert "Отклики: 0" not in text and "Откликов: 0" not in text and "applied 0" not in text
        assert "Попыток отклика: 0" not in text


def test_B1_raw_start_checkpoint_marks_action_counts_unknown():
    with analytics.observe_search("start-run", "search") as observation:
        record = observation.entry(incomplete=True)
    assert record["applied"] is None
    assert record["funnel"]["applied"] is None and record["funnel"]["apply_attempt"] is None
    assert "неизвестно" in ui.format_run_summary(record)


def test_B1_receipts_are_deduplicated_and_profile_bound(search_home, monkeypatch):
    write_records(config.RUN_HISTORY_FILE, [checkpoint()])
    receipt = {"event": "application_result", "run_id": "receipt-run", "outcome": "sent", "application_id": "a",
        "source": "hh", "vacancy_id": "1"}
    write_records(config.ANALYTICS_EVENTS_FILE, [receipt, receipt,
        {"event": "decision", "run_id": "receipt-run", "decision": "applied_auto", "source": "hh", "vacancy_id": "1"},
        {**receipt, "application_id": "b", "source": "habr", "vacancy_id": "2"},
        {**receipt, "application_id": "c", "run_id": "another-run"}])
    for record in readers(monkeypatch):
        text = ui.format_run_summary(record)
        assert "Откликов: >=2" in text or "Отклики: >=2" in text
        assert "hh:" in text and "habr:" in text
        assert text.count("applied >=1") == 2
    # No reader may accidentally use another selected profile's journal.
    selected = search_home / "selected.jsonl"
    write_records(selected, [checkpoint("selected-run")])
    bot = TelegramBot.__new__(TelegramBot)
    monkeypatch.setattr(bot, "_profile", lambda name: SimpleNamespace(run_history_file=str(selected),
        analytics_events_file=str(search_home / "missing-selected-events.jsonl")))
    assert "неизвестно" in ui.format_run_summary(bot._recent_runs("selected", 1)[0])


def test_B1_completed_zero_and_positive_counts_remain_exact(search_home):
    records = [{"run_id": "zero", "status": "finished", "ok": True, "applied": 0,
        "found": 2, "funnel": {"new": 2, "applied": 0, "apply_attempt": 0}, "reason_breakdown": {"keyword_filter": 2}},
        {"run_id": "one", "status": "finished", "ok": True, "applied": 1}]
    write_records(config.RUN_HISTORY_FILE, records)
    read = reporting.load_recent_run_history(2)
    assert "Отклики: 1" in ui.format_run_summary(read[0])
    assert "Откликов: 0" in ui.format_run_summary(read[1]) and "0 откликов" in ui.format_run_summary(read[1])


def test_B1_no_dispatch_final_event_can_establish_zero(search_home, monkeypatch):
    write_records(config.RUN_HISTORY_FILE, [checkpoint()])
    write_records(config.ANALYTICS_EVENTS_FILE, [{"event": "search_finished", "run_id": "receipt-run",
        "status": "finished", "applied": 0, "funnel": {"applied": 0, "apply_attempt": 0}}])
    for record in readers(monkeypatch):
        text = ui.format_run_summary(record)
        assert "не завершён" in text
        assert "Откликов: 0" in text or "Отклики: 0" in text


def configure_one_receipt(monkeypatch, home):
    real_dispatch = agent.apply_orchestrator.dispatch_apply
    configure_synthetic_search(monkeypatch, home)
    monkeypatch.setattr(config, "MAX_APPLICATIONS_PER_RUN", 1)
    monkeypatch.setattr(config, "HH_PRIMARY_RESUME_ID", "synthetic-resume")
    monkeypatch.setattr(agent.apply_orchestrator, "create_hh_apply_trace", lambda *args, **kwargs: None)
    calls = []
    async def native(*args, **kwargs):
        calls.append("sent")
        return {"ok": True}
    monkeypatch.setattr(agent.apply_orchestrator, "dispatch_apply", real_dispatch)
    monkeypatch.setattr(agent.apply_orchestrator, "_dispatch_apply", native)
    return calls


@pytest.mark.parametrize("final_loss", ["lost", "truncated"])
def test_B1_actual_search_lost_final_checkpoint_retains_receipt(search_home, monkeypatch, final_loss):
    calls = configure_one_receipt(monkeypatch, search_home)
    append = agent.append_json
    def lose_final(path, record, **kwargs):
        if str(path) == config.RUN_HISTORY_FILE and record.get("status") == "finished":
            if final_loss == "truncated":
                with Path(path).open("a") as stream:
                    stream.write('{"status":"finished",\n')
            raise OSError("Synthetic final write failure")
        return append(path, record, **kwargs)
    monkeypatch.setattr(agent, "append_json", lose_final)
    result = asyncio.run(agent.do_search())
    assert result["applied"] == 1 and calls == ["sent"]
    assert agent.seen._load()["4"]["action"] == "applied"
    assert len(read_json_records(config.RUN_HISTORY_FILE)) == 1
    assert any(e.get("event") == "application_result" and e.get("outcome") == "sent"
        for e in read_json_records(config.ANALYTICS_EVENTS_FILE))
    for record in readers(monkeypatch):
        assert ">=1" in ui.format_run_summary(record)
        assert record["status"] == "incomplete"


def test_B1_actual_search_SIGKILL_after_receipt_preserves_minimum(search_home, monkeypatch):
    child_home = search_home / "child"
    child_home.mkdir()
    source = r'''
import asyncio, json, socket, sys
from pathlib import Path
import pytest
import agent, config, analytics
from tests.test_observability_remediation import configure_one_receipt
home=Path(sys.argv[1])
def deny(*a, **k):raise AssertionError('No subprocess IP network')
socket.socket.connect=deny;socket.socket.connect_ex=deny
with pytest.MonkeyPatch.context() as patch:
    for key,name in {'JOB_HUNTER_HOME':'','RUN_HISTORY_FILE':'runs.jsonl','RUNTIME_STATUS_FILE':'runtime.json',
        'SEEN_VACANCIES_FILE':'seen.json','HH_STATE_DIR':'hh','HH_GUARD_STATE_FILE':'guard.json',
        'ANALYTICS_EVENTS_FILE':'events.jsonl','ANALYTICS_STATE_FILE':'analytics.json',
        'HH_COOKIES_FILE':'hh-cookies.json'}.items():patch.setattr(config,key,str(home/name))
    patch.setattr(config,'ANALYTICS_ENABLED',True)
    calls=configure_one_receipt(patch,home)
    client=agent.HHClient()
    async def stop():
        (home/'receipt-count.json').write_text(json.dumps({'native_calls':len(calls)}))
        (home/'ready').write_text('cleanup after successful receipt')
        await asyncio.Event().wait()
    patch.setattr(client,'stop',stop)
    asyncio.run(agent.do_search())
'''
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"), "HOME": str(child_home),
           "PYTHONDONTWRITEBYTECODE": "1", "PYTHONPATH": str(Path(__file__).resolve().parents[1])}
    process = subprocess.Popen([sys.executable, "-B", "-c", source, str(child_home)], env=env,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        deadline = time.monotonic() + 20
        while not (child_home / "ready").exists() and process.poll() is None and time.monotonic() < deadline:
            time.sleep(.02)
        assert (child_home / "ready").exists(), process.communicate(timeout=2)
        process.kill()
        process.communicate(timeout=5)
        assert process.returncode == -9
    finally:
        if process.poll() is None:
            process.kill();process.communicate(timeout=5)
    assert json.loads((child_home / "receipt-count.json").read_text())["native_calls"] == 1
    assert json.loads((child_home / "seen.json").read_text())["4"]["action"] == "applied"
    assert not any(e.get("event") == "search_finished" for e in read_json_records(child_home / "events.jsonl"))
    monkeypatch.setattr(config, "RUN_HISTORY_FILE", str(child_home / "runs.jsonl"))
    monkeypatch.setattr(config, "ANALYTICS_EVENTS_FILE", str(child_home / "events.jsonl"))
    for record in readers(monkeypatch):
        assert record["status"] == "incomplete"
        assert ">=1" in ui.format_run_summary(record)


@pytest.mark.parametrize("scalar", [None, "old", [], [1], 7, 2.5, True])
def test_NB1_non_object_json_history_records_are_ignored_without_rewrite(search_home, scalar):
    records = [scalar] + [{"created_at": str(i), "ok": True, "applied": 1} for i in range(6)]
    write_records(config.RUN_HISTORY_FILE, records)
    original = Path(config.RUN_HISTORY_FILE).read_bytes()
    assert reporting.load_recent_run_history(5) == list(reversed(records[-5:]))
    assert analytics.latest_run_records(records) == records[1:]
    assert Path(config.RUN_HISTORY_FILE).read_bytes() == original


@pytest.mark.parametrize("source", ["hh", "future-source"])
@pytest.mark.parametrize("bucket,expected", [({"relevant": 3}, "keyword pass 3"),
    ({"relevant": 3, "keyword_pass": 2}, "keyword pass 2"), ({"relevant": 3, "keyword_pass": 0}, "")])
def test_NB2_notifier_keyword_fallback_only_when_new_field_absent(source, bucket, expected):
    text = notifier._format_source_stats({source: bucket})
    assert expected in text if expected else text == ""
    assert "релевант" not in text


def test_B1_idless_history_cannot_borrow_uncorrelated_receipt(search_home, monkeypatch):
    write_records(config.RUN_HISTORY_FILE, [checkpoint(run_id=None)])
    write_records(config.ANALYTICS_EVENTS_FILE, [{"event": "application_result", "outcome": "sent"}])
    for record in readers(monkeypatch):
        assert "неизвестно" in ui.format_run_summary(record)
        assert ">=" not in ui.format_run_summary(record)


def test_B1_legacy_missing_application_count_is_not_assumed_zero(search_home):
    record = {"ok": True, "found": 3, "created_at": "legacy"}
    write_records(config.RUN_HISTORY_FILE, [record])
    text = ui.format_run_summary(reporting.load_recent_run_history(1)[0])
    assert "Отклики: неизвестно" in text and "0 откликов" not in text
