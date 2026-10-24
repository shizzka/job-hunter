"""B41 critic: authorize real tester recipients before mocking Telegram I/O."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import agent
import config
import directions
import manual_apply_queue
import notifier
import telegram_access
from tests.test_agent_manual_apply import FakeHHClient
from tests.test_agent_trace_apply import FakePage, FakeTrace
from tests.test_b7_critic_regressions import scoped_bot


@pytest.mark.parametrize("event,expected", [
    ({"_direction_selection": {"direction": "qa", "hh_resume_hash": "resume_111"}}, [202]),
    ({"direction": "electric"}, [303]),
    ({}, []),
    ({"direction": "qa", "hh_resume_hash": "resume_222"}, []),
])
@pytest.mark.parametrize("active", ["qa", "electric"])
def test_stub_review_reaches_only_testers_owning_the_event(scoped_bot, monkeypatch, event, expected, active):
    _, _, profile = scoped_bot
    telegram_access.upsert_user(303, profile=profile.name, role="tester", direction="electric")
    directions.set_active_direction(profile.home_dir, active)
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "synthetic-token")
    transport = AsyncMock(return_value=True)
    monkeypatch.setattr(notifier, "_deliver", transport)
    vacancy = {"id": "123", "title": "Пример", "company": "Пример", **event}

    delivered = asyncio.run(agent._notify_stub_cover(vacancy, "Шаблон", "token", profile_name=profile.name))

    assert delivered is bool(expected)
    assert [call.args[1]["chat_id"] for call in transport.await_args_list] == expected
    if expected:
        buttons = transport.await_args.args[1]["reply_markup"]["inline_keyboard"][0]
        assert [b["text"] for b in buttons] == ["🔄 Перегенерировать", "📨 Отправить шаблон"]
        assert buttons[1]["callback_data"] == f"stub:{profile.name}:token"
    # Ownership must not leak to the next unbound notification in this task.
    assert notifier._delivery_direction.get() is None


@pytest.mark.parametrize("mode", ["manual", "trace"])
@pytest.mark.parametrize("delivery", [True, False, "transport_error", "disabled_recipient"])
def test_apply_reports_review_only_after_real_authorized_delivery(
        scoped_bot, tmp_path, monkeypatch, capsys, mode, delivery):
    _, _, profile = scoped_bot
    monkeypatch.setattr(config, "HH_APPLICATION_MODE", "auto")
    monkeypatch.setattr(config, "TELEGRAM_BOT_TOKEN", "synthetic-token")
    monkeypatch.setattr(manual_apply_queue, "_queue_path", lambda profile_name=None: tmp_path / "queue.json")
    monkeypatch.setattr(agent.hh_guard, "can_auto_apply", lambda: (True, ""))
    # Keep the generator's real direction binding; only its text is synthetic.
    monkeypatch.setattr(agent, "generate_cover_letter", AsyncMock(
        side_effect=lambda vacancy, *a: agent.direction_runtime.bind_cover(vacancy, "Непроверенное письмо.")))
    dispatch = AsyncMock()
    monkeypatch.setattr(agent.apply_orchestrator, "dispatch_apply", dispatch)
    transport = AsyncMock(return_value=delivery is True)
    if delivery == "transport_error":
        transport.side_effect = OSError("synthetic transport failure")
    if delivery == "disabled_recipient":
        telegram_access.upsert_user(202, profile=profile.name, role="tester", direction="qa", enabled=False)
    monkeypatch.setattr(notifier, "_deliver", transport)

    if mode == "manual":
        monkeypatch.setattr(agent, "HHClient", FakeHHClient)
        monkeypatch.setattr(agent.apply_orchestrator, "fetch_vacancy_details", AsyncMock(return_value="Описание"))
        vacancy = {"id": "123", "source": "hh", "title": "Пример", "company": "Пример"}
        agent.direction_runtime.prepare(vacancy)
        candidate = manual_apply_queue.create_candidate(vacancy, {"score": 60}, "Описание", profile_name=profile.name)
        result = asyncio.run(agent.do_manual_apply_token(candidate["token"]))
    else:
        trace = FakeTrace()
        page = FakePage()
        page.content = AsyncMock(return_value="")
        client = SimpleNamespace(_page=page, start=AsyncMock(), stop=AsyncMock(),
            is_logged_in=AsyncMock(return_value=True), get_vacancy_details=AsyncMock(return_value="Описание"))
        monkeypatch.setattr(agent, "HHClient", lambda: client)
        monkeypatch.setattr(agent.apply_orchestrator, "create_hh_apply_trace", lambda *a, **k: trace)
        result = asyncio.run(agent.do_trace_apply("123", confirm_real=True))
        candidate = next(iter(manual_apply_queue._store().load()["items"].values()))
        client.stop.assert_awaited_once()

    dispatch.assert_not_awaited()
    assert result["ok"] is False
    assert result["stub_review"] is (delivery is True)
    assert manual_apply_queue.get_candidate(candidate["token"])["status"] == "pending"
    if delivery == "disabled_recipient":
        transport.assert_not_awaited()
    else:
        transport.assert_awaited_once()
        assert transport.await_args.args[1]["chat_id"] == 202
    if delivery is not True:
        assert result["notification_failed"] is True
        assert "Не удалось доставить кнопки" in result["message"]
        assert "Вакансии на проверку" in capsys.readouterr().out
