"""Offline fault/concurrency checks for synchronous form state mutations."""
import json
import fcntl
import multiprocessing
import os
import stat
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from google_forms import drafts
from state_store.google_forms import GoogleFormStateRepository, form_state_lock
from state_store import json_store


TOKEN = "abcdef123456"
NEXT = "123456abcdef"


def _preview(token=TOKEN, *, status="preview", count=40):
    return {"token": token, "status": status, "created_at": int(time.time()),
            "questions": [{"index": index, "question": f"Synthetic field {index}", "type": "text"}
                          for index in range(count)], "answers": []}


@pytest.fixture
def home(tmp_path):
    GoogleFormStateRepository(tmp_path).remember(TOKEN, _preview(), trim_expired=False)
    return str(tmp_path)


def _path(home, kind):
    return Path(home) / ("google_form_previews.json" if kind == "preview" else "google_form_edits.json")


def _mutate(home, kind, index=0):
    if kind == "preview":
        token = f"{index + 1:012x}"
        return GoogleFormStateRepository(home).remember(token, _preview(token), trim_expired=False)
    return drafts.save_answer(home, TOKEN, index, f"Synthetic answer {index}", 7)


@pytest.mark.parametrize("kind", ["preview", "edits"])
@pytest.mark.parametrize("content", [b'{"broken":', b'[]', b'\xff', b'{"items":[]}', b'{"items":{"bad":null},"abcdef123456":{"answers":[]}}'])
def test_corruption_blocks_repeated_mutations_without_reset(home, kind, content):
    path = _path(home, kind)
    path.write_bytes(content)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="[Cc]orrupt|[Rr]estore"):
            _mutate(home, kind)
        assert path.read_bytes() == content
        assert not list(path.parent.glob(f"{path.name}.corrupt-*"))


@pytest.mark.parametrize("kind", ["preview", "edits"])
def test_parallel_mutations_keep_every_record(home, kind, monkeypatch):
    original = json_store.JsonStore._save_unlocked
    def delayed(self, value):
        time.sleep(0.02)
        return original(self, value)
    monkeypatch.setattr(json_store.JsonStore, "_save_unlocked", delayed)
    barrier = threading.Barrier(6)
    def worker(index):
        barrier.wait(timeout=5)
        return _mutate(home, kind, index)
    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(worker, range(6)))
    if kind == "preview":
        assert len(GoogleFormStateRepository(home).load()["items"]) == 7
    else:
        assert len(drafts.manual_answers(home, TOKEN)) == 6


@pytest.mark.parametrize("status", sorted(drafts.TERMINAL))
def test_remember_does_not_reopen_terminal_preview(home, status):
    repository = GoogleFormStateRepository(home)
    repository.remember(TOKEN, _preview(status=status), trim_expired=False)
    before = _path(home, "preview").read_bytes()
    with pytest.raises(ValueError, match="terminal|отправ|финаль"):
        repository.remember(TOKEN, _preview(), trim_expired=False)
    assert _path(home, "preview").read_bytes() == before


def test_supersede_keeps_answers_and_first_successor(home):
    drafts.save_answer(home, TOKEN, 0, "Synthetic answer", 7)
    drafts.supersede(home, TOKEN, NEXT)
    before = _path(home, "edits").read_bytes()
    with pytest.raises(ValueError, match="новая|верси|supersed"):
        drafts.supersede(home, TOKEN, "bbbbbbbbbbbb")
    assert _path(home, "edits").read_bytes() == before
    assert drafts.manual_answers(home, TOKEN)


def test_idempotent_supersede_does_not_rewrite_edits(home, monkeypatch):
    drafts.supersede(home, TOKEN, NEXT)
    def forbidden(*args):
        raise AssertionError("Identical successor must not rewrite state")
    monkeypatch.setattr(json_store.JsonStore, "_save_unlocked", forbidden)
    drafts.supersede(home, TOKEN, NEXT)


def test_unknown_draft_cannot_create_supersede_record(tmp_path):
    with pytest.raises(ValueError, match="не найден|устарел"):
        drafts.supersede(str(tmp_path), TOKEN, NEXT)
    assert not _path(tmp_path, "edits").exists()


@pytest.mark.parametrize("operation", ["fsync", "replace"])
@pytest.mark.parametrize("kind", ["preview", "edits"])
def test_write_failure_preserves_original_and_cleans_temp(home, kind, operation, monkeypatch):
    _mutate(home, kind)
    path = _path(home, kind)
    before = path.read_bytes()
    def fail(*args):
        raise OSError("synthetic persistence failure")
    monkeypatch.setattr(json_store.os, operation, fail)
    with pytest.raises(OSError, match="synthetic persistence failure"):
        _mutate(home, kind, 1)
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".*.tmp"))


@pytest.mark.parametrize("kind", ["preview", "edits"])
def test_read_permission_failure_preserves_original(home, kind, monkeypatch):
    _mutate(home, kind)
    path = _path(home, kind)
    before = path.read_bytes()
    original = Path.open
    def denied(self, *args, **kwargs):
        if self == path:
            raise PermissionError("synthetic read denial")
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(PermissionError, match="synthetic read denial"):
        _mutate(home, kind, 1)
    with original(path, "rb") as stream:
        assert stream.read() == before


@pytest.mark.parametrize("kind", ["preview", "edits"])
def test_private_fsync_backed_writes(home, kind, monkeypatch):
    calls = []
    original = os.fsync
    def sync(fd):
        calls.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        return original(fd)
    monkeypatch.setattr(json_store.os, "fsync", sync)
    _mutate(home, kind)
    assert stat.S_IMODE(_path(home, kind).stat().st_mode) == 0o600
    assert calls == ["file", "directory"]


def _process_writer(home, kind, index, barrier):
    barrier.wait(timeout=10)
    for offset in range(10):
        _mutate(home, kind, index * 10 + offset)


@pytest.mark.parametrize("kind", ["preview", "edits"])
def test_separate_processes_preserve_every_record(home, kind):
    context = multiprocessing.get_context("spawn")
    barrier = context.Barrier(4)
    workers = [context.Process(target=_process_writer, args=(home, kind, index, barrier)) for index in range(4)]
    try:
        for worker in workers:
            worker.start()
        for worker in workers:
            worker.join(timeout=15)
            assert worker.exitcode == 0
    finally:
        for worker in workers:
            if worker.pid is not None:
                if worker.is_alive():
                    worker.terminate()
                worker.join(timeout=5)
    if kind == "preview":
        assert len(GoogleFormStateRepository(home).load()["items"]) == 41
    else:
        assert len(drafts.manual_answers(home, TOKEN)) == 40
    assert stat.S_IMODE((Path(home) / ".google_form_workflow.lock").stat().st_mode) == 0o600


@pytest.mark.parametrize("transition", ["terminal", "superseded"])
def test_answer_checks_eligibility_after_waiting_for_workflow_lock(home, transition, monkeypatch):
    waiting = threading.Event()
    original = fcntl.flock
    def observe(fd, operation):
        if (operation == fcntl.LOCK_EX and threading.current_thread() is not threading.main_thread()
                and os.readlink(f"/proc/self/fd/{fd}").endswith(".google_form_workflow.lock")):
            waiting.set()
        return original(fd, operation)
    monkeypatch.setattr(fcntl, "flock", observe)
    with ThreadPoolExecutor(max_workers=1) as pool:
        with form_state_lock(home):
            future = pool.submit(drafts.save_answer, home, TOKEN, 0, "Synthetic answer", 7)
            assert waiting.wait(timeout=5)
            # Test fixture owns the coordinator, just like a cooperating writer.
            if transition == "terminal":
                GoogleFormStateRepository(home)._store.update(
                    lambda state: state["items"][TOKEN].update(status="submitted"))
            else:
                drafts.edits_store(home).update(lambda state: state.setdefault(TOKEN, {}).update(superseded_by=NEXT))
        with pytest.raises(ValueError):
            future.result(timeout=5)
    assert drafts.manual_answers(home, TOKEN) == {}


def test_simultaneous_superseders_keep_one_successor_and_answers(home):
    drafts.save_answer(home, TOKEN, 0, "Synthetic answer", 7)
    barrier = threading.Barrier(6)
    def replace(index):
        new_token = f"{index + 1:012x}"
        barrier.wait(timeout=5)
        try:
            drafts.supersede(home, TOKEN, new_token)
        except ValueError:
            return None
        return new_token
    with ThreadPoolExecutor(max_workers=6) as pool:
        winners = [token for token in pool.map(replace, range(6)) if token is not None]
    assert len(winners) == 1
    assert drafts.edits_store(home).load()[TOKEN]["superseded_by"] == winners[0]
    assert len(drafts.manual_answers(home, TOKEN)) == 1


@pytest.mark.parametrize("kind", ["preview", "edits"])
def test_serialization_failure_keeps_original(home, kind):
    _mutate(home, kind)
    path = _path(home, kind)
    before = path.read_bytes()
    with pytest.raises(TypeError):
        if kind == "preview":
            detail = _preview()
            detail["unsupported_metadata"] = {1, 2}
            GoogleFormStateRepository(home).remember(TOKEN, detail, trim_expired=False)
        else:
            drafts.save_answer(home, TOKEN, 1, "Synthetic answer", {1, 2})
    assert path.read_bytes() == before
    assert not list(path.parent.glob(".*.tmp"))


@pytest.mark.parametrize("bad", [{"items": []}, {"items": {"bad": None}}, {"items": {"bad": {"questions": [None]}}}])
def test_invalid_snapshot_save_cannot_erase_existing_previews(home, bad):
    path = _path(home, "preview")
    before = path.read_bytes()
    with pytest.raises(ValueError, match="Invalid"):
        GoogleFormStateRepository(home).save(bad)
    assert path.read_bytes() == before


@pytest.mark.parametrize("bad", [{"answers": {}}, {"answers": [None]}, {"answers": [{"index": 0}], "vacancy": []}])
def test_invalid_preview_detail_is_not_published(home, bad):
    before = _path(home, "preview").read_bytes()
    with pytest.raises(ValueError, match="Invalid"):
        GoogleFormStateRepository(home).remember("new", bad, trim_expired=False)
    assert _path(home, "preview").read_bytes() == before


@pytest.mark.parametrize("new_token", [TOKEN, "invalid", "../other", None])
def test_invalid_successor_does_not_write_edits(home, new_token):
    with pytest.raises(ValueError):
        drafts.supersede(home, TOKEN, new_token)
    assert not _path(home, "edits").exists()


def test_metadata_and_other_tokens_survive_answer_update(home):
    first = drafts.edits_store(home)
    first.save({TOKEN: {"answers": {}, "extra": "keep"}, NEXT: {"answers": {}, "extra": "other"}})
    answer = drafts.save_answer(home, TOKEN, 2, "Synthetic answer", 7)
    state = first.load()
    assert state[TOKEN]["extra"] == "keep"
    assert state[NEXT] == {"answers": {}, "extra": "other"}
    assert state[TOKEN]["answers"] == {drafts.question_key(_preview()["questions"][2]): answer}


def test_terminal_detail_noop_does_not_rewrite_file(home, monkeypatch):
    repository = GoogleFormStateRepository(home)
    detail = _preview(status="submitted")
    repository.remember(TOKEN, detail, trim_expired=False)
    def forbidden(*args):
        raise AssertionError("Identical terminal detail must not rewrite state")
    monkeypatch.setattr(json_store.JsonStore, "_save_unlocked", forbidden)
    repository.remember(TOKEN, detail, trim_expired=False)
