"""Cancellation uses synthetic process tables and intercepted signals only."""
import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

import config
import runtime_control as runtime
from telegram_bot import TelegramBot


def process_table(monkeypatch, rows):
    original = Path.iterdir
    monkeypatch.setattr(Path, 'iterdir', lambda path: iter(Path('/proc') / str(pid) for pid in rows)
                        if path == Path('/proc') else original(path))
    monkeypatch.setattr(runtime, 'read_process_argv', lambda pid: rows.get(pid, []))
    monkeypatch.setattr(runtime, 'is_pid_running', lambda pid: pid in rows)
    monkeypatch.setattr(runtime.time, 'sleep', lambda _: None)


@pytest.mark.parametrize('kind,target,other', [('prod', 'qa', 'qa-dev'), ('dev', 'qa-dev', 'qa')])
def test_cancel_external_worker_only_signals_exact_profile(monkeypatch, kind, target, other):
    monkeypatch.setattr(config, 'JOB_HUNTER_INSTANCE_KIND', kind)
    rows = {900001: ['python', '/synthetic/agent.py', '--profile', target, '--search'],
            900002: ['python', '/synthetic/agent.py', '--profile', other, '--search'],
            900003: ['python', '/synthetic/agent.py', '--profile', target + '-extra', '--search']}
    process_table(monkeypatch, rows)
    signals = []
    def signal(pid, sig):
        signals.append(pid)
        rows.pop(pid)
    monkeypatch.setattr(runtime.os, 'killpg', signal)
    monkeypatch.setattr(runtime.os, 'kill', lambda *a: pytest.fail('Unexpected direct signal'))
    bot = TelegramBot.__new__(TelegramBot)
    bot._active_command = lambda _: None
    bot._menu_reply_markup = lambda _: {}
    bot._send_text = AsyncMock()
    asyncio.run(bot._cancel_active_command(101, {'role': 'admin', 'user_id': 101}, profile_name=target))
    assert signals == [900001]
    assert set(rows) == {900002, 900003}


@pytest.mark.parametrize('changed_at', ['term', 'kill'])
def test_cancel_revalidates_process_ownership_before_each_signal(monkeypatch, changed_at):
    monkeypatch.setattr(config, 'JOB_HUNTER_INSTANCE_KIND', 'prod')
    original = ['python', 'agent.py', '--profile', 'qa', '--search']
    rows = {900001: original}
    process_table(monkeypatch, rows)
    signals = []
    def signal(pid, sig):
        signals.append(sig)
        rows[pid] = ['python', 'agent.py', '--profile', 'qa-dev', '--search']
    monkeypatch.setattr(runtime.os, 'killpg', signal)
    monkeypatch.setattr(runtime.os, 'kill', lambda *a: pytest.fail('Unexpected direct signal'))
    if changed_at == 'term':
        rows[900001] = ['python', 'agent.py', '--profile', 'qa-dev', '--search']
    result = runtime.stop_pid(900001, timeout=0, process_group=True,
                              expected_profile='qa', expected_argv=original)
    assert result['ownership_changed'] and not result['ok']
    assert signals == ([] if changed_at == 'term' else [runtime.signal.SIGTERM])


def test_proc_argument_reader_keeps_spaces_inside_argument(monkeypatch):
    monkeypatch.setattr(Path, 'read_bytes', lambda _: b'python\0agent.py\0--profile\0qa-dev\0--note\0--profile qa\0--search\0')
    argv = runtime.read_process_argv(900001)
    assert argv[-2:] == ['--profile qa', '--search']
    assert not runtime._agent_process_flag(argv, 'qa')
    assert runtime._agent_process_flag(argv, 'qa-dev') == '--search'
