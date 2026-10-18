"""B1: HR messages in chats the employer already rejected go to one digest, not reply cards."""
import asyncio
import json

import hh_chat_responder as cr


def test_latest_topic_state_per_chat(tmp_path, monkeypatch):
    monkeypatch.setattr(cr, "os", cr.os)
    import config
    monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))
    rows = [{"chatId": 1, "lastState": "RESPONSE"}, {"kind": "snapshot_summary"}, {"chatId": 1, "lastState": "DISCARD"},
            {"chatId": 2, "lastState": "INVITATION"}]
    (tmp_path / "negotiation_snapshots.jsonl").write_text("\n".join(json.dumps(r) for r in rows))
    assert cr._latest_topic_states() == {"1": "DISCARD", "2": "INVITATION"}


def test_no_snapshot_changes_nothing(tmp_path, monkeypatch):
    import config
    monkeypatch.setattr(config, "JOB_HUNTER_HOME", str(tmp_path))
    assert cr._latest_topic_states() == {}


def test_digest_lists_rejections_without_buttons():
    sent = []

    class Notifier:
        async def send_message(self, text):
            sent.append(text)
            return True

    items = [{"title": f"QA {i}", "company": f"C{i}"} for i in range(22)]
    assert asyncio.run(cr._notify_rejections(Notifier(), items))
    assert "Отказы в чатах: 22" in sent[0] and "C0 — QA 0" in sent[0] and "…и ещё 2" in sent[0]

