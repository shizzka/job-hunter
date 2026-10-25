import telegram_bot_ui as ui

from telegram_bot import (
    BUTTON_AI_LIMITS,
    BUTTON_BACKFILL,
    BUTTON_DAEMON_OFF,
    BUTTON_DAEMON_ON,
    BUTTON_DRYRUN,
    BUTTON_DIAGNOSTICS,
    BUTTON_HH_AUTH,
    BUTTON_HH_RESPONSES,
    BUTTON_CHAT_AI,
    BUTTON_REPEAT_OFF,
    MENU_RUN,
    ROLE_ADMIN,
    ROLE_USER,
    _format_users_text,
    build_ai_limits_text,
    build_diagnostics_text,
    build_daily_summary_text,
    build_help_text,
    build_hh_auth_result_text,
    build_hh_response_count_text,
    build_menu_section_text,
    format_ai_snapshot_text,
)


def test_button_labels_are_russian():
    from telegram_bot import BUTTON_SEARCH_SETTINGS, BUTTON_SEARCH_USE_DRAFT

    assert BUTTON_DRYRUN == "🧪 Пробный поиск (без откликов)"
    assert BUTTON_DIAGNOSTICS == "🩺 Диагностика"
    assert BUTTON_BACKFILL == "🗃 Пересчёт аналитики"
    assert BUTTON_AI_LIMITS == "🎁 Лимиты ИИ"
    assert BUTTON_HH_AUTH == "🔐 Вход в hh.ru"
    assert BUTTON_HH_RESPONSES == "📨 Отклики на hh.ru"
    assert BUTTON_CHAT_AI == "🤖 Ответ ИИ в чат"
    assert BUTTON_DAEMON_ON == "🟢 Фоновый режим: вкл"
    assert BUTTON_DAEMON_OFF == "⛔ Фоновый режим: выкл"
    assert BUTTON_REPEAT_OFF == "🛑 Повтор: выкл"
    assert BUTTON_SEARCH_SETTINGS == "🎯 Мои поиски"
    assert BUTTON_SEARCH_USE_DRAFT == "✅ Сохранить предложенное"


def test_search_settings_menu_is_available_to_regular_user():
    from telegram_bot_ui import MENU_SEARCH, build_reply_markup

    markup = ui.build_menu_inline_markup(ROLE_USER, MENU_SEARCH, token="synthetic")
    labels = [button["text"] for row in markup["inline_keyboard"] for button in row]

    assert "✏️ Изменить запросы" in labels
    assert "✨ Предложить ИИ" in labels
    assert "✅ Сохранить предложенное" in labels
    assert "🧾 Выбрать резюме" not in labels
    assert "⚙️ Условия поиска" in labels
    assert "🛡 Режим откликов" in labels
    assert "📨 Отклики" in [
        button["text"]
        for row in build_reply_markup(ROLE_USER)["keyboard"]
        for button in row
    ]


def test_search_conditions_and_application_modes_are_available_to_regular_user():
    from telegram_bot_ui import (
        MENU_APPLICATION_MODE,
        MENU_EXPERIENCE,
        MENU_SEARCH_CONDITIONS,
        build_reply_markup,
    )

    condition_labels = [
        button["text"]
        for row in ui.build_menu_inline_markup(ROLE_USER, MENU_SEARCH_CONDITIONS, token="synthetic")["inline_keyboard"]
        for button in row
    ]
    assert "👤 Опыт" in condition_labels
    assert "💰 Зарплата от" in condition_labels
    assert "🏠 Только удалёнка" in condition_labels

    mode_labels = [
        button["text"]
        for row in ui.build_menu_inline_markup(ROLE_USER, MENU_APPLICATION_MODE, token="synthetic")["inline_keyboard"]
        for button in row
    ]
    assert {"👀 Только показать", "✋ Подтверждать отклик", "⚡ Автоматически"} <= set(mode_labels)

    experience_labels = [
        button["text"]
        for row in ui.build_menu_inline_markup(ROLE_USER, MENU_EXPERIENCE, token="synthetic")["inline_keyboard"]
        for button in row
    ]
    assert "1–3 года" in experience_labels


def test_every_section_has_persistent_global_navigation():
    import telegram_bot_ui as ui
    assert ui.normalize_command("/back") == ("/back", "")
    for role in (ROLE_ADMIN, ROLE_USER):
        for menu in ui.reply_menu_layouts(role):
            markup = ui.build_reply_markup(role, menu=menu)
            assert [b["text"] for row in markup["keyboard"] for b in row] == [
                "🔎 Найти вакансии", "📋 Ждут решения", "📨 Отклики", "⚙️ Настройки"]
            assert markup["is_persistent"] is True


def test_help_and_run_menu_do_not_use_old_anglicisms():
    help_text = build_help_text(ROLE_ADMIN, profile_name="qa")
    run_text = build_menu_section_text(MENU_RUN, role=ROLE_USER, profile_name="qa")

    for text in (help_text, run_text):
        assert "Dry-run" not in text
        assert "Backfill" not in text
        assert "AI-" not in text
        assert "AI " not in text
        assert "HH auth" not in text


def test_daily_summary_can_show_calendar_day_period():
    text = build_daily_summary_text(
        profile_name="qa",
        analytics_summary={"search_runs": 0, "events": 0, "decisions": 0, "auto_applied": 0, "manual": 0, "rejected_statuses": 0},
        recent_runs=[],
        period_label="Сегодня, с 00:00 до 20:00",
    )

    assert "Профиль QA · Сегодня, с 00:00 до 20:00" in text


def test_ai_texts_are_russian():
    snapshot = {
        "free_used": 1,
        "free_total": 3,
        "bonus_total": 2,
        "available_soft": 4,
        "analysis_total": 5,
        "profiles": {"qa": {"analysis_count": 5}},
    }
    users = [{"user_id": 1, "role": ROLE_ADMIN, "profile": "qa", "enabled": True, "label": "owner"}]
    snapshots = [{"user_id": 1, **snapshot}]

    snapshot_text = format_ai_snapshot_text(snapshot)
    limits_text = build_ai_limits_text(users=users, snapshots=snapshots, events=[])

    assert "AI-" not in snapshot_text
    assert "AI " not in snapshot_text
    assert "free-" not in limits_text
    assert "ИИ" in snapshot_text
    assert "бесплатный лимит" in limits_text


def test_hh_auth_result_and_users_text_are_russian():
    result_text = build_hh_auth_result_text({"ok": True, "count": 2, "resumes": []})
    users_text = _format_users_text(
        [{"user_id": 42, "role": ROLE_USER, "profile": "qa", "enabled": True, "label": "qa-user"}]
    )

    assert "Cookies" not in result_text
    assert "debug log" not in result_text
    assert "Сессия HH сохранена." in result_text
    assert "Захвачено резюме" not in result_text
    assert "profile " not in users_text
    assert "enabled" not in users_text
    assert "доступ открыт" in users_text


def test_hh_auth_result_mentions_resume_import_only_when_requested():
    result_text = build_hh_auth_result_text(
        {
            "ok": True,
            "authenticated": True,
            "imported_resumes": True,
            "count": 1,
            "resumes": [{"id": "1", "title": "QA Engineer"}],
        }
    )

    assert "Захвачено резюме: 1" in result_text
    assert "Основное резюме: QA Engineer" in result_text


def test_manual_feedback_callback_parser():
    from telegram_bot_ui import _parse_manual_feedback_callback_data

    assert _parse_manual_feedback_callback_data("manual_fb:qa:abcdef123456:good") == ("qa", "abcdef123456", "good")
    assert _parse_manual_feedback_callback_data("manual_fb:qa:abcdef123456:bad") == ("qa", "abcdef123456", "bad")
    assert _parse_manual_feedback_callback_data("manual_fb:qa:abcdef123456:wat") == ("", "", "")


def test_manual_block_company_and_why_callback_parser_and_markup():
    import manual_apply_queue
    from telegram_bot_ui import (
        _parse_manual_block_company_callback_data,
        _parse_manual_why_callback_data,
    )

    assert _parse_manual_block_company_callback_data("manual_block_company:qa:abcdef123456") == ("qa", "abcdef123456")
    assert _parse_manual_block_company_callback_data("manual_apply:qa:abcdef123456") == ("", "")
    assert _parse_manual_why_callback_data("manual_why:qa:abcdef123456") == ("qa", "abcdef123456")
    assert _parse_manual_why_callback_data("manual_apply:qa:abcdef123456") == ("", "")

    markup = manual_apply_queue.build_manual_apply_markup(
        {"source": "hh", "company": "Acme", "url": "https://hh.ru/vacancy/1"},
        "qa",
        "abcdef123456",
    )
    buttons = [button for row in markup["inline_keyboard"] for button in row]

    assert any(button["text"] == "❓ Почему" for button in buttons)
    assert any(button.get("callback_data") == "manual_why:qa:abcdef123456" for button in buttons)
    assert any(button["text"] == "🚫 Заблокировать компанию" for button in buttons)
    assert any(button.get("callback_data") == "manual_block_company:qa:abcdef123456" for button in buttons)


def test_manual_why_text_contains_decision_context():
    import manual_apply_queue

    item = {
        "vacancy": {
            "title": "Junior QA Engineer",
            "company": "Acme",
            "source_label": "hh.ru",
            "url": "https://hh.ru/vacancy/1",
        },
        "evaluation": {
            "score": 62,
            "response_probability_score": 78,
            "cluster": "api_qa",
            "resume_variant": "qa_api",
            "cover_style": "api_qa",
            "red_flags": ["middle wording"],
            "soft_flags": ["junior-friendly"],
            "reason": "Хороший API-матч, но мало уверенности для автоотклика.",
        },
    }

    text = manual_apply_queue.build_manual_why_text(item)

    assert "Почему ручное решение" in text
    assert "Junior QA Engineer @ Acme" in text
    assert "match 62/100 | response 78/100" in text
    assert "Кластер: api_qa" in text
    assert "Красные флаги: middle wording" in text
    assert "Хороший API-матч" in text


def test_manual_chat_ai_arg_parser():
    from telegram_bot import _parse_manual_chat_ai_arg

    assert _parse_manual_chat_ai_arg("https://chatik.hh.ru/chat/5416682595") == ("5416682595", "")
    assert _parse_manual_chat_ai_arg("/chat_ai 5416682595 14513855732") == ("5416682595", "14513855732")
    assert _parse_manual_chat_ai_arg("chat_id=5416682595&message_id=14513855732") == ("5416682595", "14513855732")
    assert _parse_manual_chat_ai_arg("") == ("", "")


def test_chat_ai_command_is_available_to_admin_menu():
    from telegram_bot_ui import ACTIVE_CONFLICT_COMMANDS, ADMIN_BUTTON_MAP, ADMIN_ONLY_COMMANDS

    assert ADMIN_BUTTON_MAP[BUTTON_CHAT_AI] == "/chat_ai"
    assert ADMIN_BUTTON_MAP[BUTTON_DIAGNOSTICS] == "/diagnostics"
    assert "/chat_ai" not in ADMIN_ONLY_COMMANDS
    assert "/chat_ai" in ACTIVE_CONFLICT_COMMANDS
    assert "/chat_ai" in build_help_text(ROLE_ADMIN, profile_name="qa", help_section=True)


def test_response_menu_keeps_results_separate_from_pending_actions():
    from telegram_bot_ui import MENU_RESPONSES, build_reply_markup

    markup = ui.build_menu_inline_markup(ROLE_ADMIN, MENU_RESPONSES, token="synthetic")
    labels = [button["text"] for row in markup["inline_keyboard"] for button in row]

    assert BUTTON_CHAT_AI not in labels
    assert "📈 Статистика" in labels


def test_diagnostics_button_is_visible_in_admin_section_only():
    from telegram_bot_ui import MENU_ADMIN, build_reply_markup

    admin_markup = ui.build_menu_inline_markup(ROLE_ADMIN, MENU_ADMIN, token="synthetic")
    user_markup = ui.build_menu_inline_markup(ROLE_USER, MENU_ADMIN, token="synthetic")
    admin_labels = [button["text"] for row in admin_markup["inline_keyboard"] for button in row]
    user_labels = [button["text"] for row in user_markup["inline_keyboard"] for button in row]

    assert BUTTON_DIAGNOSTICS in admin_labels
    assert BUTTON_DIAGNOSTICS not in user_labels


def test_hh_response_button_is_visible_and_mapped_for_both_roles():
    from telegram_bot_ui import ADMIN_BUTTON_MAP, MENU_RESPONSES, USER_BUTTON_MAP, build_reply_markup

    for role in (ROLE_ADMIN, ROLE_USER):
        markup = ui.build_menu_inline_markup(role, MENU_RESPONSES, token="synthetic")
        labels = [button["text"] for row in markup["inline_keyboard"] for button in row]
        assert "📨 Все" in labels

    assert ADMIN_BUTTON_MAP[BUTTON_HH_RESPONSES] == "/hh_responses"
    assert USER_BUTTON_MAP[BUTTON_HH_RESPONSES] == "/hh_responses"


def test_build_hh_response_count_text():
    text = build_hh_response_count_text(
        {
            "profile": "qa",
            "fetched_at": "2026-09-11T12:00:00+03:00",
            "active": 119,
            "archived": 1611,
            "deleted": 7,
            "total": 1737,
            "delta": {"active": 2, "archived": 1, "deleted": 0, "total": 3},
        }
    )

    assert "Всего за всё время: 1 737" in text
    assert "Активные: 119" in text
    assert "Архив: 1 611" in text
    assert "Удалённые: 7" in text
    assert "Изменение с прошлого замера: +3" in text


def test_build_diagnostics_text():
    text = build_diagnostics_text(
        profile_name="qa",
        generated_at="2026-07-28T13:55:00",
        checks=[
            {"name": "Daemon", "ok": True, "detail": "pid 1"},
            {"name": "Telegram API", "ok": False, "detail": "timeout"},
            {"name": "Polling", "ok": None, "detail": "recent disconnect"},
        ],
    )

    assert "Самодиагностика Job Hunter" in text
    assert "✅ Daemon" in text
    assert "❌ Telegram API" in text
    assert "есть проблемы" in text


def test_chat_ai_manual_callback_parser():
    from telegram_bot_ui import (
        _parse_chat_ai_manual_callback_data,
        _parse_chat_manual_send_callback_data,
    )

    assert _parse_chat_ai_manual_callback_data("chat_ai_any:qa:5416682595:14513855732") == (
        "qa",
        "5416682595",
        "14513855732",
    )
    assert _parse_chat_manual_send_callback_data("chat_send_any:qa:5416682595:14513855732~abcdef012345") == (
        "qa",
        "5416682595",
        "14513855732~abcdef012345",
    )
    assert _parse_chat_ai_manual_callback_data("chat_ai:qa:5416682595:14513855732") == ("", "", "")


def test_chat_ai_candidate_list_markup():
    from telegram_bot import _build_chat_ai_candidates_markup, _build_chat_ai_candidates_text

    summary = {
        "chats_scanned": 12,
        "chats_read": 5,
        "candidates": [
            {
                "chat_id": "5416682595",
                "message_id": "14513855732",
                "title": "QA Engineer",
                "company": "Циан",
                "author": "Олеся",
                "question": "Заполните короткую форму",
                "kind_label": "HR",
                "allow_any": True,
                "google_form_urls": ["https://forms.gle/abc123"],
            },
            {
                "chat_id": "5417714076",
                "message_id": "14599999999",
                "title": "Тестировщик",
                "company": "НДМ",
                "author": "ИИ-помощник",
                "question": "Сколько лет опыта тестирования?",
                "kind_label": "AI",
                "allow_any": False,
            },
        ],
    }

    text = _build_chat_ai_candidates_text(summary)
    markup = _build_chat_ai_candidates_markup("qa", summary)

    assert "Нашёл чатов для ответа: 2" in text and "просмотрено 5 из 12" in text
    assert "QA Engineer @ Циан" in text
    assert "📝 анкета" in text
    assert markup["inline_keyboard"][0][0]["callback_data"] == "chat_ai_any:qa:5416682595:14513855732"
    assert markup["inline_keyboard"][0][1]["text"] == "📝 Анкета 1"
    assert markup["inline_keyboard"][0][1]["callback_data"] == "gform_preview:qa:5416682595:14513855732"
    assert markup["inline_keyboard"][1][0]["callback_data"] == "chat_ai:qa:5417714076:14599999999"



def test_hh_reauth_callback_parser():
    from telegram_bot import _parse_hh_reauth_callback_data as bot_parse_hh_reauth_callback_data
    from telegram_bot_ui import CALLBACK_HH_REAUTH, _parse_hh_reauth_callback_data

    assert CALLBACK_HH_REAUTH == "hh_reauth"
    assert _parse_hh_reauth_callback_data("hh_reauth:qa") == "qa"
    assert bot_parse_hh_reauth_callback_data("hh_reauth:qa") == "qa"
    assert _parse_hh_reauth_callback_data("hh_reauth:qa.profile-1") == "qa.profile-1"
    assert _parse_hh_reauth_callback_data("hh_reauth:bad/profile") == ""
    assert _parse_hh_reauth_callback_data("chat_ai:qa:1:2") == ""


def test_hh_reauth_notifier_markup():
    import notifier

    markup = notifier.build_hh_reauth_markup("qa")
    buttons = [button for row in markup["inline_keyboard"] for button in row]

    assert any(button.get("callback_data") == "hh_reauth:qa" for button in buttons)
    assert any(button.get("url") == "https://hh.ru/account/login" for button in buttons)


def test_hh_auth_code_text_cleanup():
    from telegram_bot import _clean_hh_auth_code_text

    assert _clean_hh_auth_code_text("12 34-56") == "123456"
    assert _clean_hh_auth_code_text("код: 9876") == "9876"


def test_standalone_hh_auth_code_detection():
    from telegram_bot import _looks_like_standalone_hh_auth_code

    assert _looks_like_standalone_hh_auth_code("7113") is True
    assert _looks_like_standalone_hh_auth_code("123") is False
    assert _looks_like_standalone_hh_auth_code("код: 7113") is False


def test_chat_ai_candidate_list_explains_counts_and_skips():
    """B16/B18: «14/60» read like «sent 14 of 60»; say what was found and what was skipped."""
    from telegram_bot import _build_chat_ai_candidates_text
    text = _build_chat_ai_candidates_text({
        "ok": True, "chats_scanned": 60, "chats_read": 14, "skipped_closing": 4, "skipped_other_profile": 2,
        "candidates": [{"chat_id": "1", "message_id": "2", "title": "QA", "company": "A", "question": "Опыт?"}]})
    assert text.startswith("Нашёл чатов для ответа: 1 (просмотрено 14 из 60 последних чатов)")
    assert "отказы и «свяжемся» — 4" in text and "отклики другого профиля — 2" in text


def test_renamed_buttons_keep_their_commands_and_old_keyboard_shortcuts():
    import telegram_bot_ui as ui

    shortcuts = [
        ("🔎 Поиск", ui.BUTTON_SEARCH, "/search"),
        ("🧪 Тестовый прогон", ui.BUTTON_DRYRUN, "/dryrun"),
        ("Тестовый прогон", ui.BUTTON_DRYRUN, "/dryrun"),
        ("🎯 Мой поиск", ui.BUTTON_SEARCH_SETTINGS, "/search_settings"),
        ("🧭 Что ищу", ui.BUTTON_DIRECTIONS, "/directions"),
        ("🔐 Мой hh", ui.BUTTON_HH, "/menu_hh"),
        ("📊 Мониторинг", ui.BUTTON_MENU_MONITOR, "/menu_monitor"),
        ("📨 Отклики HH", ui.BUTTON_HH_RESPONSES, "/hh_responses"),
        ("✋ Подтверждать", ui.BUTTON_APPLICATION_CONFIRM, "/search_application_confirm"),
        ("⚡ Автоотклик", ui.BUTTON_APPLICATION_AUTO, "/search_application_auto"),
    ]
    for role in (ui.ROLE_USER, ui.ROLE_ADMIN):
        for old, new, command in shortcuts:
            expected = "/directions" if new == ui.BUTTON_SEARCH_SETTINGS else "/find" if new == ui.BUTTON_SEARCH else command
            assert ui._resolve_message_command(new, role) == (expected, "")
            old_expected = "/find" if old == "🔎 Поиск" else command
            assert ui._resolve_message_command(old, role) == (old_expected, "")


def test_help_has_troubleshooting_and_start_uses_buttons():
    start = build_help_text(ROLE_USER, profile_name="qa")
    help_text = build_help_text(ROLE_USER, profile_name="qa", help_section=True)
    assert "Если что-то не работает" not in start
    assert "Если что-то не работает" in help_text
    assert "Роль:" not in start and "Роль:" not in help_text
    assert "/" not in start.replace("hh.ru", "")
    assert "Состояние → Журналы → Подробный" in help_text


def test_trial_search_label_in_search_section_and_command_messages():
    import telegram_bot_ui as ui

    label = "Пробный поиск (без откликов)"
    for role in (ui.ROLE_USER, ui.ROLE_ADMIN):
        texts = [ui.build_menu_section_text(ui.MENU_SEARCH, role=role, profile_name="synthetic"),
                  ui.build_progress_text("dry-run", profile_name="synthetic"),
                  ui.build_busy_status_text(active_command="dry-run", profile_name="synthetic", elapsed_sec=1)]
        texts += [ui.format_command_result("dry-run", result, role=role) for result in
                  ({"ok": True}, {"ok": False}, {"cancelled": True}, {"timeout": True})]
        for text in texts:
            assert label in text
            assert "Тестовый прогон" not in text
