"""Synthetic application metadata for chat tests unrelated to owner resolution."""
import json
from pathlib import Path

import config


def owned_chats(monkeypatch, home, chat_ids):
    home = Path(home)
    monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(home))
    monkeypatch.setattr(config, "ACTIVE_DIRECTION", {"name": "synthetic"}, raising=False)
    directory = home / "directions"
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "synthetic.json").write_text(json.dumps({"name": "synthetic", "variants": [
        {"hh_resume_numeric_id": "111", "title": "Synthetic resume"}]}))
    with (home / "negotiation_snapshots.jsonl").open("a") as stream:
        for chat_id in chat_ids:
            stream.write(json.dumps({"chatId": chat_id, "resumeId": "111"}) + "\n")


def owned_telegram_chats(home, chat_ids):
    """The controller also loads the active direction before launching a child."""
    import directions
    from tests.test_telegram_directions import make_direction
    make_direction(str(home), "synthetic")
    directions.set_active_direction(str(home), "synthetic")
    with (Path(home) / "negotiation_snapshots.jsonl").open("a") as stream:
        for chat_id in chat_ids:
            stream.write(json.dumps({"chatId": chat_id, "resumeId": "1"}) + "\n")
