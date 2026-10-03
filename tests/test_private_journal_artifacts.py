"""Synthetic append, logging, PID, image and shell contracts."""
import asyncio
import concurrent.futures
import json
import logging
import multiprocessing
import os
import stat
import subprocess
from pathlib import Path

import pytest

import profile as profile_mod
from private_artifacts import capture_artifacts, private_screenshot, state_dir_for
from private_logging import PrivateFileHandler
from state_store.private_journal import append_json, write_all, open_private_append


def append_worker(args):
    path, index = args
    for offset in range(10): append_json(path, {"id": f"{index}-{offset}", "text": "я" * 1000})


@pytest.mark.parametrize("processes", [False, True])
def test_concurrent_complete_private_append_records(tmp_path, processes):
    path = tmp_path / "journal.jsonl"
    pool = (concurrent.futures.ProcessPoolExecutor(max_workers=4, mp_context=multiprocessing.get_context("spawn"))
            if processes else concurrent.futures.ThreadPoolExecutor(max_workers=30))
    with pool: list(pool.map(append_worker, [(str(path), i) for i in range(30)]))
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == len({row["id"] for row in rows}) == 300
    assert path.stat().st_mode & 0o777 == 0o600


def test_short_write_and_eintr_complete_the_record(tmp_path, monkeypatch):
    path = tmp_path / "journal.jsonl"
    original = os.write
    calls = 0
    def short(fd, data):
        nonlocal calls
        calls += 1
        if calls == 1: raise InterruptedError
        return original(fd, data[:3])
    monkeypatch.setattr(os, "write", short)
    append_json(path, {"text": "complete"})
    assert json.loads(path.read_text()) == {"text": "complete"}
    assert calls > 2


@pytest.mark.parametrize("failure", ["zero", "error"])
def test_partial_failed_append_rolls_back_own_bytes(tmp_path, monkeypatch, failure):
    path = tmp_path / "journal.jsonl"; path.write_bytes(b'{"old":1}\n')
    original = os.write; calls = 0
    def partial(fd, data):
        nonlocal calls
        calls += 1
        if calls == 1: return original(fd, data[:3])
        if failure == "zero": return 0
        raise OSError("synthetic append failure")
    with monkeypatch.context() as scoped:
        scoped.setattr(os, "write", partial)
        with pytest.raises(OSError): append_json(path, {"new": 2})
    assert path.read_bytes() == b'{"old":1}\n'
    append_json(path, {"new": 3})
    assert [json.loads(line) for line in path.read_text().splitlines()] == [{"old":1}, {"new":3}]


def test_serialization_failure_happens_before_creation(tmp_path):
    path = tmp_path / "journal.jsonl"
    with pytest.raises(TypeError): append_json(path, {"bad": object()})
    assert not path.exists() and not path.with_name('.journal.jsonl.lock').exists()


def test_symlink_and_nonregular_log_targets_are_not_followed(tmp_path):
    target = tmp_path / "private.txt"; target.write_text('original'); target.chmod(0o644)
    link = tmp_path / "symlink.log"; link.symlink_to(target)
    with pytest.raises(OSError): open_private_append(link)
    assert target.read_text() == 'original' and target.stat().st_mode & 0o777 == 0o644
    fifo = tmp_path / "fifo"; os.mkfifo(fifo)
    with pytest.raises(OSError): open_private_append(fifo)


def test_rotation_reopens_new_private_inode_without_erasing_history(tmp_path):
    path = tmp_path / "log.txt"
    handler = PrivateFileHandler(path)
    record = logging.LogRecord('synthetic', logging.INFO, '', 1, 'first', (), None)
    handler.emit(record)
    archived = tmp_path / "previous.log"; path.rename(archived)
    record.msg = 'second'; handler.emit(record); handler.close()
    assert archived.read_text() == 'first\n' and path.read_text() == 'second\n'
    assert path.stat().st_mode & 0o777 == 0o600


def test_log_failure_reports_type_once_without_echoing_sensitive_record(tmp_path, monkeypatch, capsys):
    handler = PrivateFileHandler(tmp_path / "log")
    def fail(*a, **k): raise OSError('synthetic-secret-exception')
    monkeypatch.setattr('private_logging.append_text', fail)
    record = logging.LogRecord('synthetic', logging.INFO, '', 1, 'synthetic-secret-record', (), None)
    handler.emit(record); handler.emit(record)
    error = capsys.readouterr().err
    assert error == 'Private log write failed: OSError\n'


def test_pid_short_writes_keep_original_flock_inode(tmp_path, monkeypatch):
    profile = profile_mod.Profile(name='synthetic', home_dir=str(tmp_path))
    path = tmp_path / '.lock'; path.write_text('old'); inode = path.stat().st_ino
    original = os.write
    monkeypatch.setattr(os, 'write', lambda fd, data: original(fd, data[:1]))
    try:
        profile_mod._acquire_lock(profile)
        assert path.read_text() == f'{os.getpid()}\n'
        assert path.stat().st_ino == inode and path.stat().st_mode & 0o777 == 0o600
    finally: profile_mod._release_lock()


class FakePage:
    async def screenshot(self, *, path, **options): Path(path).write_bytes(b'synthetic PNG')
    async def content(self): return '<input value="private"><script>secret</script><textarea>private</textarea><p>DOM</p>'


def test_artifacts_are_unique_private_and_html_is_sanitized(tmp_path):
    one = asyncio.run(capture_artifacts(FakePage(), tmp_path, '../same'))
    two = asyncio.run(capture_artifacts(FakePage(), tmp_path, '../same'))
    assert one['screenshot'] != two['screenshot']
    for saved in (one, two):
        for value in saved.values():
            path = Path(value)
            assert path.parent.parent == tmp_path
            assert path.stat().st_mode & 0o777 == 0o600
            assert path.parent.stat().st_mode & 0o777 == 0o700
        html = Path(saved['html']).read_text()
        assert 'private' not in html and 'secret' not in html and '<p>DOM</p>' in html


def test_capture_cancellation_cleans_only_own_new_directory(tmp_path):
    existing = tmp_path / 'existing'; existing.mkdir(); (existing / 'keep').write_text('retain')
    class Page(FakePage):
        async def content(self): raise asyncio.CancelledError
    with pytest.raises(asyncio.CancelledError): asyncio.run(capture_artifacts(Page(), tmp_path, 'cancel'))
    assert list(tmp_path.iterdir()) == [existing]


def test_screenshot_does_not_change_process_umask_over_await(tmp_path):
    current = os.umask(0o022)
    try:
        class Page(FakePage):
            async def screenshot(self, *, path, **options):
                observed = os.umask(0o022)
                assert observed == 0o022
                await super().screenshot(path=path)
        asyncio.run(private_screenshot(Page(), tmp_path / 'private.png'))
        assert (tmp_path / 'private.png').stat().st_mode & 0o777 == 0o600
    finally: os.umask(current)


def test_failed_screenshot_keeps_previous_complete_artifact(tmp_path):
    path = tmp_path / 'private.png'; path.write_bytes(b'original')
    class Page:
        async def screenshot(self, *, path, **options):
            Path(path).write_bytes(b'partial')
            raise OSError('synthetic browser failure')
    with pytest.raises(OSError): asyncio.run(private_screenshot(Page(), path))
    assert path.read_bytes() == b'original' and not list(tmp_path.glob('.screenshot-*'))


def test_capture_directory_cannot_follow_later_cwd(tmp_path, monkeypatch):
    first = tmp_path / 'a'; first.mkdir(); second = tmp_path / 'b'; second.mkdir()
    monkeypatch.chdir(first)
    class Page(FakePage):
        async def screenshot(self, *, path, **options):
            monkeypatch.chdir(second)
            await super().screenshot(path=path)
    saved = asyncio.run(capture_artifacts(Page(), 'state', 'bound'))
    assert Path(saved['html']).is_relative_to(first)
    assert not (second / 'state').exists()


def test_launch_scripts_enforce_private_default_and_syntax():
    root = Path(__file__).resolve().parents[1]
    for relative in ('run.sh', 'scripts/install_job_hunter_bot_user_service.sh'):
        script = root / relative
        assert 'umask 077' in script.read_text()
        assert subprocess.run(['bash', '-n', str(script)], capture_output=True).returncode == 0


def test_journal_readers_keep_good_records_after_invalid_utf8(tmp_path):
    import analytics
    import runtime_control
    from state_store.private_journal import read_json_records
    path = tmp_path/'history.jsonl'
    before = b'{"event":"first"}\n\xff\n[]\n{"event":"last"}\n{"partial":'
    path.write_bytes(before)
    assert read_json_records(path) == [{'event':'first'},{'event':'last'}]
    assert runtime_control.latest_run_entry(str(path)) == {'event':'last'}
    assert analytics._iter_events(str(path)) == [{'event':'first'},{'event':'last'}]
    assert path.read_bytes() == before


def test_analytics_context_captures_destinations_before_await(tmp_path, monkeypatch):
    import analytics
    import config
    from state_store.private_journal import read_json_records
    original = tmp_path/'a'; other=tmp_path/'b'
    monkeypatch.setattr(config,'ANALYTICS_ENABLED',True)
    monkeypatch.setattr(config,'ANALYTICS_EVENTS_FILE',str(original/'events.jsonl'))
    monkeypatch.setattr(config,'ANALYTICS_STATE_FILE',str(original/'state.json'))
    async def rotate():
        monkeypatch.setattr(config,'ANALYTICS_EVENTS_FILE',str(other/'events.jsonl'))
        monkeypatch.setattr(config,'ANALYTICS_STATE_FILE',str(other/'state.json'))
        analytics.record_invitations([{'id':'synthetic'}])
    asyncio.run(analytics.tracked_call('test','run',{'id':'1'},rotate))
    assert read_json_records(original/'events.jsonl')[0]['vacancy_id']=='synthetic'
    assert (original/'state.json').exists() and not other.exists()


def test_trace_path_and_capture_remain_bound_after_cwd_change(tmp_path,monkeypatch):
    from debug_trace import ApplyTrace
    first=tmp_path/'a'; first.mkdir(); other=tmp_path/'b'; other.mkdir()
    monkeypatch.chdir(first)
    trace=ApplyTrace.create(home_dir='state',source='hh',vacancy_id='synthetic',profile='qa',mode='test')
    monkeypatch.chdir(other)
    saved=asyncio.run(trace.capture(FakePage(),'failure'))
    trace.finish(ok=False)
    assert trace.jsonl_path.is_relative_to(first) and Path(saved['html']).is_relative_to(first)
    assert not (other/'state').exists()


def test_plain_log_failure_never_truncates_other_stdout_bytes(tmp_path,monkeypatch):
    from state_store.private_journal import append_text
    path=tmp_path/'log'; path.write_bytes(b'original\n')
    original=os.write; calls=0
    def partial(fd,data):
        nonlocal calls
        calls+=1
        if calls==1: return original(fd,data[:3])
        with path.open('ab') as stream: stream.write(b'other stdout\n')
        raise OSError('synthetic short-write failure')
    monkeypatch.setattr(os,'write',partial)
    with pytest.raises(OSError): append_text(path,'incomplete log\n')
    assert path.read_bytes()==b'original\nincother stdout\n'


def test_trace_safe_url_drops_userinfo():
    from debug_trace import safe_page_url
    assert safe_page_url('https://user:synthetic-password@example.test/path?token=private') == 'https://example.test/path'


def test_new_journal_parent_fsync_failure_prevents_record_write(tmp_path, monkeypatch):
    path = tmp_path / 'journal.jsonl'
    def fail(fd):
        raise OSError('synthetic parent fsync failure')
    monkeypatch.setattr(os, 'fsync', fail)
    with pytest.raises(OSError): append_json(path, {'new': 1})
    assert path.read_bytes() == b''


def test_existing_journal_fsync_failure_retains_complete_visible_record(tmp_path, monkeypatch):
    path = tmp_path / 'journal.jsonl'; path.write_bytes(b'{"old":1}\n')
    def fail(fd):
        raise OSError('synthetic file fsync failure')
    monkeypatch.setattr(os, 'fsync', fail)
    with pytest.raises(OSError): append_json(path, {'new': 2})
    assert [json.loads(line) for line in path.read_text().splitlines()] == [{'old': 1}, {'new': 2}]


@pytest.mark.parametrize('directory', [False, True])
def test_screenshot_fsync_failure_has_explicit_publication_boundary(tmp_path, monkeypatch, directory):
    path = tmp_path / 'image.png'; path.write_bytes(b'old image')
    original = os.fsync
    def fail(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode) == directory:
            raise OSError('synthetic image fsync failure')
        return original(fd)
    monkeypatch.setattr(os, 'fsync', fail)
    with pytest.raises(OSError): asyncio.run(private_screenshot(FakePage(), path))
    assert path.read_bytes() == (b'synthetic PNG' if directory else b'old image')


def test_hh_search_and_resume_diagnostics_do_not_use_raw_browser_writers():
    import inspect
    from hh_client import HHClient
    for method in (HHClient.search_vacancies, HHClient.download_resume_by_id):
        source = inspect.getsource(method)
        assert 'await self._save_debug_snapshot(' in source
        assert '.screenshot(' not in source and 'with open(' not in source
