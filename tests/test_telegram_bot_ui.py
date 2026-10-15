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
