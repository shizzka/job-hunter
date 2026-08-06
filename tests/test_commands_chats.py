import asyncio

import pytest

from commands import chats
import hh_chat_responder as chat_responder


class FakeClient:
    instances = []

    def __init__(self):
        self.stopped = False
        self.__class__.instances.append(self)

    async def stop(self):
        self.stopped = True


def _install_fake_client(monkeypatch):
    FakeClient.instances = []
    monkeypatch.setattr(chats, "HHClient", FakeClient)


def test_respond_all_preserves_summary(monkeypatch, capsys):
    async def fake_process_all(client):
        return {
            "chats_scanned": 3,
            "with_ai": 2,
            "suspicious": 1,
            "suspicious_notified": 1,
            "google_forms_found": 1,
            "google_forms_prepared": 1,
            "google_forms_failed": 0,
            "answers_drafted": 2,
            "answers_sent": 0,
            "skipped": 1,
            "read_failures": 0,
            "details": [],
        }

    _install_fake_client(monkeypatch)
    monkeypatch.setattr(chat_responder, "process_all", fake_process_all)

    asyncio.run(chats.respond_all())

    assert capsys.readouterr().out == (
        "📋 Chat-respond summary:\n"
        "  Чатов проверено: 3\n"
        "  С AI-помощником: 2\n"
        "  Подозрительных HR-сообщений: 1\n"
        "  Уведомлений на подтверждение: 1\n"
        "  Google Forms: найдено 1 | preview 1 | ошибок 0\n"
        "  Подготовлено ответов: 2\n"
        "  Отправлено: 0\n"
        "  Пропущено: 1\n"
        "  Ошибок чтения: 0\n"
    )
    assert FakeClient.instances[0].stopped is True


def test_list_candidates_clamps_limits_and_preserves_json(monkeypatch, capsys):
    async def fake_list(client, **kwargs):
        assert kwargs == {"limit": 1, "max_scan": 1}
        return [{"chat_id": "42", "vacancy": "Тестировщик"}]

    _install_fake_client(monkeypatch)
    monkeypatch.setattr(chat_responder, "list_reply_candidates", fake_list)

    asyncio.run(chats.list_candidates(limit=0, max_scan=-2))

    assert capsys.readouterr().out == (
        '{"chat_candidates": [{"chat_id": "42", "vacancy": "Тестировщик"}]}\n'
    )
    assert FakeClient.instances[0].stopped is True


def test_respond_one_preserves_options_and_preview(monkeypatch, capsys):
    async def fake_process_one(client, chat_id, **kwargs):
        assert chat_id == "42"
        assert kwargs == {
            "message_id": "7",
            "allow_suspicious": True,
            "allow_any": False,
            "dry_run": True,
            "notify": True,
        }
        return {
            "ok": True,
            "chat_id": "42",
            "message": "preview",
            "question": "Вопрос",
            "answer": "Ответ",
            "dry_run": True,
            "preview": {"screenshot_path": "/tmp/chat.png"},
        }

    _install_fake_client(monkeypatch)
    monkeypatch.setattr(chat_responder, "process_one", fake_process_one)

    asyncio.run(
        chats.respond_one(
            "42",
            message_id="7",
            allow_suspicious=True,
            allow_any=False,
            force_send=False,
        )
    )

    assert capsys.readouterr().out == (
        "📋 Chat one-shot summary:\n"
        "  Чат: 42\n"
        "  OK: True\n"
        "  Сообщение: preview\n"
        "  Вопрос: Вопрос\n"
        "  Ответ: Ответ\n"
        "  DRY-RUN скрин: /tmp/chat.png\n"
    )
    assert FakeClient.instances[0].stopped is True


def test_respond_one_stops_client_when_responder_fails(monkeypatch):
    async def fake_process_one(client, chat_id, **kwargs):
        raise RuntimeError("boom")

    _install_fake_client(monkeypatch)
    monkeypatch.setattr(chat_responder, "process_one", fake_process_one)

    with pytest.raises(RuntimeError, match="boom"):
        asyncio.run(
            chats.respond_one(
                "42",
                message_id="",
                allow_suspicious=False,
                allow_any=False,
                force_send=False,
            )
        )

    assert FakeClient.instances[0].stopped is True
