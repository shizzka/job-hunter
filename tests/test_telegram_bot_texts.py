from telegram_bot import (
    BUTTON_AI_LIMITS,
    BUTTON_BACKFILL,
    BUTTON_DAEMON_OFF,
    BUTTON_DAEMON_ON,
    BUTTON_DRYRUN,
    BUTTON_HH_AUTH,
    BUTTON_REPEAT_OFF,
    MENU_RUN,
    ROLE_ADMIN,
    ROLE_USER,
    _format_users_text,
    build_ai_limits_text,
    build_help_text,
    build_hh_auth_result_text,
    build_menu_section_text,
    format_ai_snapshot_text,
)


def test_button_labels_are_russian():
    assert BUTTON_DRYRUN == "🧪 Тестовый прогон"
    assert BUTTON_BACKFILL == "🗃 Пересчёт аналитики"
    assert BUTTON_AI_LIMITS == "🎁 Лимиты ИИ"
    assert BUTTON_HH_AUTH == "🔐 Вход HH"
    assert BUTTON_DAEMON_ON == "🟢 Демон: вкл"
    assert BUTTON_DAEMON_OFF == "⛔ Демон: выкл"
    assert BUTTON_REPEAT_OFF == "🛑 Повтор: выкл"


def test_help_and_run_menu_do_not_use_old_anglicisms():
    help_text = build_help_text(ROLE_ADMIN, profile_name="qa")
    run_text = build_menu_section_text(MENU_RUN, role=ROLE_USER, profile_name="qa")

    for text in (help_text, run_text):
        assert "Dry-run" not in text
        assert "Backfill" not in text
        assert "AI-" not in text
        assert "AI " not in text
        assert "HH auth" not in text


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
    assert "profile " not in users_text
    assert "enabled" not in users_text
    assert "доступ открыт" in users_text
