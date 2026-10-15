import asyncio
from datetime import datetime

import telegram_bot


class _RunningTask:
    def __init__(self):
        self._done = False

    def done(self):
        return self._done

    def cancel(self):
        self._done = True


class TestCommandHelpers:
    def test_normalize_command(self):
        command, arg = telegram_bot.normalize_command("/Status@TestBot run now")
        assert command == "/status"
        assert arg == "run now"

    def test_split_message(self):
        text = ("abc\n" * 2000).strip()
        parts = telegram_bot.split_message(text, limit=200)
        assert len(parts) > 1
        assert "".join(part.replace("\n", "") for part in parts)

    def test_resolve_message_command_from_button(self):
        command, arg = telegram_bot._resolve_message_command(telegram_bot.BUTTON_STATUS, telegram_bot.ROLE_ADMIN)
        assert command == "/status"
        assert arg == ""

    def test_resolve_message_command_from_section_button(self):
        command, arg = telegram_bot._resolve_message_command(telegram_bot.BUTTON_MENU_REPEAT, telegram_bot.ROLE_ADMIN)
        assert command == "/menu_repeat"
        assert arg == ""

    def test_resolve_message_command_from_section_button_for_user(self):
        command, arg = telegram_bot._resolve_message_command(telegram_bot.BUTTON_MENU_REPEAT, telegram_bot.ROLE_USER)
        assert command == "/menu_repeat"
        assert arg == ""

    def test_resolve_message_command_profile_button(self):
        command, arg = telegram_bot._resolve_message_command(f"{telegram_bot.BUTTON_PROFILE_PREFIX}alice", telegram_bot.ROLE_ADMIN)
        assert command == "/profile"
        assert arg == "alice"

    def test_resolve_message_command_legacy_profile_button(self):
        command, arg = telegram_bot._resolve_message_command("Профиль: alice", telegram_bot.ROLE_ADMIN)
        assert command == "/profile"
        assert arg == "alice"

    def test_resolve_message_command_repeat_button(self):
        command, arg = telegram_bot._resolve_message_command(telegram_bot.BUTTON_REPEAT_DAILY, telegram_bot.ROLE_ADMIN)
        assert command == "/repeat_daily"
        assert arg == ""

    def test_resolve_message_command_busy_button(self):
        command, arg = telegram_bot._resolve_message_command(telegram_bot.BUTTON_BUSY, telegram_bot.ROLE_ADMIN)
        assert command == "/busy"
        assert arg == ""

    def test_resolve_message_command_cancel_active_button(self):
        command, arg = telegram_bot._resolve_message_command(telegram_bot.BUTTON_CANCEL_ACTIVE, telegram_bot.ROLE_ADMIN)
        assert command == "/cancel_active"
        assert arg == ""

    def test_resolve_message_command_ai_limits_button(self):
        command, arg = telegram_bot._resolve_message_command(telegram_bot.BUTTON_AI_LIMITS, telegram_bot.ROLE_ADMIN)
        assert command == "/ai_limits"
        assert arg == ""

    def test_resolve_guest_command(self):
        command, arg = telegram_bot._resolve_guest_command(telegram_bot.BUTTON_CLIENT_START)
        assert command == "/client_start"
        assert arg == ""

    def test_parse_callback_data(self):
        action, user_id = telegram_bot._parse_callback_data("ca:42")
        assert action == telegram_bot.CALLBACK_CLIENT_APPROVE
        assert user_id == 42


class TestFormatting:
    def test_build_help_text(self):
        text = telegram_bot.build_help_text()
        assert "🤖 Job Hunter" in text
        assert "⚡ Основные действия доступны через кнопки меню." in text
        assert "🔧 Раздел «Админ»: профили, пользователи, клиенты и лимиты ИИ." in text

    def test_build_help_text_user_mentions_repeat_commands(self):
        text = telegram_bot.build_help_text(role=telegram_bot.ROLE_USER, profile_name="qa")
        assert "📁 Профиль: QA" in text
        assert "• «Мой поиск» → «Расписание»: периодические запуски" in text

    def test_build_reply_markup_admin(self):
        markup = telegram_bot.build_reply_markup(telegram_bot.ROLE_ADMIN)
        assert markup["resize_keyboard"] is True
        assert any(button["text"] == telegram_bot.BUTTON_MENU_MONITOR for row in markup["keyboard"] for button in row)
        assert not any(button["text"] == telegram_bot.BUTTON_DAEMON_ON for row in markup["keyboard"] for button in row)

    def test_build_reply_markup_admin_repeat_menu(self):
        markup = telegram_bot.build_reply_markup(telegram_bot.ROLE_ADMIN, menu=telegram_bot.MENU_REPEAT)
        assert any(button["text"] == telegram_bot.BUTTON_REPEAT_DAILY for row in markup["keyboard"] for button in row)
        assert markup["is_persistent"] is True

    def test_build_reply_markup_admin_menu_admin(self):
        markup = telegram_bot.build_reply_markup(telegram_bot.ROLE_ADMIN, menu=telegram_bot.MENU_LLM)
        assert any(button["text"] == telegram_bot.BUTTON_AI_LIMITS for row in markup["keyboard"] for button in row)

    def test_build_reply_markup_user_main_menu(self):
        markup = telegram_bot.build_reply_markup(telegram_bot.ROLE_USER)
        assert any(button["text"] == telegram_bot.BUTTON_SEARCH for row in markup["keyboard"] for button in row)
        assert any(button["text"] == telegram_bot.BUTTON_DRYRUN for row in markup["keyboard"] for button in row)

    def test_build_reply_markup_user_run_menu(self):
        markup = telegram_bot.build_reply_markup(telegram_bot.ROLE_USER, menu=telegram_bot.MENU_RUN)
        assert any(button["text"] == telegram_bot.BUTTON_ANALYZE for row in markup["keyboard"] for button in row)
        assert any(button["text"] == telegram_bot.BUTTON_HH_AUTH for row in markup["keyboard"] for button in row)

    def test_build_reply_markup_user_repeat_menu(self):
        markup = telegram_bot.build_reply_markup(telegram_bot.ROLE_USER, menu=telegram_bot.MENU_REPEAT)
        assert any(button["text"] == telegram_bot.BUTTON_REPEAT_DAILY for row in markup["keyboard"] for button in row)
        assert not any(button["text"] == telegram_bot.BUTTON_DAEMON_ON for row in markup["keyboard"] for button in row)

    def test_build_reply_markup_active_adds_busy_controls(self):
        markup = telegram_bot.build_reply_markup(
            telegram_bot.ROLE_USER,
            menu=telegram_bot.MENU_MONITOR,
            active=True,
            can_stop=True,
        )
        assert any(button["text"] == telegram_bot.BUTTON_STATUS for row in markup["keyboard"] for button in row)
        assert any(button["text"] == telegram_bot.BUTTON_BUSY for row in markup["keyboard"] for button in row)
        assert any(button["text"] == telegram_bot.BUTTON_CANCEL_ACTIVE for row in markup["keyboard"] for button in row)
        assert markup["is_persistent"] is True

    def test_build_busy_reply_markup(self):
        markup = telegram_bot.build_busy_reply_markup()
        assert markup["keyboard"][0][0]["text"] == telegram_bot.BUTTON_BUSY
        assert markup["keyboard"][0][1]["text"] == telegram_bot.BUTTON_CANCEL_ACTIVE
        assert markup["is_persistent"] is False

    def test_build_busy_reply_markup_without_stop(self):
        markup = telegram_bot.build_busy_reply_markup(can_stop=False)
        assert markup["keyboard"] == [[{"text": telegram_bot.BUTTON_BUSY}]]

    def test_build_busy_status_text(self):
        text = telegram_bot.build_busy_status_text(
            active_command="search",
            profile_name="qa",
            elapsed_sec=75,
            pid=12345,
            can_cancel=True,
        )
        assert "Сейчас выполняется команда" in text
        assert "Поиск вакансий" in text
        assert "QA" in text
        assert "1 мин 15 сек" in text
        assert "12345" in text
        assert "Остановить" in text

    def test_command_conflicts_with_active(self):
        assert telegram_bot._command_conflicts_with_active("/search") is True
        assert telegram_bot._command_conflicts_with_active("/repeat_daily") is True
        assert telegram_bot._command_conflicts_with_active("/status") is False
        assert telegram_bot._command_conflicts_with_active("/menu") is False
        assert telegram_bot._command_conflicts_with_active("/menu_monitor") is False

    def test_normalize_process_runtime_marks_dead_process_stale(self, monkeypatch):
        runtime = {
            "action": "search_collect",
            "status": "working",
            "pid": 123,
            "message": "Собираю hh.ru",
        }
        monkeypatch.setattr(telegram_bot.runtime_control, "is_pid_running", lambda pid: False)

        normalized = telegram_bot._normalize_process_runtime(runtime, expected_tokens=("agent.py",))

        assert normalized["action"] == "runtime_stale"
        assert normalized["status"] == "stale"
        assert normalized["pid"] == 0

    def test_build_client_review_inline_markup(self):
        markup = telegram_bot.build_client_review_inline_markup(42)
        row = markup["inline_keyboard"][0]
        assert row[0]["callback_data"] == "ca:42"
        assert row[1]["callback_data"] == "cr:42"

    def test_build_clients_inline_markup(self):
        markup = telegram_bot.build_clients_inline_markup(
            [
                {"user_id": 42, "status": "pending_review"},
                {"user_id": 43, "status": "approved"},
            ]
        )
        assert any(button["callback_data"] == "ca:42" for row in markup["inline_keyboard"] for button in row)
        assert any(button["callback_data"] == "ch:43" for row in markup["inline_keyboard"] for button in row)

    def test_build_progress_text_for_analyze(self):
        text = telegram_bot.build_progress_text(
            "analyze resume",
            profile_name="qa",
            elapsed_sec=65,
            frame_idx=2,
        )
        assert "ИИ-анализ резюме" in text
        assert "QA" in text
        assert "1 мин 05 сек" in text
        assert "Обычно занимает 30-90 секунд" in text

    def test_build_status_text(self):
        text = telegram_bot.build_status_text(
            profile_name="default",
            daemon_state={"running": True, "pid": 111},
            bot_state={"running": True, "pid": 222},
            search_interval_min=480,
            invite_check_interval_min=480,
            runtime_status={"action": "search_done", "status": "idle", "mode": "search", "message": "ok", "updated_at": "2026-03-19T12:00:00"},
            bot_runtime={"action": "command_done", "status": "idle", "message": "done", "updated_at": "2026-03-19T12:01:00"},
            last_run={"created_at": "2026-03-19T11:00:00", "mode": "search", "ok": True, "found": 3, "applied": 1, "skipped": 2, "source_stats": {}},
            active_command="search",
        )
        assert "⚙️ Процессы:" in text
        assert "Поиск: 3 раза в день" in text
        assert "⏳ Активная задача: Поиск вакансий" in text
        assert "🕓 Последний прогон:" in text

    def test_build_schedule_text(self):
        text = telegram_bot.build_schedule_text(
            profile_name="qa",
            daemon_state={"running": True, "pid": 111},
            search_interval_min=1440,
            invite_check_interval_min=1440,
        )
        assert "Расписание профиля QA" in text
        assert "1 раз в день" in text

    def test_build_menu_section_text(self):
        text = telegram_bot.build_menu_section_text(
            telegram_bot.MENU_MONITOR,
            role=telegram_bot.ROLE_ADMIN,
            profile_name="qa",
        )
        assert "Мониторинг профиля QA" in text
        assert "Статистика откликов" in text

    def test_build_guest_welcome_text(self):
        text = telegram_bot.build_guest_welcome_text()
        assert "Добро пожаловать" in text
        assert "Стать клиентом" in text

    def test_build_client_status_text(self):
        text = telegram_bot.build_client_status_text(
            {
                "status": "pending_review",
                "auth_status": "not_started",
                "full_name": "Test User",
                "target_role": "QA Engineer",
                "target_location": "Remote",
                "profile_name": "",
                "admin_note": "",
                "submitted_at": "2026-03-19T12:00:00",
            }
        )
        assert "Заявка клиента" in text
        assert "ждёт проверки" in text
        assert "QA Engineer" in text

    def test_build_hh_resumes_text(self):
        text = telegram_bot.build_hh_resumes_text(
            "qa",
            [{"id": "123", "title": "QA Resume", "path": "/tmp/resume.md"}],
        )
        assert "HH резюме профиля QA" in text
        assert "QA Resume" in text

    def test_build_hh_auth_result_text(self):
        text = telegram_bot.build_hh_auth_result_text(
            {
                "ok": True,
                "count": 2,
                "cookies_file": "/tmp/hh_cookies.json",
                "resume_file": "/tmp/resume.md",
                "catalog_path": "/tmp/hh_resumes.json",
                "resumes": [{"title": "QA Resume"}],
            }
        )
        assert "Вход HH завершён" in text
        assert "Сессия HH сохранена." in text
        assert "Основное резюме: QA Resume" in text
        assert "Захвачено резюме" not in text

    def test_build_hh_auth_result_text_authenticated_without_resumes(self):
        text = telegram_bot.build_hh_auth_result_text(
            {
                "ok": False,
                "authenticated": True,
                "cookies_file": "/tmp/hh_cookies.json",
            }
        )
        assert "Сессия HH сохранена." in text
        assert "Импорт резюме не выполнен" in text
        assert "Резюме можно синхронизировать отдельной командой" in text

    def test_build_hh_auth_admin_text(self):
        text = telegram_bot.build_hh_auth_admin_text(
            {
                "ok": True,
                "profile_name": "client_42",
                "cookies_file": "/tmp/hh_cookies.json",
                "count": 1,
                "resumes": [{"title": "QA Resume"}],
                "resume_file": "/tmp/resume.md",
            },
            client={"user_id": 42, "full_name": "Test User", "profile_name": "client_42"},
            debug_log_path="/tmp/telegram-bot-debug.jsonl",
        )
        assert "Сессия HH сохранена" in text
        assert "Test User" in text
        assert "Журнал отладки" in text

    def test_build_clients_text(self):
        text = telegram_bot.build_clients_text(
            [
                {
                    "user_id": 42,
                    "full_name": "Test User",
                    "username": "tester",
                    "status": "approved",
                    "auth_status": "pending_web_auth",
                    "target_role": "QA Engineer",
                    "profile_name": "client_42",
                }
            ]
        )
        assert "Клиенты" in text
        assert "Test User" in text
        assert "/client_approve <id_пользователя> [имя_профиля]" in text
        assert "Быстрые действия" in text

    def test_format_ai_snapshot_text(self):
        text = telegram_bot.format_ai_snapshot_text(
            {
                "free_total": 1,
                "free_used": 1,
                "bonus_total": 2,
                "available_soft": 2,
                "analysis_total": 3,
                "profiles": {"qa": {"analysis_count": 2}, "electrician": {"analysis_count": 1}},
            }
        )
        assert "Бесплатных использовано: 1/1" in text
        assert "Бонусов: 2" in text
        assert "QA 2" in text

    def test_build_ai_limits_text(self):
        text = telegram_bot.build_ai_limits_text(
            users=[{"user_id": 1, "role": telegram_bot.ROLE_ADMIN, "profile": "qa", "label": "owner"}],
            snapshots=[{"user_id": 1, "free_total": 1, "free_used": 1, "bonus_total": 0, "available_soft": 0, "analysis_total": 1, "profiles": {}}],
            events=[{"created_at": "2026-03-19T12:00:00", "action": "analysis_used", "user_id": 1, "profile_name": "qa", "amount": 1, "actor_user_id": 0}],
        )
        assert "Лимиты ИИ" in text
        assert "Базовый бесплатный лимит: 1" in text
        assert "analysis_used" in text

    def test_build_stats_text(self):
        text = telegram_bot.build_stats_text(
            profile_name="qa",
            seen_stats={
                "total": 10,
                "applied": 4,
                "manual": 2,
                "skipped": 4,
                "by_source": {"hh": {"total": 10, "applied": 4, "manual": 2, "skipped": 4}},
            },
            analytics_summary={
                "days": 30,
                "search_runs": 5,
                "events": 20,
                "decisions": 15,
                "auto_applied": 4,
                "manual": 2,
                "dry_run_matched": 1,
                "keyword_filtered": 3,
                "red_flagged": 1,
                "low_score": 2,
                "invitations": 1,
                "positive_statuses": 1,
                "rejected_statuses": 1,
                "pending_statuses": 2,
                "interview_statuses": 1,
                "offer_statuses": 0,
                "test_task_statuses": 0,
                "positive_other_statuses": 0,
                "pending_new_statuses": 1,
                "pending_viewed_statuses": 1,
                "funnel": {"applied": 4, "viewed": 3, "response_rate": 75.0, "positive": 1, "positive_rate": 25.0},
            },
            recent_runs=[{"created_at": "2026-03-19T11:00:00", "mode": "search", "found": 3, "applied": 1, "skipped": 2}],
            selected=True,
        )
        assert "Профиль QA" in text
        assert "Отправлено: 4" in text
        assert "Отказы: 1 | Собесы: 1 | Офферы: 0" in text
        assert "Доля ответов: 75.0%" in text

    def test_build_daily_summary_text(self):
        text = telegram_bot.build_daily_summary_text(
            profile_name="qa",
            analytics_summary={
                "search_runs": 2,
                "events": 9,
                "decisions": 7,
                "auto_applied": 3,
                "manual": 1,
                "rejected_statuses": 1,
                "questionnaires": 2,
                "questionnaire_successes": 2,
                "questionnaire_best_guess": 1,
                "funnel": {"applied": 3, "viewed": 2, "response_rate": 66.7, "positive": 1},
                "by_retry_reason": {"not_viewed": {"auto_applied": 1}},
            },
            recent_runs=[{"created_at": "2026-03-19T11:00:00", "mode": "search", "found": 4, "applied": 2, "skipped": 2}],
            days=1,
        )

        assert "Ежедневная сводка Job Hunter" in text
        assert "Профиль QA · за сутки" in text
        assert "Откликов: 3 | ручных: 1 | отказов: 1" in text
        assert "Воронка: отклики 3 | просмотры 2 | ответ 66.7% | позитив 1" in text
        assert "Анкеты: 2 | успешно 2 | best_guess 1" in text
        assert "Повторы: 1 отклик" in text

    def test_daily_summary_due_respects_hour_and_date(self, monkeypatch, tmp_path):
        state_file = tmp_path / "telegram-state.json"
        monkeypatch.setattr(telegram_bot.config, "TELEGRAM_BOT_STATE_FILE", str(state_file))
        monkeypatch.setattr(telegram_bot.config, "TELEGRAM_DAILY_SUMMARY_ENABLED", True)
        monkeypatch.setattr(telegram_bot.config, "TELEGRAM_DAILY_SUMMARY_HOUR", 20)

        bot = telegram_bot.TelegramBot(profile_name="qa")

        assert bot._daily_summary_due(datetime(2026, 7, 12, 19, 59)) == (False, "2026-07-12")
        assert bot._daily_summary_due(datetime(2026, 7, 12, 20, 0)) == (True, "2026-07-12")

        telegram_bot.runtime_control.write_json_file(
            str(state_file),
            {"daily_summary": {"last_sent_date": "2026-07-12"}},
        )

        assert bot._daily_summary_due(datetime(2026, 7, 12, 21, 0)) == (False, "2026-07-12")
        assert bot._daily_summary_due(datetime(2026, 7, 13, 20, 0)) == (True, "2026-07-13")

    def test_format_command_result(self):
        text = telegram_bot.format_command_result(
            "search",
            {"ok": True, "stdout": "done", "stderr": "", "timeout": False},
        )
        assert text.startswith("ℹ️ Поиск вакансий: команда завершена")
        assert "done" in text

    def test_format_run_summary_includes_note(self):
        text = telegram_bot.format_run_summary(
            {
                "created_at": "2026-03-20T02:45:38",
                "mode": "search",
                "ok": True,
                "found": 0,
                "applied": 0,
                "skipped": 0,
                "source_stats": {
                    "hh": {
                        "label": "hh.ru",
                        "fetched": 50,
                        "already_seen": 50,
                        "new": 0,
                        "relevant": 0,
                        "applied": 0,
                        "manual": 0,
                        "rejected": 0,
                    }
                },
                "note": "Новых вакансий нет. По источникам: hh.ru: просмотрено 50, уже обработано 50",
            }
        )
        assert "Примечание: Новых вакансий нет." in text

    def test_format_command_result_user_hides_stderr(self):
        text = telegram_bot.format_command_result(
            "search",
            {"ok": False, "stdout": "", "stderr": "traceback", "timeout": False},
            role=telegram_bot.ROLE_USER,
        )
        assert "traceback" not in text
        assert "Подробности скрыты" in text

    def test_format_command_result_sanitizes_analyze_output(self):
        text = telegram_bot.format_command_result(
            "analyze resume",
            {
                "ok": True,
                "stdout": "Анализирую резюме: /tmp/resume.md\nМодель: gpt\n# Итог\n**Сильные стороны**\n- опыт",
                "stderr": "",
                "timeout": False,
            },
            role=telegram_bot.ROLE_USER,
        )
        assert "Модель:" not in text
        assert "# Итог" not in text
        assert "Сильные стороны" in text
        assert "• опыт" in text

    def test_extract_analyze_markdown_keeps_markdown_body(self):
        text = telegram_bot._extract_analyze_markdown(
            "Анализирую резюме: /tmp/resume.md\nМодель: gpt\n# Итог\n**Сильные стороны**\n- опыт\n📄 Анализ сохранён: /tmp/file.md"
        )
        assert "Модель:" not in text
        assert "# Итог" in text
        assert "**Сильные стороны**" in text

    def test_analysis_document_payload_falls_back_to_stdout(self, tmp_path, monkeypatch):
        bot = telegram_bot.TelegramBot(profile_name="default")
        monkeypatch.setattr(bot, "_analysis_output_path", lambda profile_name: str(tmp_path / "missing.md"))
        filename, content = bot._analysis_document_payload(
            "qa",
            "Анализирую резюме: /tmp/resume.md\n# Итог\n- опыт",
        )
        assert filename == "qa_resume_analysis.md"
        assert content.decode("utf-8") == "# Итог\n- опыт"

    def test_parse_hh_auth_command_result_reads_last_json_line(self):
        result = telegram_bot.parse_hh_auth_command_result(
            {
                "ok": False,
                "timeout": False,
                "stdout": "log line\n{\"ok\": true, \"count\": 1, \"profile_name\": \"client_42\"}",
                "stderr": "",
            },
            profile_name="client_42",
        )
        assert result["ok"] is True
        assert result["count"] == 1

    def test_parse_hh_auth_command_result_preserves_cancelled(self):
        result = telegram_bot.parse_hh_auth_command_result(
            {
                "ok": False,
                "timeout": False,
                "cancelled": True,
                "stdout": "{\"ok\": false, \"profile_name\": \"client_42\"}",
                "stderr": "",
            },
            profile_name="client_42",
        )
        assert result["cancelled"] is True

    def test_build_stats_text_multi_profile(self):
        text = telegram_bot.build_stats_text(
            profile_name="qa",
            seen_stats={"total": 1, "applied": 1, "manual": 0, "skipped": 0, "by_source": {}},
            analytics_summary={"events": 0, "funnel": {}, "search_runs": 0, "decisions": 0},
            recent_runs=[],
            profile_snapshots=[
                {
                    "profile_name": "qa",
                    "seen_stats": {"total": 1, "applied": 1, "manual": 0, "skipped": 0, "by_source": {}},
                    "analytics_summary": {"events": 0, "funnel": {}, "search_runs": 0, "decisions": 0},
                    "recent_runs": [],
                    "selected": True,
                },
                {
                    "profile_name": "electrician",
                    "seen_stats": {"total": 2, "applied": 2, "manual": 0, "skipped": 0, "by_source": {}},
                    "analytics_summary": {"events": 0, "funnel": {}, "search_runs": 0, "decisions": 0},
                    "recent_runs": [],
                    "selected": False,
                },
            ],
        )
        assert "📈 Профили Job Hunter" in text
        assert "Профиль QA" in text
        assert "Профиль Электрик" in text


class TestProfileSelection:
    def test_profile_names_hide_default_when_named_exist(self, monkeypatch):
        bot = telegram_bot.TelegramBot(profile_name="default")
        monkeypatch.setattr(telegram_bot.profile_mod, "list_profiles", lambda: ["default", "electrician", "qa"])
        assert bot._profile_names() == ["qa", "electrician"]

    def test_selected_profile_promotes_qa_from_default(self, monkeypatch):
        bot = telegram_bot.TelegramBot(profile_name="default")
        monkeypatch.setattr(bot, "_default_profile_name", lambda: "qa")
        monkeypatch.setattr(bot, "_load_state", lambda: {})
        principal = {"user_id": 1, "role": telegram_bot.ROLE_ADMIN, "profile": "default"}
        assert bot._selected_profile(principal) == "qa"


class TestActiveCommands:
    def test_menu_reply_markup_is_scoped_to_selected_profile(self, monkeypatch):
        bot = telegram_bot.TelegramBot(profile_name="default")
        admin = {"user_id": 1, "role": telegram_bot.ROLE_ADMIN, "profile": "qa"}
        user = {"user_id": 42, "role": telegram_bot.ROLE_USER, "profile": "client_42"}

        active_command = bot._mark_active_command(principal=admin, label="search", profile_name="qa")
        active_command.task = _RunningTask()

        monkeypatch.setattr(bot, "_selected_profile", lambda principal: principal["profile"])
        monkeypatch.setattr(bot, "_selected_menu", lambda principal: telegram_bot.MENU_RUN)

        admin_markup = bot._menu_reply_markup(admin)
        user_markup = bot._menu_reply_markup(user)

        assert any(button["text"] == telegram_bot.BUTTON_BUSY for row in admin_markup["keyboard"] for button in row)
        assert not any(button["text"] == telegram_bot.BUTTON_BUSY for row in user_markup["keyboard"] for button in row)

    def test_send_busy_status_does_not_leak_other_profile_run(self, monkeypatch):
        bot = telegram_bot.TelegramBot(profile_name="default")
        admin = {"user_id": 1, "role": telegram_bot.ROLE_ADMIN, "profile": "qa"}
        user = {"user_id": 42, "role": telegram_bot.ROLE_USER, "profile": "client_42"}

        active_command = bot._mark_active_command(principal=admin, label="search", profile_name="qa")
        active_command.task = _RunningTask()
        active_command.subprocess_pid = 391113

        monkeypatch.setattr(bot, "_selected_profile", lambda principal: principal["profile"])
        messages = []

        async def fake_send_text(chat_id, text, reply_markup=None):
            messages.append(text)
            return {}

        monkeypatch.setattr(bot, "_send_text", fake_send_text)

        asyncio.run(bot._send_busy_status(42, user, profile_name="client_42"))

        assert len(messages) == 1
        assert "нет активной команды" in messages[0]
        assert "391113" not in messages[0]
        assert "QA" not in messages[0]

    def test_dispatch_allows_parallel_search_for_other_profile(self, monkeypatch):
        bot = telegram_bot.TelegramBot(profile_name="default")
        admin = {"user_id": 1, "role": telegram_bot.ROLE_ADMIN, "profile": "qa"}
        user = {"user_id": 42, "role": telegram_bot.ROLE_USER, "profile": "client_42"}

        active_command = bot._mark_active_command(principal=admin, label="search", profile_name="qa")
        active_command.task = _RunningTask()

        monkeypatch.setattr(bot, "_selected_profile", lambda principal: principal["profile"])
        calls = []

        async def fake_start(chat_id, principal, profile_name, flag, label, timeout):
            calls.append((chat_id, principal["user_id"], profile_name, flag, label, timeout))

        async def fake_send_text(chat_id, text, reply_markup=None):
            raise AssertionError(f"unexpected send_text: {text}")

        monkeypatch.setattr(bot, "_start_cli_command", fake_start)
        monkeypatch.setattr(bot, "_send_text", fake_send_text)

        asyncio.run(bot._dispatch(42, user, "/search", ""))

        assert calls == [(42, 42, "client_42", "--search", "search", 3600)]

    def test_active_runtime_stays_busy_until_last_profile_finishes(self, monkeypatch):
        bot = telegram_bot.TelegramBot(profile_name="default")
        admin = {"user_id": 1, "role": telegram_bot.ROLE_ADMIN, "profile": "qa"}
        user = {"user_id": 42, "role": telegram_bot.ROLE_USER, "profile": "client_42"}
        runtime_updates = []

        monkeypatch.setattr(bot, "_write_runtime", lambda action, message, status: runtime_updates.append((action, message, status)))

        qa_command = bot._mark_active_command(principal=admin, label="search", profile_name="qa")
        qa_command.task = _RunningTask()
        client_command = bot._mark_active_command(principal=user, label="search", profile_name="client_42")
        client_command.task = _RunningTask()

        bot._clear_active_command("qa")
        assert runtime_updates[-1][2] == "busy"
        assert runtime_updates[-1][1] == "Выполняется search для профиля client_42"

        bot._clear_active_command("client_42")
        assert runtime_updates[-1] == ("command_done", "Нет активных команд", "idle")
