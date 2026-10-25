"""F27 navigation keeps local actions inline and global destinations persistent."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
import telegram_bot_ui as ui
from tests.test_telegram_menu_routing import menu_bot
from telegram_app import history
from telegram_app.callback_handlers import CallbackContext, navigation

GLOBAL = ["🔎 Найти вакансии", "📋 Ждут решения", "📨 Отклики", "⚙️ Настройки"]

@pytest.mark.parametrize("role", [ui.ROLE_USER, ui.ROLE_ADMIN])
def test_every_screen_keeps_exactly_four_global_reply_buttons(role):
    for menu in ui.reply_menu_layouts(role):
        markup = ui.build_reply_markup(role, menu=menu, active=True, can_stop=True)
        assert [b["text"] for row in markup["keyboard"] for b in row] == GLOBAL
        assert markup["is_persistent"] is True

@pytest.mark.parametrize("role", [ui.ROLE_USER, ui.ROLE_ADMIN])
def test_settings_actions_are_inline_and_admin_is_role_scoped(role):
    markup = ui.build_menu_inline_markup(role, ui.MENU_SETTINGS, token="bound")
    labels = [b["text"] for row in markup["inline_keyboard"] for b in row]
    assert labels[:6] == ["🎯 Мои поиски", "🔐 hh.ru и резюме", "🧠 Факты для ИИ", "⏰ Расписание", "🚫 Компании", "❓ Помощь"]
    assert ("🛠 Для админа" in labels) == (role == ui.ROLE_ADMIN)
    assert not any("Статистика" in label or "Почему" in label for label in labels)

@pytest.mark.parametrize("old,command", [("🎯 Направления", "/search_settings"), ("🧭 Направления", "/directions"), ("🧠 Обо мне", "/candidate_profile"), ("✋ Спрашивать меня", "/search_application_confirm"), ("✅ ✋ Спрашивать меня", "/search_application_confirm"), ("📋 Отклики", "/menu_responses")])
def test_cached_keyboards_remain_routable(old, command):
    for role in (ui.ROLE_USER, ui.ROLE_ADMIN):
        assert ui._resolve_message_command(old, role) == (command, "")

@pytest.mark.parametrize("data,role,expected", [("nav:bound:directions", ui.ROLE_USER, "/directions"), ("nav:bound:menu_admin", ui.ROLE_USER, None), ("nav:stale:directions", ui.ROLE_ADMIN, None), ("nav:bound:unknown", ui.ROLE_ADMIN, None)])
def test_inline_navigation_checks_binding_and_allowlist(monkeypatch, data, role, expected):
    monkeypatch.setattr(history, "token", lambda home: "bound")
    bot = SimpleNamespace(_selected_profile=lambda p: "qa", _profile=lambda p: SimpleNamespace(home_dir="synthetic"), _dispatch=AsyncMock(), _send_text=AsyncMock(), _answer_callback_query=AsyncMock())
    context = CallbackContext("id", 42, {"user_id": 42, "role": role}, data, {"chat": {"type": "private"}}, 42, 1, lambda p: True)
    asyncio.run(navigation.handle_navigation(bot, context))
    if expected:
        bot._dispatch.assert_awaited_once_with(42, context.principal, expected, "")
    else:
        bot._dispatch.assert_not_awaited()


def test_response_text_has_unambiguous_waiting_label():
    text = ui.build_responses_summary_text({"sent": 12, "viewed": 5, "invitations": 1, "rejected": 3, "waiting": 8})
    assert "Отклики за 30 дней: 12" in text
    assert "⏳ Работодатель ещё не ответил: 8" in text
    assert "Ждут ответа" not in text


def test_period_summary_deduplicates_receipts_and_uses_all_saved_sends(tmp_path, monkeypatch):
    import json
    from datetime import datetime, timezone
    import analytics
    import application_history
    monkeypatch.setattr(analytics, "_now", lambda: datetime(2026, 10, 25, 12, tzinfo=timezone.utc))
    events = []
    for number in range(12):
        base = {"vacancy_id": str(number), "source": "hh", "run_id": "run", "created_at": "2026-10-24T12:00:00+00:00"}
        events += [{**base, "event": "decision", "decision": "applied_auto"},
                   {**base, "event": "application_result", "outcome": "sent"}]
    events += [{"vacancy_id": "old", "source": "hh", "event": "decision", "decision": "applied_auto", "created_at": "2026-08-01T12:00:00+00:00"},
               {"vacancy_id": "uncertain", "source": "hh", "event": "decision", "decision": "apply_uncertain", "created_at": "2026-10-24T12:00:00+00:00"}]
    events += [{"event": "negotiation_status", "vacancy_id": str(number), "source": "hh", "status_bucket": bucket,
                "created_at": "2026-10-25T10:00:00+00:00"}
               for number, bucket in [(0, "positive"), (1, "rejected")]]
    events += [{"event": "negotiation_status", "vacancy_id": "2", "source": "hh", "status_bucket": "pending",
                "status_detail_bucket": "pending_viewed", "created_at": "2026-10-25T10:00:00+00:00"}]
    path = tmp_path / "analytics_events.jsonl"
    path.write_text("".join(json.dumps(row) + "\n" for row in events))
    profile = SimpleNamespace(home_dir=str(tmp_path), analytics_events_file=str(path), seen_file=str(tmp_path / "seen_vacancies.json"))
    assert application_history.summarize(tmp_path, days=7)["count"] == 12
    snapshot = history.responses_snapshot(profile)
    assert snapshot == {"sent": 12, "viewed": 3, "invitations": 1, "rejected": 1, "waiting": 10, "incomplete": False}


@pytest.mark.parametrize("configured,pending", [(True, 2), (True, 0), (False, 0)])
def test_start_uses_local_configuration_and_restores_context(tmp_path, monkeypatch, configured, pending):
    import json
    import directions
    import client_hh_auth
    import application_history
    import company_blacklist
    from telegram_app.navigation import TelegramNavigation
    from hh import session_probe
    cookies = tmp_path / "cookies.json"
    cookies.write_text("{}")
    active = {"name": "qa", "title": "QA", "application_mode": "confirm"} if configured else None
    monkeypatch.setattr(directions, "active_direction", lambda home: active)
    monkeypatch.setattr(directions, "list_directions", lambda home: [active] if active else [])
    monkeypatch.setattr(client_hh_auth, "load_hh_resume_catalog", lambda name: [{"id": "one"}])
    monkeypatch.setattr(application_history, "summarize", lambda *a, **k: {"count": 7})
    monkeypatch.setattr(session_probe, "probe", lambda *a: pytest.fail("/start must not probe hh.ru"))
    monkeypatch.setattr(company_blacklist, "is_blocked", lambda *a: False)
    (tmp_path / "manual_apply_queue.json").write_text(json.dumps({"items": {
        str(number): {"status": "pending", "profile_name": "qa", "direction": "qa"} for number in range(pending)}}))
    profile = SimpleNamespace(home_dir=str(tmp_path), analytics_events_file="synthetic", seen_file="synthetic", hh=SimpleNamespace(cookies_file=str(cookies), application_mode="confirm"))
    bot = SimpleNamespace(_profile=lambda name: profile, _daemon_state=lambda name: {"running": True},
        _profile_schedule=lambda name: (480, 60), _set_selected_menu=lambda *a: None,
        _menu_reply_markup=lambda *a, **k: ui.build_reply_markup(ui.ROLE_USER), _send_text=AsyncMock())
    result = asyncio.run(TelegramNavigation._send_start_summary(bot, 42, {"user_id": 42}, "qa"))
    assert result == configured
    if configured:
        text = bot._send_text.call_args_list[0].args[1]
        assert f"Ждут решения: {pending}" in text
        assert "Отправлено за неделю: 7" in text and "Следующий поиск:" in text
        assert "Подтверждать отклик" in text
        assert (f"Посмотреть {pending}" in str(bot._send_text.call_args_list)) == bool(pending)
    else:
        bot._send_text.assert_not_awaited()


@pytest.mark.parametrize("role,index,stale,expected", [(ui.ROLE_ADMIN, "1", False, "electric"), (ui.ROLE_USER, "1", False, None), (ui.ROLE_ADMIN, "8", False, None), (ui.ROLE_ADMIN, "1", True, None)])
def test_profile_picker_is_admin_only_and_binds_display_order(tmp_path, role, index, stale, expected):
    names = ["qa", "electric"]
    bound = history.profile_picker_token(tmp_path, names)
    bot = SimpleNamespace(_selected_profile=lambda p: "qa", _profile=lambda p: SimpleNamespace(home_dir=tmp_path),
        _profile_names=lambda: list(reversed(names)) if stale else names,
        _dispatch=AsyncMock(), _send_text=AsyncMock(), _answer_callback_query=AsyncMock())
    context = CallbackContext("id", 42, {"user_id": 42, "role": role}, f"nav:{bound}:profile:{index}", {"chat": {"type": "private"}}, 42, 1, lambda p: True)
    asyncio.run(navigation.handle_navigation(bot, context))
    if expected:
        bot._dispatch.assert_awaited_once_with(42, context.principal, "/profile", expected)
    else:
        bot._dispatch.assert_not_awaited()


def test_same_navigation_message_can_be_reopened(monkeypatch):
    monkeypatch.setattr(history, "token", lambda home: "bound")
    bot = SimpleNamespace(_selected_profile=lambda p: "qa", _profile=lambda p: SimpleNamespace(home_dir="synthetic"),
        _dispatch=AsyncMock(), _send_text=AsyncMock(), _answer_callback_query=AsyncMock())
    context = CallbackContext("id", 42, {"user_id": 42, "role": ui.ROLE_USER}, "nav:bound:directions", {"chat": {"type": "private"}}, 42, 1, lambda p: True)
    asyncio.run(navigation.handle_navigation(bot, context))
    asyncio.run(navigation.handle_navigation(bot, context))
    assert bot._dispatch.await_count == 2


def test_same_inline_back_button_remains_reusable(menu_bot, monkeypatch):
    from tests.test_telegram_callbacks import _callback
    bot, _ = menu_bot
    principal = {'user_id': 42, 'role': ui.ROLE_USER, 'profile': 'qa'}
    monkeypatch.setattr('telegram_access.resolve_user', lambda uid: principal)
    monkeypatch.setattr(history, 'token', lambda home: 'bound')
    monkeypatch.setattr(bot, '_answer_callback_query', AsyncMock())
    monkeypatch.setattr(bot, '_dispatch', AsyncMock())
    async def scenario():
        callback = _callback('nav:bound:back', user_id=42, chat_type='private')
        await bot._handle_callback_query(callback)
        await bot._handle_callback_query(callback)
    asyncio.run(scenario())
    assert bot._dispatch.await_count == 2


def test_busy_status_does_not_point_to_removed_button():
    text = ui.build_busy_status_text(active_command='search', profile_name='qa', elapsed_sec=10, can_cancel=True)
    assert 'Нажмите «⏳' not in text
    assert 'Результат придёт отдельным сообщением.' in text
    assert '⏹ Остановить' in text
