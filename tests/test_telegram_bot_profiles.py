import json
import logging

import telegram_bot
from runtime_context import TelegramRuntimePaths
from telegram_bot import ROLE_USER, TelegramBot


def test_bot_state_and_debug_files_use_explicit_runtime_snapshot(tmp_path, monkeypatch):
    paths = TelegramRuntimePaths(
        bot_pid_file=str(tmp_path / "profile-a" / "bot.pid"),
        bot_state_file=str(tmp_path / "profile-a" / "bot-state.json"),
        bot_runtime_file=str(tmp_path / "profile-a" / "bot-runtime.json"),
        bot_log_file=str(tmp_path / "profile-a" / "bot.log"),
        bot_debug_log_file=str(tmp_path / "profile-a" / "bot-debug.jsonl"),
    )
    bot = TelegramBot(profile_name="qa", runtime_paths=paths)
    monkeypatch.setattr(
        telegram_bot.config,
        "TELEGRAM_BOT_STATE_FILE",
        str(tmp_path / "profile-b" / "bot-state.json"),
    )

    bot._save_state({"last_update_id": 42})
    bot._write_runtime("test", "Testing", "idle")
    bot._append_debug_log("test_event", value=7)
    described_paths = []

    def fake_describe_process(path, **kwargs):
        described_paths.append(path)
        return {"running": False}

    monkeypatch.setattr(telegram_bot.runtime_control, "describe_process", fake_describe_process)

    assert bot._load_state() == {"last_update_id": 42}
    assert telegram_bot.runtime_control.read_json_file(paths.bot_runtime_file)["action"] == "test"
    debug_record = json.loads(
        (tmp_path / "profile-a" / "bot-debug.jsonl").read_text(encoding="utf-8")
    )
    assert debug_record["event"] == "test_event"
    assert debug_record["value"] == 7
    assert bot._bot_state() == {"running": False}
    assert described_paths == [paths.bot_pid_file]
    assert not (tmp_path / "profile-b" / "bot-state.json").exists()


def test_logging_handlers_use_explicit_runtime_snapshot(tmp_path, monkeypatch):
    paths = TelegramRuntimePaths(
        bot_pid_file=str(tmp_path / "profile-a" / "bot.pid"),
        bot_state_file=str(tmp_path / "profile-a" / "bot-state.json"),
        bot_runtime_file=str(tmp_path / "profile-a" / "bot-runtime.json"),
        bot_log_file=str(tmp_path / "profile-a" / "bot.log"),
        bot_debug_log_file=str(tmp_path / "profile-a" / "bot-debug.jsonl"),
    )
    monkeypatch.setattr(
        telegram_bot.config,
        "TELEGRAM_BOT_LOG_FILE",
        str(tmp_path / "profile-b" / "bot.log"),
    )

    handlers = telegram_bot._build_logging_handlers(paths)
    try:
        file_handlers = [handler for handler in handlers if isinstance(handler, logging.FileHandler)]
        assert [handler.baseFilename for handler in file_handlers] == [paths.bot_log_file]
    finally:
        for handler in handlers:
            handler.close()


def test_selected_profile_for_user_prefers_client_profile(monkeypatch):
    bot = TelegramBot(profile_name="default")
    principal = {"user_id": 42, "role": ROLE_USER, "profile": "qa"}

    monkeypatch.setattr(bot, "_default_profile_name", lambda: "qa")
    monkeypatch.setattr(bot, "_profile_names", lambda: ["qa", "client_42"])
    monkeypatch.setattr(bot, "_client_record", lambda user_id: {"profile_name": "client_42"})

    assert bot._selected_profile(principal) == "client_42"


def test_selected_profile_for_user_falls_back_to_access_profile(monkeypatch):
    bot = TelegramBot(profile_name="default")
    principal = {"user_id": 42, "role": ROLE_USER, "profile": "qa"}

    monkeypatch.setattr(bot, "_default_profile_name", lambda: "qa")
    monkeypatch.setattr(bot, "_profile_names", lambda: ["qa"])
    monkeypatch.setattr(bot, "_client_record", lambda user_id: {"profile_name": "client_42"})

    assert bot._selected_profile(principal) == "qa"
