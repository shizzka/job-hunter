"""Synthetic authoritative state tests; no browser, credentials or network."""
import concurrent.futures
import multiprocessing
import os
import stat

import pytest

from state_store.chat_responder import ChatResponderStateRepository as Repository


def _claim_worker(home):
    return Repository(home).claim("chat", "reply", "message")


def _increment_worker(args):
    home, index = args
    repo = Repository(home)
    repo.update_chat("chat", lambda chat: chat.update({f"field-{index}": index}))


def test_thread_claim_has_one_winner(tmp_path):
    with concurrent.futures.ThreadPoolExecutor(max_workers=30) as pool:
        owners = list(pool.map(lambda _: _claim_worker(tmp_path), range(30)))
    assert len([owner for owner in owners if owner]) == 1


def test_process_claim_has_one_winner(tmp_path):
    with concurrent.futures.ProcessPoolExecutor(max_workers=4,
            mp_context=multiprocessing.get_context("spawn")) as pool:
        owners = list(pool.map(_claim_worker, [str(tmp_path)] * 12))
    assert len([owner for owner in owners if owner]) == 1


def test_process_updates_preserve_unrelated_fields(tmp_path):
    with concurrent.futures.ProcessPoolExecutor(max_workers=4,
            mp_context=multiprocessing.get_context("spawn")) as pool:
        list(pool.map(_increment_worker, [(str(tmp_path), i) for i in range(40)]))
    assert Repository(tmp_path).load()["chat"] == {f"field-{i}": i for i in range(40)}


def test_owner_completion_preserves_other_chats_and_increments_once(tmp_path):
    repo = Repository(tmp_path)
    repo.save({"chat": {"replies_count": 4, "custom": "keep"}})
    owner = repo.claim("chat", "reply", "5", max_replies=5)
    repo.update_chat("other", lambda chat: chat.update(fresh=True))
    assert repo.finish("chat", owner, "completed")
    assert not repo.finish("chat", owner, "completed")
    state = repo.load()
    assert state["other"] == {"fresh": True}
    assert state["chat"]["replies_count"] == 5
    assert state["chat"]["custom"] == "keep"
    assert repo.claim("chat", "reply", "6", max_replies=5) is None
    assert repo.claim("chat", "reply", "5") is None


def test_old_message_cannot_be_replayed_after_new_reply(tmp_path):
    repo = Repository(tmp_path)
    for message in ("old", "new"):
        owner = repo.claim("chat", "reply", message)
        assert repo.finish("chat", owner, "completed")
    assert repo.claim("chat", "reply", "old") is None
    assert repo.claim("chat", "reply", "old", repeat=True) is None


@pytest.mark.parametrize("acting", [False, True])
def test_abandonment_outcome_and_retry_boundary(tmp_path, acting):
    repo = Repository(tmp_path)
    with pytest.raises(KeyboardInterrupt):
        with repo.attempt("chat", "reply", "message") as owner:
            if acting:
                repo.mark_acting("chat", owner)
            raise KeyboardInterrupt
    item = next(iter(repo.load()["chat"]["attempts"].values()))
    assert item["status"] == ("uncertain" if acting else "failed")
    assert bool(repo.claim("chat", "reply", "message")) is (not acting)
    if acting:
        assert repo.claim("chat", "reply", "other-message") is None


def test_no_auto_expiry_of_active_attempt(tmp_path, monkeypatch):
    repo = Repository(tmp_path)
    repo.claim("chat", "reply", "message")
    monkeypatch.setattr("state_store.chat_responder.time.time", lambda: 10**12)
    assert repo.claim("chat", "reply", "message") is None
    assert repo.claim("chat", "preview", "different") is None


def test_late_owner_cannot_finish_retry(tmp_path):
    repo = Repository(tmp_path)
    old = repo.claim("chat", "reply", "message")
    repo.finish("chat", old, "failed")
    new = repo.claim("chat", "reply", "message")
    assert new != old
    assert not repo.finish("chat", old, "completed", lambda chat: chat.update(lost=True))
    assert repo.finish("chat", new, "completed")
    assert "lost" not in repo.load()["chat"]
    assert repo.load()["chat"]["replies_count"] == 1


@pytest.mark.parametrize("contents", [b"{", b"\xff", b"[]", b'{"chat": []}',
    b'{"chat":{"replies_count":true}}', b'{"chat":{"replies_count":-1}}',
    b'{"chat":{"google_form_previews":[]}}', b'{"chat":{"attempts":[]}}',
    b'{"chat":{"last_reply_at":NaN}}', b'{"chat":{"last_replied_msg_id":2}}'])
def test_corruption_never_resets_state(tmp_path, contents):
    repo = Repository(tmp_path)
    repo.path.write_bytes(contents)
    for operation in (repo.load, lambda: repo.claim("chat", "reply", "message"),
                      lambda: repo.save({})):
        with pytest.raises(RuntimeError, match="Corrupt state"):
            operation()
        assert repo.path.read_bytes() == contents


@pytest.mark.parametrize("failure", ["replace", "serialize", "file_fsync"])
def test_failed_claim_does_not_replace_old_state(tmp_path, monkeypatch, failure):
    repo = Repository(tmp_path)
    repo.save({"chat": {"replies_count": 3}})
    before = repo.path.read_bytes()
    def fail(*args, **kwargs):
        raise OSError("synthetic persistence failure")
    target = {"replace": "os.replace", "serialize": "json.dump", "file_fsync": "os.fsync"}[failure]
    monkeypatch.setattr("state_store.json_store." + target, fail)
    with pytest.raises(OSError):
        repo.claim("chat", "reply", "message")
    assert repo.path.read_bytes() == before
    assert not list(tmp_path.glob("*.tmp"))


def test_directory_fsync_failure_leaves_sticky_complete_claim(tmp_path, monkeypatch):
    repo = Repository(tmp_path)
    original = os.fsync
    def fail_directory(fd):
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            raise OSError("synthetic directory fsync failure")
        return original(fd)
    with monkeypatch.context() as scoped:
        scoped.setattr("state_store.json_store.os.fsync", fail_directory)
        with pytest.raises(OSError):
            repo.claim("chat", "reply", "message")
    assert repo.claim("chat", "reply", "message") is None
    assert stat.S_IMODE(repo.path.stat().st_mode) == 0o600


def test_read_permission_error_preserves_file(tmp_path, monkeypatch):
    repo = Repository(tmp_path)
    repo.save({"chat": {"replies_count": 1}})
    before = repo.path.read_bytes()
    original = type(repo.path).open
    def denied(path, *args, **kwargs):
        if path == repo.path:
            raise PermissionError("synthetic read failure")
        return original(path, *args, **kwargs)
    with monkeypatch.context() as scoped:
        scoped.setattr(type(repo.path), "open", denied)
        with pytest.raises(PermissionError):
            repo.claim("chat", "reply", "message")
    assert repo.path.read_bytes() == before


def test_noop_finish_does_not_replace_state(tmp_path, monkeypatch):
    repo = Repository(tmp_path)
    owner = repo.claim("chat", "reply", "message")
    repo.finish("chat", owner, "completed")
    monkeypatch.setattr("state_store.json_store.os.replace", lambda *args: pytest.fail("No-op write"))
    assert not repo.finish("chat", owner, "completed")
    assert repo.claim("chat", "reply", "message") is None


def test_invalid_mutation_is_not_published(tmp_path):
    repo = Repository(tmp_path)
    repo.save({"chat": {"replies_count": 1}})
    before = repo.path.read_bytes()
    with pytest.raises(ValueError):
        repo.update_chat("chat", lambda chat: chat.update(replies_count=-1))
    assert repo.path.read_bytes() == before


def test_failed_result_cannot_make_acting_send_retryable(tmp_path):
    repo = Repository(tmp_path)
    owner = repo.claim("chat", "reply", "message")
    repo.mark_acting("chat", owner)
    assert repo.finish("chat", owner, "failed")
    assert repo.claim("chat", "reply", "message") is None


@pytest.mark.parametrize("limit", [True, 0, -1, "5"])
def test_invalid_reply_limit_cannot_bypass_cap(tmp_path, limit):
    with pytest.raises(ValueError):
        Repository(tmp_path).claim("chat", "reply", "message", max_replies=limit)
