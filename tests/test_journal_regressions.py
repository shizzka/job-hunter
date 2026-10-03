"""Old-tree-compatible journal/privacy/durability regressions."""
import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import agent
import debug_trace
from telegram_bot import TelegramBot


@pytest.fixture(params=["run", "debug", "audit"])
def journal(request, tmp_path, monkeypatch):
    path = tmp_path / "journal.jsonl"
    monkeypatch.setattr(agent.config, "RUN_HISTORY_FILE", str(path))
    host = SimpleNamespace(runtime_paths=SimpleNamespace(bot_debug_log_file=str(path)), profile_name="synthetic")
    writer = {"run": lambda record: agent._append_run_history(record),
              "debug": lambda record: TelegramBot._append_debug_log(host, "test", **record),
              "audit": lambda record: TelegramBot._append_chat_ai_audit_event(host, "test", **record)}[request.param]
    return path, writer


def test_append_separates_incomplete_old_tail(journal):
    path, writer = journal
    path.write_bytes(b'{"unfinished":')
    writer({"value": "fresh"})
    assert path.read_bytes().startswith(b'{"unfinished":\n')
    assert json.loads(path.read_text().splitlines()[-1])["value"] == "fresh"


def test_append_hardens_existing_public_file(journal):
    path, writer = journal
    path.write_text('')
    path.chmod(0o644)
    writer({"value": "private"})
    assert path.stat().st_mode & 0o777 == 0o600


def test_serialization_failure_does_not_edit_partial_tail(journal):
    path, writer = journal
    before = b'{"unfinished":'
    path.write_bytes(before)
    recursive = {}; recursive["nested"] = recursive
    try: writer(recursive)
    except ValueError: pass
    assert path.read_bytes() == before


def test_trace_text_short_write_does_not_truncate(tmp_path, monkeypatch):
    path = tmp_path / "summary.txt"
    original = os.write
    monkeypatch.setattr(debug_trace.os, "write", lambda fd, data: original(fd, data[:3]))
    debug_trace._private_write_text(path, "synthetic complete summary")
    assert path.read_text() == "synthetic complete summary"


def test_trace_summary_fsync_failure_preserves_old_file(tmp_path, monkeypatch):
    path = tmp_path / "summary.txt"
    path.write_text("original")
    def fail(*args): raise OSError("synthetic fsync failure")
    monkeypatch.setattr(debug_trace.os, "fsync", fail)
    with pytest.raises(OSError): debug_trace._private_write_text(path, "new")
    assert path.read_text() == "original"
