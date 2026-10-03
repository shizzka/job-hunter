"""Synthetic bot-state races; no Telegram or production runtime access."""
import asyncio
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

import telegram_bot


@pytest.fixture
def bot(tmp_path, monkeypatch):
    monkeypatch.setattr(telegram_bot.config, "TELEGRAM_BOT_STATE_FILE", str(tmp_path / "bot.json"))
    return telegram_bot.TelegramBot(profile_name="synthetic")


def test_poll_response_keeps_changes_made_while_awaiting(bot, monkeypatch):
    bot._save_state({"last_update_id": 10})

    async def request(*args, **kwargs):
        bot._set_selected_profile(42, "synthetic-new")
        return [{"update_id": 11}]

    monkeypatch.setattr(bot, "_api_request", request)
    asyncio.run(bot._get_updates())
    state = bot._load_state()
    assert state["user_state"]["42"]["selected_profile"] == "synthetic-new"
    assert state["last_update_id"] == 11


def test_old_poll_response_cannot_move_offset_backwards(bot, monkeypatch):
    bot._save_state({"last_update_id": 10})

    async def request(*args, **kwargs):
        bot._save_state({"last_update_id": 30, "guest_state": {"42": {"step": "new"}}})
        return [{"update_id": 11}]

    monkeypatch.setattr(bot, "_api_request", request)
    asyncio.run(bot._get_updates())
    assert bot._load_state()["last_update_id"] == 30
    assert bot._guest_state(42)["step"] == "new"


def test_bootstrap_keeps_changes_made_during_request(bot, monkeypatch):
    async def get_updates(**kwargs):
        bot._set_guest_state(42, step="synthetic")
        return [{"update_id": 12}]

    monkeypatch.setattr(bot, "_get_updates", get_updates)
    asyncio.run(bot._bootstrap_offset())
    assert bot._guest_state(42)["step"] == "synthetic"
    assert bot._load_state()["last_update_id"] == 12


def test_health_diagnostics_do_not_overwrite_user_state(bot, monkeypatch):
    monkeypatch.setattr(telegram_bot.config, "TELEGRAM_HEALTH_CHECK_ENABLED", True)

    async def collect(profile):
        bot._set_search_settings_state(42, search_interval=45)
        return [{"name": "synthetic", "ok": True}]

    monkeypatch.setattr(bot, "_collect_diagnostics", collect)
    asyncio.run(bot._maybe_send_health_alert())
    assert bot._search_settings_state(42)["search_interval"] == 45
    assert bot._load_state()["health_check"]["last_failed_count"] == 0


def test_parallel_user_state_updates_are_not_lost(bot):
    barrier = Barrier(6)

    def worker(index):
        barrier.wait(timeout=10)
        for offset in range(5):
            user_id = 100 + index * 10 + offset
            bot._set_selected_profile(user_id, "synthetic")
            bot._set_selected_menu(user_id, "main")
            bot._set_guest_state(user_id, step="synthetic")

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(worker, range(6)))
    state = bot._load_state()
    assert len(state["user_state"]) == 30
    assert len(state["guest_state"]) == 30
    assert all(entry["selected_profile"] == "synthetic" and entry["menu"] == "main" for entry in state["user_state"].values())


@pytest.mark.parametrize("content", [b'{"broken":', b'[]', b'\xff', b'{"user_state":[]}'])
def test_corrupt_bot_state_is_preserved_and_blocks_mutations(bot, content):
    from pathlib import Path
    path = Path(bot.runtime_paths.bot_state_file)
    path.write_bytes(content)
    for _ in range(2):
        with pytest.raises(RuntimeError, match="[Cc]orrupt|[Rr]estore"):
            bot._set_selected_menu(42, "main")
        assert path.read_bytes() == content


def test_clearing_state_keeps_unrelated_users_and_fields(bot):
    bot._set_search_settings_state(42, selected_profile="synthetic", draft="old")
    bot._set_search_settings_state(43, draft="keep")
    bot._set_guest_state(42, step="remove")
    bot._set_guest_state(43, step="keep")
    bot._clear_search_settings_state(42, "draft")
    bot._clear_guest_state(42)
    assert bot._search_settings_state(42)["selected_profile"] == "synthetic"
    assert "draft" not in bot._search_settings_state(42)
    assert bot._search_settings_state(43)["draft"] == "keep"
    assert bot._guest_state(43)["step"] == "keep"
    assert bot._guest_state(42) == {}


def test_bot_state_replace_failure_keeps_previous_snapshot(bot, monkeypatch):
    from pathlib import Path
    from state_store import json_store
    bot._set_selected_profile(42, "synthetic")
    path = Path(bot.runtime_paths.bot_state_file)
    before = path.read_bytes()
    original = json_store.os.replace

    def fail(src, dst):
        if str(dst) == str(path):
            raise OSError("synthetic replace error")
        return original(src, dst)

    monkeypatch.setattr(json_store.os, "replace", fail)
    with pytest.raises(OSError):
        bot._set_selected_menu(42, "main")
    assert path.read_bytes() == before
    assert not list(path.parent.glob(f".{path.name}.*.tmp"))


def test_bot_state_read_permission_error_keeps_previous_snapshot(bot, monkeypatch):
    from pathlib import Path
    bot._set_selected_profile(42, "synthetic")
    path = Path(bot.runtime_paths.bot_state_file)
    before = path.read_bytes()
    original = Path.open

    def denied(self, *args, **kwargs):
        if self == path:
            raise PermissionError("synthetic read error")
        return original(self, *args, **kwargs)

    monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(PermissionError):
        bot._set_selected_menu(42, "main")
    with original(path, "rb") as stream:
        assert stream.read() == before


def test_bootstrap_does_not_replace_newer_offset(bot, monkeypatch):
    async def get_updates(**kwargs):
        bot._save_state({"last_update_id": 30})
        return [{"update_id": 12}]

    monkeypatch.setattr(bot, "_get_updates", get_updates)
    asyncio.run(bot._bootstrap_offset())
    assert bot._load_state()["last_update_id"] == 30


def test_runtime_normalization_keeps_new_agent_progress(bot, tmp_path, monkeypatch):
    from types import SimpleNamespace
    import agent
    from state_store.json_store import JsonStore

    path = tmp_path / "agent-runtime.json"
    monkeypatch.setattr(agent.config, "RUNTIME_STATUS_FILE", str(path))
    monkeypatch.setattr(bot, "_profile", lambda profile: SimpleNamespace(runtime_status_file=str(path)))
    monkeypatch.setattr(telegram_bot.runtime_control, "is_pid_running", lambda pid: False)
    JsonStore(path).save({"pid": 900000, "status": "working", "action": "old"})
    original = telegram_bot._normalize_process_runtime
    injected = False

    def normalize_after_agent_write(runtime, **kwargs):
        nonlocal injected
        normalized = original(runtime, **kwargs)
        if not injected:
            injected = True
            agent._write_runtime_status("new_progress", "Synthetic progress", "idle", "synthetic")
        return normalized

    monkeypatch.setattr(telegram_bot, "_normalize_process_runtime", normalize_after_agent_write)
    assert bot._runtime_status("synthetic")["action"] == "new_progress"
    assert JsonStore(path).load()["action"] == "new_progress"


def test_agent_runtime_writer_uses_shared_store_lock(tmp_path, monkeypatch):
    import agent
    from state_store.json_store import JsonStore, file_lock
    from threading import Event

    path = tmp_path / "agent-runtime.json"
    monkeypatch.setattr(agent.config, "RUNTIME_STATUS_FILE", str(path))
    started = Event()
    finished = Event()

    def writer():
        started.set()
        agent._write_runtime_status("synthetic", "Synthetic progress", "idle", "synthetic")
        finished.set()

    with ThreadPoolExecutor(max_workers=1) as pool:
        with file_lock(path):
            future = pool.submit(writer)
            assert started.wait(timeout=3)
            assert not finished.wait(timeout=0.1)
        future.result(timeout=3)
    assert JsonStore(path).load()["action"] == "synthetic"
