"""Fake polling only: no Telegram credentials, sockets or second poller."""
import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

import pytest

import telegram_bot
from runtime_context import TelegramRuntimePaths
from telegram_app.api import TelegramAPIError, TelegramAPIClient

SENSITIVE = "synthetic-sensitive-marker"


@pytest.fixture
def bot(tmp_path, monkeypatch):
    paths = TelegramRuntimePaths(*(str(tmp_path / name) for name in
        ("bot.pid", "state.json", "runtime.json", "bot.log", "debug.jsonl")))
    monkeypatch.setattr(telegram_bot.config, "TELEGRAM_CONTROL_BOT_TOKEN", "synthetic")
    monkeypatch.setattr(telegram_bot.telegram_access, "load_registry", lambda: {})
    monkeypatch.setattr(telegram_bot.runtime_control, "register_current_process", lambda *a, **k: 1)
    monkeypatch.setattr(telegram_bot.runtime_control, "unregister_current_process", lambda *a: None)
    return telegram_bot.TelegramBot("synthetic", drop_pending=False, runtime_paths=paths)


def runtime(bot):
    return json.loads(Path(bot.runtime_paths.bot_runtime_file).read_text())


def run_polls(bot, monkeypatch, outcomes, *, on_update=None):
    remaining = list(outcomes)
    observations = []
    handled = []

    async def poll():
        outcome = remaining.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    async def pause(seconds):
        assert seconds == 5
        observations.append(runtime(bot))
        if not remaining:
            bot._stop_event.set()

    async def handle(update):
        handled.append(update)
        if on_update:
            on_update(update)

    async def daily():
        pass

    async def health():
        observations.append(runtime(bot))
        if not remaining:
            bot._stop_event.set()

    monkeypatch.setattr(bot, "_get_updates", poll)
    monkeypatch.setattr(bot, "_handle_update", handle)
    monkeypatch.setattr(bot, "_maybe_send_daily_summary", daily)
    monkeypatch.setattr(bot, "_maybe_send_health_alert", health)
    monkeypatch.setattr(telegram_bot.asyncio, "sleep", pause)

    async def scenario():
        monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", lambda *a: None)
        await bot.run()

    asyncio.run(scenario())
    return observations, handled


def test_failed_poll_recovery_clears_error(bot, monkeypatch):
    observations, _ = run_polls(bot, monkeypatch, [OSError("temporary transport failure"), []])
    assert observations[0]["status"] == "error"
    assert observations[1]["status"] == "idle"


def test_poll_failure_diagnostics_exclude_raw_transport_payload(bot, monkeypatch, caplog):
    with caplog.at_level(logging.ERROR):
        observations, _ = run_polls(bot, monkeypatch, [OSError(f"https://example.invalid/bot{SENSITIVE}/getUpdates")])
    assert SENSITIVE not in json.dumps(observations)
    assert SENSITIVE not in caplog.text
    assert "OSError" in observations[0]["message"]


def test_poll_recovery_retains_busy(bot, monkeypatch):
    bot._mark_active_command(principal={"user_id": 1}, label="synthetic", profile_name="synthetic")
    observations, _ = run_polls(bot, monkeypatch, [OSError("temporary"), []])
    assert observations[-1]["status"] == "busy"
    assert observations[-1]["action"] == "command_start"


def test_first_success_records_actual_poll(bot, monkeypatch):
    observations, _ = run_polls(bot, monkeypatch, [[]])
    assert observations[0].get("polling", {}).get("status") == "ok"
    assert observations[0]["polling"]["last_success_at"]


def test_start_is_not_poll_success_and_stop_is_offline(bot):
    bot._write_runtime("bot_start", "starting", "idle")
    assert runtime(bot)["polling"] == {"status": "starting"}


def test_successful_stop_retains_last_success_but_not_healthy_status(bot, monkeypatch):
    observations, _ = run_polls(bot, monkeypatch, [[]])
    final = runtime(bot)
    assert final["status"] == "offline"
    assert final["polling"]["status"] == "stopped"
    assert final["polling"]["last_success_at"] == observations[0]["polling"]["last_success_at"]


def test_poll_heartbeat_throttles_success_not_recovery(bot, monkeypatch):
    clock = [100.0]
    monkeypatch.setattr(telegram_bot, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    writes = []
    original = bot._write_runtime

    def write(*args):
        writes.append(args)
        original(*args)

    monkeypatch.setattr(bot, "_write_runtime", write)
    bot._record_poll_result()
    for step in range(1, 30):
        clock[0] = 100.0 + step
        bot._record_poll_result()
    assert len(writes) == 1
    clock[0] = 130
    bot._record_poll_result()
    assert len(writes) == 2
    clock[0] = 131
    bot._record_poll_result(OSError("temporary"))
    bot._record_poll_result()
    assert len(writes) == 4
    assert runtime(bot)["polling"]["status"] == "ok"


def test_error_retains_last_success_and_success_retains_safe_history(bot):
    bot._record_poll_result()
    first = runtime(bot)["polling"]
    bot._record_poll_result(TelegramAPIError("getUpdates", 401, {"description": SENSITIVE}))
    failed = runtime(bot)["polling"]
    assert failed["last_success_at"] == first["last_success_at"]
    assert failed["last_error_status"] == 401
    assert SENSITIVE not in Path(bot.runtime_paths.bot_runtime_file).read_text()
    bot._record_poll_result()
    recovered = runtime(bot)["polling"]
    assert recovered["status"] == "ok"
    assert recovered["last_error_at"] == failed["last_error_at"]
    assert recovered["last_error_type"] == "TelegramAPIError"


def test_transport_error_does_not_retain_old_http_status(bot):
    bot._record_poll_result(TelegramAPIError("getUpdates", 429, {"parameters": {"retry_after": 5}}))
    bot._record_poll_result(OSError("temporary"))
    assert runtime(bot)["polling"]["last_error_status"] is None


@pytest.mark.parametrize("error", [OSError(SENSITIVE), ValueError(SENSITIVE)])
def test_diagnostic_failure_does_not_discard_fetched_updates(bot, monkeypatch, caplog, error):
    original = bot._write_runtime

    def write(action, *args):
        if action == "command_done":
            raise error
        original(action, *args)

    monkeypatch.setattr(bot, "_write_runtime", write)
    with caplog.at_level(logging.WARNING):
        _, handled = run_polls(bot, monkeypatch, [[{"update_id": 1}]])
    assert handled == [{"update_id": 1}]
    assert SENSITIVE not in caplog.text


def test_failed_diagnostic_publish_is_retried_and_does_not_fake_success(bot, monkeypatch):
    original = bot._sync_active_runtime
    monkeypatch.setattr(bot, "_sync_active_runtime", lambda: (_ for _ in ()).throw(OSError("disk")))
    bot._record_poll_result()
    assert bot._poll_health["status"] == "starting"
    assert bot._last_poll_runtime_at is None
    monkeypatch.setattr(bot, "_sync_active_runtime", original)
    bot._record_poll_result()
    assert runtime(bot)["polling"]["status"] == "ok"


def test_cancellation_does_not_claim_poll_success(bot, monkeypatch):
    with pytest.raises(asyncio.CancelledError):
        run_polls(bot, monkeypatch, [asyncio.CancelledError()])
    final = runtime(bot)
    assert final["polling"]["status"] == "stopped"
    assert "last_success_at" not in final["polling"]


def test_stopped_owner_cannot_record_more_poll_results(bot):
    bot._stop_event.set()
    bot._record_poll_result()
    bot._record_poll_result(OSError("closed"))
    assert not Path(bot.runtime_paths.bot_runtime_file).exists()


def test_runtime_remains_private_and_captured_on_profile_change(bot, monkeypatch, tmp_path):
    other = tmp_path / "other-runtime.json"
    monkeypatch.setattr(telegram_bot.config, "TELEGRAM_BOT_RUNTIME_FILE", str(other))
    bot._record_poll_result()
    assert not other.exists()
    assert Path(bot.runtime_paths.bot_runtime_file).stat().st_mode & 0o777 == 0o600
    assert runtime(bot)["profile"] == "synthetic"


def test_recovery_logs_once_per_transition_and_has_no_notification(bot, monkeypatch, caplog):
    async def forbidden(*a, **k):
        raise AssertionError("No recovery notification authorized")
    monkeypatch.setattr(bot, "_send_text", forbidden)
    with caplog.at_level(logging.INFO):
        bot._record_poll_result(OSError("temporary"))
        bot._record_poll_result()
        bot._record_poll_result()
    assert sum(record.message == "Telegram polling recovered" for record in caplog.records) == 1


@pytest.mark.parametrize("error", [OSError(SENSITIVE), TelegramAPIError("getMe", 401, {"description": SENSITIVE})])
def test_health_getme_diagnostics_do_not_expose_raw_exception(bot, monkeypatch, tmp_path, error):
    monkeypatch.setattr(bot, "_profile", lambda name: SimpleNamespace(hh=SimpleNamespace(cookies_file=str(tmp_path / "missing-cookies"))))
    monkeypatch.setattr(bot, "_profile_schedule", lambda name: (30, 30))
    monkeypatch.setattr(bot, "_daemon_state", lambda name: {})
    monkeypatch.setattr(bot, "_bot_state", lambda: {"running": True, "pid": 1})
    monkeypatch.setattr(bot, "_profile_recipient_ids", lambda name: [])
    monkeypatch.setattr(bot, "_log_tail", lambda *a, **k: ("", ""))
    monkeypatch.setattr(bot, "_latest_run", lambda name: {})

    async def request(*a, **k):
        raise error

    monkeypatch.setattr(bot, "_api_request", request)
    checks = asyncio.run(bot._collect_diagnostics("synthetic"))
    api = next(check for check in checks if check["name"] == "Telegram API")
    assert api["ok"] is False
    assert type(error).__name__ in api["detail"]
    assert SENSITIVE not in json.dumps(checks)


def test_failure_diagnostic_write_error_does_not_stop_retry_loop(bot, monkeypatch, caplog):
    original = bot._write_runtime

    def write(action, *args):
        if action == "bot_poll_error":
            raise OSError(SENSITIVE)
        original(action, *args)

    monkeypatch.setattr(bot, "_write_runtime", write)
    with caplog.at_level(logging.WARNING):
        observations, _ = run_polls(bot, monkeypatch, [OSError("network"), []])
    assert observations[-1]["polling"]["status"] == "ok"
    assert SENSITIVE not in caplog.text


def test_post_publication_failure_cannot_throttle_immediate_recovery(bot, monkeypatch):
    monkeypatch.setattr(telegram_bot, "time", SimpleNamespace(monotonic=lambda: 100.0))
    bot._record_poll_result()
    original = bot._write_runtime

    def write(action, *args):
        original(action, *args)
        if action == "bot_poll_error":
            raise OSError("directory fsync failed after replacement")

    monkeypatch.setattr(bot, "_write_runtime", write)
    bot._record_poll_result(OSError("network"))
    assert runtime(bot)["status"] == "error"
    bot._record_poll_result()
    assert runtime(bot)["status"] == "idle"
    assert runtime(bot)["polling"]["status"] == "ok"


@pytest.mark.parametrize("operation", ["api", "download", "document"])
def test_proxy_fallback_log_excludes_raw_sensitive_exception(monkeypatch, caplog, operation):
    monkeypatch.setattr(telegram_bot.config, "TELEGRAM_PROXY", "socks5://127.0.0.1:1080")
    client = TelegramAPIClient()

    async def request(*args, use_proxy, **kwargs):
        if use_proxy:
            raise OSError(SENSITIVE)
        return {"result": True}

    if operation == "api":
        monkeypatch.setattr(client, "_api_request_once", request)
        call = client._api_request("getUpdates", {})
    elif operation == "document":
        monkeypatch.setattr(client, "_send_document_once", request)
        call = client._send_document(1, filename="synthetic", content=b"synthetic")
    else:
        async def meta(*a, **k):
            return {"file_path": "synthetic"}
        monkeypatch.setattr(client, "_api_request", meta)
        monkeypatch.setattr(client, "_download_file_once", request)
        call = client._download_file("synthetic")
    with caplog.at_level(logging.WARNING):
        assert asyncio.run(call) == {"result": True}
    assert "OSError" in caplog.text
    assert SENSITIVE not in caplog.text
