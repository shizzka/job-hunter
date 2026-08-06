import asyncio

import pytest

from commands import google_forms
import google_form_filler as gforms


class FakeClient:
    instances = []

    def __init__(self):
        self.stopped = False
        self.__class__.instances.append(self)

    async def stop(self):
        self.stopped = True


def test_preview_preserves_summary_and_stops_client(monkeypatch, capsys):
    async def fake_preview(client, chat_id, **kwargs):
        assert chat_id == "42"
        assert kwargs == {"message_id": "7", "profile_name": "qa", "notify": True}
        return {
            "ok": True,
            "message": "preview",
            "chat_id": "42",
            "token": "abc",
            "questions": [{"index": 0}],
            "fill_result": {"filled": [{"index": 0}], "skipped": []},
        }

    FakeClient.instances = []
    monkeypatch.setattr(google_forms, "HHClient", FakeClient)
    monkeypatch.setattr(gforms, "preview_from_hh_chat", fake_preview)

    asyncio.run(google_forms.preview("42", message_id="7", profile_name="qa"))

    assert capsys.readouterr().out == (
        "📋 Google Form preview summary:\n"
        "  OK: True\n"
        "  Сообщение: preview\n"
        "  Чат: 42\n"
        "  Токен: abc\n"
        "  Вопросов: 1\n"
        "  Заполнено: 1 | пропущено: 0\n"
    )
    assert FakeClient.instances[0].stopped is True


def test_submit_preserves_failure_exit_and_stops_client(monkeypatch, capsys):
    async def fake_submit(client, token, **kwargs):
        assert token == "bad-token"
        assert kwargs == {"notify": True}
        return {"ok": False, "message": "not found"}

    FakeClient.instances = []
    monkeypatch.setattr(google_forms, "HHClient", FakeClient)
    monkeypatch.setattr(gforms, "submit_saved_preview", fake_submit)

    with pytest.raises(SystemExit) as exc_info:
        asyncio.run(google_forms.submit("bad-token"))

    assert exc_info.value.code == 1
    assert capsys.readouterr().out == (
        "📋 Google Form submit summary:\n"
        "  OK: False\n"
        "  Сообщение: not found\n"
        "  Токен: bad-token\n"
    )
    assert FakeClient.instances[0].stopped is True
