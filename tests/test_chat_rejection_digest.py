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
    assert "Отвечать на них не нужно" in sent[0]



import pytest


@pytest.mark.parametrize("text,kind", [
    ("Кандидат, благодарю вас за уделённое время и ответы на вопросы. Мы обязательно рассмотрим ваше резюме и "
     "результаты диалога в рамках процесса подбора. При положительном решении свяжемся с вами.", "waiting"),
    ("Добрый день! В настоящий момент мы не готовы пригласить вас к дальнейшему рассмотрению.", "rejection"),
    ("К сожалению, сейчас мы не готовы пригласить вас на следующий этап.", "rejection"),
    ("К сожалению, вакансия уже закрыта.", "rejection"),
    ("Когда готовы приступить?", ""),
    ("Какие у вас зарплатные ожидания? При положительном решении свяжемся.", ""),  # a question stays a card
])
def test_closing_messages_seen_live(text, kind):
    assert cr._closing_kind(text) == kind


def test_digest_has_both_sections():
    sent = []

    class Notifier:
        async def send_message(self, text):
            sent.append(text)
            return True

    asyncio.run(cr._notify_rejections(Notifier(), [{"title": "QA", "company": "Acme"}],
                                      [{"title": "Тестировщик", "company": "Crowd"}]))
    assert "Отказы в чатах: 1" in sent[0] and "Acme — QA" in sent[0]
    assert "Ответят сами" in sent[0] and "Crowd — Тестировщик" in sent[0]
