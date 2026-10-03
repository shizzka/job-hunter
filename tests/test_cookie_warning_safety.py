"""Cookie alerts cannot escape offline tests or flood under contention."""
import asyncio
import json
import multiprocessing
import socket
from pathlib import Path

import pytest

import config
import notifier
from state_store.json_store import atomic_write_json


SOURCES = ("HH", "SUPERJOB", "HABR", "GEEKJOB")


def _configure(home):
    config.JOB_HUNTER_HOME = str(home)
    for source in SOURCES:
        setattr(config, source + "_ENABLED", source == "HH")
        setattr(config, source + "_COOKIES_FILE", str(Path(home) / (source + ".json")))


def _warning_worker(home, barrier, deliveries):
    _configure(home)

    async def send(text):
        with deliveries.get_lock():
            deliveries.value += 1
        await asyncio.sleep(0.05)
        return True

    notifier.send_message = send
    barrier.wait(timeout=10)
    asyncio.run(notifier.notify_stale_cookies())


@pytest.fixture
def warning_home(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))
    for source in SOURCES:
        monkeypatch.setattr(config, source + "_ENABLED", source == "HH")
        monkeypatch.setattr(config, source + "_COOKIES_FILE", str(tmp_path / (source + ".json")))
    return tmp_path


def test_parallel_async_warnings_send_once(warning_home, monkeypatch):
    sent = []

    async def send(text):
        sent.append(text)
        # A nested claim completes here; no file lock is held across await.
        await notifier.notify_stale_cookies()
        await asyncio.sleep(0)
        return True

    monkeypatch.setattr(notifier, "send_message", send)

    async def run():
        await asyncio.gather(*(notifier.notify_stale_cookies() for _ in range(30)))

    asyncio.run(run())
    assert len(sent) == 1
    assert "hh.ru" in sent[0]
    assert "SuperJob" not in sent[0]
    assert (warning_home / "cookie_warn_sent.json").stat().st_mode & 0o777 == 0o600


def test_parallel_process_warnings_send_once(warning_home):
    ctx = multiprocessing.get_context("spawn")
    barrier = ctx.Barrier(4)
    deliveries = ctx.Value("i", 0)
    processes = [ctx.Process(target=_warning_worker, args=(str(warning_home), barrier, deliveries)) for _ in range(4)]
    for process in processes:
        process.start()
    for process in processes:
        process.join(timeout=15)
        assert not process.is_alive()
        assert process.exitcode == 0
    assert deliveries.value == 1


@pytest.mark.parametrize("outcome", ["false", "error", "cancelled"])
def test_failed_or_cancelled_delivery_has_retry_cooldown(warning_home, monkeypatch, outcome):
    clock = [1_800_000_000.0]
    sent = []
    monkeypatch.setattr(notifier.time, "time", lambda: clock[0])

    async def send(text):
        sent.append(text)
        if outcome == "error":
            raise OSError("synthetic transport failure")
        if outcome == "cancelled":
            raise asyncio.CancelledError()
        return False

    monkeypatch.setattr(notifier, "send_message", send)
    for delta, expected in [(0, 1), (0, 1), (299, 1), (1, 2)]:
        clock[0] += delta
        try:
            asyncio.run(notifier.notify_stale_cookies())
        except (OSError, asyncio.CancelledError):
            pass
        assert len(sent) == expected
    state = json.loads((warning_home / "cookie_warn_sent.json").read_text())
    assert "sent_at" not in state


def test_successful_warning_retains_daily_cooldown_and_metadata(warning_home, monkeypatch):
    clock = [1_800_000_000.0]
    sent = []
    path = warning_home / "cookie_warn_sent.json"
    atomic_write_json(path, {"metadata": {"retained": True}})
    monkeypatch.setattr(notifier.time, "time", lambda: clock[0])

    async def send(text):
        sent.append(text)
        return True

    monkeypatch.setattr(notifier, "send_message", send)
    for delta, expected in [(0, 1), (86399, 1), (1, 2)]:
        clock[0] += delta
        asyncio.run(notifier.notify_stale_cookies())
        assert len(sent) == expected
    assert json.loads(path.read_text())["metadata"] == {"retained": True}


@pytest.mark.parametrize("content", [b'{', b'[]', b'\xff', b'{"sent_at":"bad"}', b'{"attempt_at":NaN}', b'{"attempt_id":5}'])
def test_invalid_warning_state_is_preserved_without_sending(warning_home, monkeypatch, content):
    path = warning_home / "cookie_warn_sent.json"
    path.write_bytes(content)

    async def send(text):
        pytest.fail("Corrupt cooldown state must not permit a send")

    monkeypatch.setattr(notifier, "send_message", send)
    for _ in range(2):
        asyncio.run(notifier.notify_stale_cookies())
        assert path.read_bytes() == content


def test_failed_claim_write_prevents_send(warning_home, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError("synthetic persistence failure")

    async def send(text):
        pytest.fail("Cannot send without a persisted claim")

    monkeypatch.setattr(notifier.ProtectedJsonStore, "update", fail)
    monkeypatch.setattr(notifier, "send_message", send)
    asyncio.run(notifier.notify_stale_cookies())


def test_failed_completion_write_keeps_retry_cooldown(warning_home, monkeypatch):
    sent = []
    original_update = notifier.ProtectedJsonStore.update
    calls = [0]

    def update(self, mutator):
        calls[0] += 1
        if calls[0] == 2:
            raise OSError("synthetic completion failure")
        return original_update(self, mutator)

    async def send(text):
        sent.append(text)
        return True

    monkeypatch.setattr(notifier.ProtectedJsonStore, "update", update)
    monkeypatch.setattr(notifier, "send_message", send)
    asyncio.run(notifier.notify_stale_cookies())
    asyncio.run(notifier.notify_stale_cookies())
    assert len(sent) == 1


def test_warning_completion_uses_original_profile_home(warning_home, monkeypatch):
    other = warning_home / "other-profile"

    async def send(text):
        monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(other))
        return True

    monkeypatch.setattr(notifier, "send_message", send)
    asyncio.run(notifier.notify_stale_cookies())
    assert json.loads((warning_home / "cookie_warn_sent.json").read_text())["sent_at"]
    assert not (other / "cookie_warn_sent.json").exists()


def test_disabled_sources_are_not_checked(warning_home, monkeypatch):
    for source in SOURCES:
        monkeypatch.setattr(config, source + "_ENABLED", False)

    def unexpected(path):
        pytest.fail("Disabled sources must not inspect cookie files")

    monkeypatch.setattr(notifier.os.path, "exists", unexpected)
    asyncio.run(notifier.notify_stale_cookies())
    assert not (warning_home / "cookie_warn_sent.json").exists()


def test_old_attempt_cannot_overwrite_new_attempt(warning_home, monkeypatch):
    clock = [1_800_000_000.0]
    path = warning_home / "cookie_warn_sent.json"
    sent = []
    monkeypatch.setattr(notifier.time, "time", lambda: clock[0])

    async def send(text):
        sent.append(text)
        if len(sent) == 1:
            clock[0] += notifier._COOKIE_WARN_RETRY_INTERVAL
            await notifier.notify_stale_cookies()
            later = json.loads(path.read_text())
            later["stale"] = ["new attempt result"]
            atomic_write_json(path, later)
        return True

    monkeypatch.setattr(notifier, "send_message", send)
    asyncio.run(notifier.notify_stale_cookies())
    assert len(sent) == 2
    assert json.loads(path.read_text())["stale"] == ["new attempt result"]


@pytest.mark.parametrize("method", ["connect", "connect_ex"])
@pytest.mark.parametrize("family", [socket.AF_INET, socket.AF_INET6])
def test_ordinary_tests_cannot_open_ip_connections(method, family):
    address = ("127.0.0.1", 9) if family == socket.AF_INET else ("::1", 9)
    with socket.socket(family, socket.SOCK_STREAM) as sock:
        with pytest.raises(AssertionError, match="network connections"):
            getattr(sock, method)(address)
