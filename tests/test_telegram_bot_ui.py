"""Offline contracts for run presentation and the agreed Telegram menu."""
from unittest.mock import AsyncMock
import asyncio

import pytest

import analytics
import notifier
import reporting
import telegram_bot_ui as ui
from run_summary import build_run_summary


def stopped_run():
    return {"run_id": "synthetic", "ok": True, "mode": "search", "status": "finished",
            "found": 117, "applied": 0, "skipped": 117,
            "funnel": {"new": 117, "applied": 0, "uncertain": 1, "not_processed": 113},
            "reason_breakdown": {"apply_uncertain": 1, "hh_recovery_breaker": 113},
            "source_stats": {"hh": {"stop_reason": "hh_recovery_breaker",
                "funnel": {"uncertain": 1, "applied": 0, "not_processed": 113}}}}


@pytest.mark.parametrize("changes", [{}, {"applied": 1, "funnel": {"applied": 1}},
                                      {"source_stats": {}, "funnel": {"applied": 0}}])
def test_console_has_no_success_badge_for_stopped_or_zero_apply(changes):
    run = {**stopped_run(), **changes}
    text = reporting.format_search_console(run)
    assert "✅" not in text
    if not run["applied"]:
        assert analytics.zero_apply_diagnosis(run) in text


def test_console_and_cards_show_execution_separately_from_delivery():
    run = stopped_run()
    for text in (reporting.format_search_console(run), ui.format_run_summary(run),
                 ui.build_run_summary_text(build_run_summary(run), profile_name="qa")):
        assert "Выполнение: завершён" in text
        assert "Исход отклика: не подтверждён; нужна ручная сверка" in text
        assert "источник остановлен" in text and "вакансий не обработано: 113" in text
        assert "✅" not in text


@pytest.mark.parametrize("dry_run", [False, True])
def test_console_success_and_dry_run(dry_run):
    run = {"ok": True, "found": 3, "applied": 1 if not dry_run else 0, "skipped": 2}
    text = reporting.format_search_console(run, dry_run=dry_run)
    if dry_run:
        assert "✅" not in text and "0 откликов: dry-run" in text
    else:
        assert text.startswith("✅") and "Исход отклика: отправлен" in text


def test_source_losses_are_visible_only_when_recorded():
    run = {"ok": True, "applied": 1, "source_stats": {"hh": {"pages_failed": 2,
        "collection": {"source_stopped": True, "source_discarded": True,
                       "combinations_unattempted": 4, "vacancies_discarded": 11}}}}
    text = ui.format_run_summary(run)
    for label in ("страниц не опрошено: 2", "комбинаций не опрошено: 4", "вакансий отброшено: 11"):
        assert label in text
    assert reporting.format_run_losses({"source_stats": {"hh": {"pages_failed": 0}}}) == []


def test_uncertain_manual_command_does_not_claim_failure():
    text = ui.format_command_result("manual AI apply", {"ok": False,
        "stdout": "⚠ Исход отклика не подтверждён; нужна ручная сверка"})
    assert text.startswith("❔") and "завершён с ошибкой" not in text and "❌" not in text


def test_notification_keeps_uncertain_separate_from_unsent(monkeypatch):
    monkeypatch.setattr(notifier, "_telegram_flag", lambda *a: True)
    send = AsyncMock()
    monkeypatch.setattr(notifier, "send_message", send)
    asyncio.run(notifier.notify_summary(117, 0, 117, {
        "hh": {"uncertain": 1, "stop_reason": "recovery", "pages_failed": 2}}))
    text = send.call_args.args[0]
    assert "Не отправлено автоматически: 116" in text
    assert "Исход отклика не подтверждён: 1; нужна ручная сверка" in text
    assert "страниц не опрошено: 2" in text and "❌" not in text

# Exact screen membership is the owner-approved tree; overview/list text appears on entry.
COMMON_SCREENS = {
    ui.MENU_MAIN: ["🔎 Поиск", "🧪 Тестовый прогон", "📋 Отклики", "🎯 Мой поиск", "🔐 Мой hh", "🧠 Обо мне", "🧭 Что ищу", "📊 Мониторинг", "❓ Помощь"],
    ui.MENU_RESPONSES: ["📋 На рассмотрении", "📨 Отклики HH", "📝 Анкеты", "🤖 Ответ ИИ в чат"],
    ui.MENU_SEARCH: ["✏️ Изменить запросы", "✨ Предложить ИИ", "✅ Сохранить предложенное", "⚙️ Условия поиска", "🚫 Компании", "🛡 Режим откликов", "⏰ Расписание"],
    ui.MENU_SEARCH_CONDITIONS: ["👤 Опыт", "💰 Зарплата от", "💵 Только с зарплатой", "🏠 Только удалёнка"],
    ui.MENU_EXPERIENCE: ["👤 Любой опыт", "🌱 Без опыта", "1–3 года", "3–6 лет", "6+ лет"],
    ui.MENU_COMPANIES: ["➕ В чёрный список", "➖ Разрешить компанию"],
    ui.MENU_APPLICATION_MODE: ["👀 Только показать", "✋ Подтверждать", "⚡ Автоотклик"],
    ui.MENU_REPEAT: ["🔁 3 раза/день", "📅 1 раз/день", "🗓 1 раз/нед", "🛑 Повтор: выкл"],
    ui.MENU_HH: ["🔐 Вход HH", "🧾 HH резюме", "🧾 Выбрать резюме", "🔄 Обновить резюме", "🧠 Анализ резюме", "📄 Забрать резюме"],
    ui.MENU_CANDIDATE: ["📋 Мои факты", "✨ Уточнить профиль", "➕ Добавить факт"],
    ui.MENU_MONITOR: ["📊 Статус", "❓ Почему не было откликов", "🕓 Прогоны", "📈 Статистика", "🔬 Отказы и конверсии", "📬 Инвайты", "📜 Журналы"],
    ui.MENU_LOGS: ["📊 Последний запуск", "🧾 Подробный", "💬 Лог чатов"],
}
ADMIN_SCREENS = {
    ui.MENU_ADMIN: ["🩺 Диагностика", "🧾 Профили", "👥 Пользователи", "🧑‍💼 Клиенты", "🧠 Платные LLM", "📰 Дайджест", "🗃 Пересчёт аналитики", "🟢 Фоновый режим: вкл", "⛔ Фоновый режим: выкл"],
    ui.MENU_LLM: ["💰 Баланс", "🧪 Проверить LLM", "🎁 Лимиты ИИ"],
}
MENU_COMMANDS = {
    "/menu_responses": ui.MENU_RESPONSES, "/search_settings": ui.MENU_SEARCH,
    "/menu_hh": ui.MENU_HH, "/candidate_profile": ui.MENU_CANDIDATE,
    "/menu_monitor": ui.MENU_MONITOR, "/menu_logs": ui.MENU_LOGS,
    "/search_conditions": ui.MENU_SEARCH_CONDITIONS, "/search_experience": ui.MENU_EXPERIENCE,
    "/search_application_mode": ui.MENU_APPLICATION_MODE, "/companies": ui.MENU_COMPANIES,
    "/schedule": ui.MENU_REPEAT, "/menu_admin": ui.MENU_ADMIN, "/menu_llm": ui.MENU_LLM,
}


def labels(role, menu):
    return [b["text"] for row in ui.build_reply_markup(role, menu=menu)["keyboard"] for b in row
            if b["text"] not in {ui.BUTTON_BACK, ui.BUTTON_MENU}]


@pytest.mark.parametrize("role", [ui.ROLE_USER, ui.ROLE_ADMIN])
def test_agreed_tree_is_reachable_and_every_button_occurs_once(role):
    from collections import Counter
    screens = {**COMMON_SCREENS}
    if role == ui.ROLE_ADMIN:
        screens.update(ADMIN_SCREENS)
        screens[ui.MENU_MAIN] = [*screens[ui.MENU_MAIN], ui.BUTTON_MENU_ADMIN]
    visited, pending, seen = set(), [ui.MENU_MAIN], Counter()
    while pending:
        menu = pending.pop()
        assert menu not in visited  # Every submenu also has exactly one entry point.
        visited.add(menu)
        actual = labels(role, menu)
        assert Counter(actual) == Counter(screens[menu])
        seen.update(actual)
        for label in actual:
            command, _ = ui._resolve_message_command(label, role)
            assert command.startswith("/")
            if role == ui.ROLE_USER:
                assert command not in ui.ADMIN_ONLY_COMMANDS
            if command in MENU_COMMANDS:
                pending.append(MENU_COMMANDS[command])
    assert visited == set(screens)
    assert set(seen.values()) == {1}
    if role == ui.ROLE_USER:
        assert not set(seen).intersection(label for items in ADMIN_SCREENS.values() for label in items)
    assert labels(role, ui.MENU_MAIN)[:2] == [ui.BUTTON_SEARCH, ui.BUTTON_DRYRUN]


def test_admin_menu_ids_are_rejected_for_regular_users():
    for menu in ADMIN_SCREENS:
        assert ui._normalize_menu(ui.ROLE_USER, menu) == ui.MENU_MAIN
    assert ui._normalize_menu(ui.ROLE_ADMIN, "unknown") == ui.MENU_MAIN


@pytest.mark.parametrize("role", [ui.ROLE_USER, ui.ROLE_ADMIN])
def test_navigation_and_profile_switch_keep_active_controls(role):
    for menu in COMMON_SCREENS:
        markup = ui.build_reply_markup(role, menu=menu, active=True, can_stop=role == ui.ROLE_ADMIN)
        actual = [b["text"] for row in markup["keyboard"] for b in row]
        assert ui.BUTTON_BUSY in actual
        assert (ui.BUTTON_CANCEL_ACTIVE in actual) == (role == ui.ROLE_ADMIN)
        if menu != ui.MENU_MAIN:
            assert ui.BUTTON_BACK in actual and ui.BUTTON_MENU in actual
    profiles = ui.build_reply_markup(role, profile_buttons=["qa", "electrician"], active=True)
    assert profiles["keyboard"][0] == [{"text": "📁 Профиль: qa"}]
    assert profiles["one_time_keyboard"] is True


def test_button_labels_have_no_substring_collisions():
    # Include secondary, guest and busy buttons as well as persistent menu buttons.
    labels = [value for name, value in vars(ui).items()
              if name.startswith("BUTTON_") and not name.endswith("_PREFIX")]
    assert len(labels) == len(set(labels)), "Different buttons must have different labels"
    assert not [(a, b) for a in labels for b in labels if a != b and a in b]


@pytest.mark.parametrize('role', [ui.ROLE_USER, ui.ROLE_ADMIN])
def test_chat_old_cli_failure_is_not_process_success(role):
    text = ui.format_command_result('chat AI reply', {
        'ok': True, 'stdout': '📋 Chat one-shot summary:\n  Чат: 123\n  OK: False'}, role=role)
    assert text.startswith('Не удалось подготовить ответ: ')
    assert '✅' not in text and 'завершён' not in text


@pytest.mark.parametrize('reason,fragment', [
    ('model_unavailable', 'модель недоступна'),
    ('grounding_unavailable', 'модель проверки фактов недоступна'),
    ('invalid_grounding_check', 'некорректную проверку фактов'),
    ('stale_question', 'вопрос изменился'),
    ('chat_unavailable', 'чат недоступен'),
])
def test_chat_failure_texts_are_plain_and_do_not_expose_process_details(reason, fragment):
    import json
    text = ui.format_command_result('chat AI alternative', {'ok': True,
        'stdout': json.dumps({'chat_reply_result': {'ok': False, 'reason': reason}}),
        'stderr': 'Private transport details'})
    assert text.startswith('Не удалось подготовить ответ: ') and fragment in text
    assert '✅' not in text and 'Private' not in text
