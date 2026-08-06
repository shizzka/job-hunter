#!/usr/bin/env python3
"""
Job Hunter Agent — автоматический поиск и отклик на вакансии hh.ru, SuperJob, Хабр Карьере и GeekJob.

Использование:
    python agent.py --login          # Первый запуск: ручной логин, сохранение cookies
    python agent.py --geekjob-login  # Ручной логин в GeekJob
    python agent.py --search         # Один прогон: поиск + отклики
    python agent.py --check          # Проверить приглашения
    python agent.py --daemon         # Демон: поиск каждые N мин + проверка приглашений
    python agent.py --stats          # Статистика
    python agent.py --dry-run        # Поиск без откликов (только показать что найдётся)
"""
import argparse
import asyncio
import json
import logging
import os
import re
import signal
import sys
from collections import defaultdict
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime

import config
import filters
import hh_resume_pipeline as hh_pipeline
import hh_guard
import profile as profile_mod
import reporting
import runtime_control
import seen
import analytics
import search_pipeline
import apply_orchestrator
import invitation_sync
import manual_apply_queue
from outcome import (
    DECISION_APPLIED_AUTO,
    DECISION_ALREADY_APPLIED,
    DECISION_APPLY_FAILED,
    DECISION_APPLY_FAILED_EXCEPTION,
    DECISION_DRY_RUN_MATCH,
    DECISION_QUESTIONS_REQUIRED,
    DECISION_MANUAL_REVIEW,
    DECISION_SKIPPED_LOW_SCORE,
    DECISION_SKIPPED_RED_FLAGS,
)
from geekjob_client import GeekJobClient
from habr_career_client import HabrCareerClient
from hh_client import HHClient
from matcher import analyze_cover_letter, evaluate_vacancy, generate_cover_letter, is_manual_review_candidate
from office_bridge import office_log, create_task, task_progress, task_complete
from office_bridge import close_session as close_office_session
import notifier
from notifier import (
    notify_application, notify_invitation, notify_search_started, notify_summary, notify_digest, notify_needs_manual,
    close_session as close_notify_session,
)
from superjob_client import SuperJobClient
from commands import google_forms as google_form_commands


@contextmanager
def _temporary_config_values(**overrides):
    previous = {name: getattr(config, name) for name in overrides}
    try:
        for name, value in overrides.items():
            setattr(config, name, value)
        yield
    finally:
        for name, value in previous.items():
            setattr(config, name, value)


def _evaluation_with_guard_flag(evaluation: dict, flag: str) -> dict:
    updated = dict(evaluation or {})
    flags = list(updated.get("guard_flags") or [])
    if flag not in flags:
        flags.append(flag)
    updated["guard_flags"] = flags
    return updated


def _build_logging_handlers() -> list[logging.Handler]:
    handlers: list[logging.Handler] = [logging.StreamHandler()]

    if config.LOG_FILE:
        log_dir = os.path.dirname(config.LOG_FILE)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        handlers.append(logging.FileHandler(config.LOG_FILE))

    if config.ERROR_LOG_FILE:
        error_dir = os.path.dirname(config.ERROR_LOG_FILE)
        if error_dir:
            os.makedirs(error_dir, exist_ok=True)
        error_handler = logging.FileHandler(config.ERROR_LOG_FILE)
        error_handler.setLevel(logging.WARNING)
        handlers.append(error_handler)

    return handlers


def _configure_logging(force: bool = False) -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=_build_logging_handlers(),
        force=force,
    )


_configure_logging()
log = logging.getLogger("agent")
SOURCE_ORDER = reporting.SOURCE_ORDER
_source_label = reporting.source_label
_format_compact_source_counts = reporting.format_compact_source_counts
_format_source_progress = reporting.format_source_progress

_CLOSED_OR_ARCHIVED_VACANCY_MARKERS = (
    "вакансия в архиве",
    "вакансия находится в архиве",
    "вакансия уже в архиве",
    "вакансия перемещена в архив",
    "вакансия закрыта",
    "вакансия уже закрыта",
    "закрыта и не принимает отклики",
    "не принимает отклики",
    "прием откликов закрыт",
    "приём откликов закрыт",
    "отклики больше не принимаются",
    "вакансия неактивна",
    "страница вакансии удалена",
)
_CLOSED_OR_ARCHIVED_VACANCY_COMPACT_MARKERS = (
    '"archived":"true"',
    '"archived":true',
    "'archived':'true'",
    "'archived':true",
    "&quot;archived&quot;:&quot;true&quot;",
    "&quot;archived&quot;:true",
)


def _has_archived_vacancy_state(value: str) -> bool:
    compact = "".join(str(value or "").split()).casefold()
    return any(marker in compact for marker in _CLOSED_OR_ARCHIVED_VACANCY_COMPACT_MARKERS)


def _looks_like_closed_or_archived(vacancy: dict, details: str) -> bool:
    raw_text = "\n".join(
        str(part or "")
        for part in (
            vacancy.get("title"),
            vacancy.get("company"),
            vacancy.get("snippet"),
            vacancy.get("url"),
            details,
        )
    )
    if _has_archived_vacancy_state(raw_text):
        return True

    compact = "".join(raw_text.split()).casefold()
    if "<html" in compact or "<template" in compact:
        return False

    text = " ".join(raw_text.split()).casefold()
    return any(marker in text for marker in _CLOSED_OR_ARCHIVED_VACANCY_MARKERS)



def _write_runtime_status(
    action: str,
    message: str,
    status: str,
    mode: str,
    extra: dict | None = None,
) -> None:
    payload = {
        "agent_id": config.AGENT_ID,
        "action": action,
        "message": message,
        "status": status,
        "mode": mode,
        "pid": os.getpid(),
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    if extra:
        payload.update(extra)

    try:
        os.makedirs(os.path.dirname(config.RUNTIME_STATUS_FILE), exist_ok=True)
        with open(config.RUNTIME_STATUS_FILE, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
    except Exception as exc:
        log.warning("Failed to write runtime status: %s", exc)


def _append_run_history(entry: dict) -> None:
    try:
        os.makedirs(os.path.dirname(config.RUN_HISTORY_FILE), exist_ok=True)
        with open(config.RUN_HISTORY_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception as exc:
        log.warning("Failed to append run history: %s", exc)


def _record_search_run(result: dict, dry_run: bool, ok: bool, error: str = "") -> None:
    mode = "dry-run" if dry_run else "search"
    entry = {
        "kind": "search",
        "ok": ok,
        "mode": mode,
        "found": result.get("found", 0),
        "applied": result.get("applied", 0),
        "skipped": result.get("skipped", 0),
        "source_stats": result.get("source_stats", {}),
        "note": result.get("note", ""),
        "error": error,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    _append_run_history(entry)
    analytics.record_search_finished(
        run_id=result.get("_run_id", ""),
        mode=mode,
        result={**result, "ok": ok},
    )





def _format_no_new_vacancies_note(source_stats: dict | None) -> str:
    if not source_stats:
        return "Новых вакансий нет"

    lines = []
    for source in SOURCE_ORDER:
        bucket = (source_stats or {}).get(source)
        if not bucket:
            continue
        fetched = int(bucket.get("fetched", 0) or 0)
        already_seen = int(bucket.get("already_seen", 0) or 0)
        if fetched <= 0 and already_seen <= 0:
            continue
        label = bucket.get("label") or source
        parts = [f"{label}: просмотрено {fetched}"]
        if already_seen > 0:
            parts.append(f"уже обработано {already_seen}")
        lines.append(", ".join(parts))
    if not lines:
        return "Новых вакансий нет"
    return "Новых вакансий нет. По источникам: " + "; ".join(lines)


def _snapshot_slug(value: object, max_length: int = 80) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(value or "").strip()).strip("._-")
    if not slug:
        return "unknown"
    return slug[:max_length]


async def _save_autoapply_failure_snapshot(
    source: str,
    vacancy_id: str,
    page,
) -> dict[str, str]:
    if page is None:
        return {}

    state_dir = Path(config.HH_STATE_DIR)
    state_dir.mkdir(parents=True, exist_ok=True)

    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    base_name = (
        f"autoapply_failed_{stamp}_"
        f"{_snapshot_slug(source, max_length=24)}_"
        f"{_snapshot_slug(vacancy_id, max_length=64)}"
    )
    screenshot_path = state_dir / f"{base_name}.png"
    html_path = state_dir / f"{base_name}.html"
    saved: dict[str, str] = {}

    try:
        await page.screenshot(path=str(screenshot_path), full_page=True)
        saved["screenshot"] = str(screenshot_path)
    except Exception as exc:
        log.warning("Failed to save auto-apply screenshot for %s/%s: %s", source, vacancy_id, exc)

    try:
        html = await page.content()
        html_path.write_text(html, encoding="utf-8")
        saved["html"] = str(html_path)
    except Exception as exc:
        log.warning("Failed to save auto-apply HTML for %s/%s: %s", source, vacancy_id, exc)

    if saved:
        log.warning(
            "Saved auto-apply failure snapshot for %s/%s: %s",
            source,
            vacancy_id,
            ", ".join(saved.values()),
        )
    return saved


def _snapshot_looks_like_closed_or_archived(
    vacancy: dict,
    snapshot: dict[str, str],
    extra_text: str = "",
) -> bool:
    if _looks_like_closed_or_archived(vacancy, extra_text):
        return True

    html_path = snapshot.get("html")
    if not html_path:
        return False
    try:
        html = Path(html_path).read_text(encoding="utf-8", errors="ignore")
    except Exception as exc:
        log.debug("Failed to read auto-apply snapshot %s: %s", html_path, exc)
        return False
    return _looks_like_closed_or_archived(vacancy, html)


def _mark_closed_or_archived_after_apply_attempt(
    *,
    vid: str,
    vacancy: dict,
    source: str,
    evaluation: dict,
    details: str,
    resume_variant: dict | None,
    result: dict,
    bucket: dict,
    run_id: str,
    note: str,
) -> None:
    seen.mark_seen(vid, vacancy, "skipped_archived")
    if source == "hh":
        hh_pipeline.mark_terminal(vid, "closed_or_archived")
    result["skipped"] += 1
    bucket["rejected"] += 1
    analytics.record_decision(
        run_id=run_id,
        vacancy=vacancy,
        decision=DECISION_SKIPPED_LOW_SCORE,
        evaluation=evaluation,
        details=details,
        resume_variant=resume_variant,
        note=note,
    )


def _autoapply_page_for_source(
    source: str,
    hh_client: HHClient | None,
    superjob_client: SuperJobClient | None,
    habr_client: HabrCareerClient | None,
    geekjob_client: GeekJobClient | None,
):
    client = {
        "hh": hh_client,
        "superjob": superjob_client,
        "habr": habr_client,
        "geekjob": geekjob_client,
    }.get(source)
    return getattr(client, "_page", None) if client is not None else None


async def do_login():
    """Интерактивный логин в браузере + автозагрузка резюме."""
    client = HHClient()
    try:
        # Логин с keep_open — браузер останется для загрузки резюме
        await client.login_interactive(keep_open=True)

        # Сразу качаем резюме тем же браузером
        print("\n⏳ Загружаю резюме с hh.ru...")
        await _save_resume_from_client(client)
    except Exception as e:
        log.error("Login failed: %s", e, exc_info=True)
        print(f"❌ Ошибка: {e}")
    finally:
        await client.stop()


async def do_google_login():
    """Интерактивный логин в Google внутри Playwright-контекста профиля."""
    client = HHClient()
    try:
        await client.google_login_interactive(
            "https://docs.google.com/forms/d/e/1FAIpQLScVKWLjCFv5RQ9HlufLEue5Rr02qKmZD4FT0RL0KXSMcq4_ww/viewform"
        )
    except Exception as e:
        log.error("Google login failed: %s", e, exc_info=True)
        print(f"❌ Ошибка: {e}")
    finally:
        try:
            await client.stop()
        except Exception:
            pass


async def do_habr_login():
    """Интерактивный логин в Хабр Карьере."""
    client = HabrCareerClient()
    try:
        await client.login_interactive()
    finally:
        await client.stop()
        await client.stop_browser()


async def do_superjob_login():
    """Интерактивный логин в SuperJob через API."""
    client = SuperJobClient()
    try:
        await client.login_interactive()
    except Exception as e:
        log.error("SuperJob login failed: %s", e, exc_info=True)
        print(f"❌ Ошибка: {e}")
    finally:
        await client.stop()


async def do_geekjob_login():
    """Интерактивный логин в GeekJob."""
    client = GeekJobClient()
    try:
        await client.login_interactive()
    except Exception as e:
        log.error("GeekJob login failed: %s", e, exc_info=True)
        print(f"❌ Ошибка: {e}")
    finally:
        await client.stop()


async def _save_resume_from_client(client: HHClient):
    """Скачать и сохранить резюме через уже открытый клиент."""
    import os
    import hh_resume_pipeline

    resumes = await client.get_resume_ids()
    if not resumes:
        print("❌ Резюме не найдены на hh.ru")
        return

    # Профильный фильтр: если включён HH-резюме-пайплайн с заданными тайтлами,
    # оставляем только резюме, попавшие в варианты профиля. Остальные
    # (например, резюме другого профиля в том же hh-аккаунте) скрываем.
    if hh_resume_pipeline.enabled():
        resolved = hh_resume_pipeline.resolve_variants(resumes)
        expected_ids = {v["id"] for v in resolved if v.get("id")}
        if expected_ids:
            filtered = [r for r in resumes if str(r.get("id", "")) in expected_ids]
            if filtered:
                hidden = len(resumes) - len(filtered)
                if hidden > 0:
                    print(f"ℹ️  Скрыто резюме не из профиля: {hidden}")
                resumes = filtered

    # Если несколько — даём выбрать
    chosen = resumes[0]
    if len(resumes) > 1:
        print(f"\n📋 Найдено {len(resumes)} резюме:\n")
        for i, r in enumerate(resumes, 1):
            print(f"  {i}. {r['title']}")
        print()
        loop = asyncio.get_running_loop()
        answer = await loop.run_in_executor(None, lambda: input(f"Какое качать? [1-{len(resumes)}]: ").strip())
        try:
            idx = int(answer) - 1
            if 0 <= idx < len(resumes):
                chosen = resumes[idx]
            else:
                print(f"  Некорректный номер, беру первое: {resumes[0]['title']}")
        except ValueError:
            print(f"  Беру первое: {resumes[0]['title']}")

    result = await client.download_resume_by_id(chosen)
    if not result["raw"].strip() or not result["title"]:
        print("❌ Не удалось скачать резюме")
        return

    os.makedirs(os.path.dirname(config.RESUME_FILE), exist_ok=True)
    with open(config.RESUME_FILE, "w") as f:
        f.write(result["raw"])

    print(f"\n✅ Резюме сохранено: {config.RESUME_FILE}")
    print(f"   Должность: {result['title']}")
    print(f"   Разделов: {len(result['sections'])}")
    for name in result["sections"]:
        print(f"   • {name}")
    print(f"\nLLM будет использовать это резюме для оценки вакансий.")


async def do_grab_resume():
    """Скачать резюме с hh.ru (отдельный запуск)."""
    client = HHClient()
    try:
        await client.start()

        if not await client.is_logged_in():
            print("❌ Не залогинен! Сначала: python agent.py --login")
            return

        await _save_resume_from_client(client)
    except Exception as e:
        log.error("Grab resume failed: %s", e, exc_info=True)
        print(f"❌ Ошибка: {e}")
    finally:
        await client.stop()


def _print_hh_resume_boost_detail(detail: dict) -> None:
    print("📌 HH resume boost:")
    print(f"  OK: {detail.get('ok')}")
    print(f"  Резюме: {detail.get('title') or '-'} ({detail.get('resume_id') or '-'})")
    print(f"  Можно поднять: {'да' if detail.get('can_boost') else 'нет'}")
    print(f"  Причина: {detail.get('reason') or '-'}")
    if detail.get("button_text"):
        print(f"  Кнопка: {detail.get('button_text')}")
    print(f"  Найдено резюме: {detail.get('resumes_found', 0)}")
    if detail.get("debug_screenshot"):
        print(f"  Скрин: {detail.get('debug_screenshot')}")
    if detail.get("debug_html"):
        print(f"  HTML: {detail.get('debug_html')}")


async def do_hh_resume_boost_status():
    """Проверить кнопку поднятия HH-резюме без клика."""
    client = HHClient()
    try:
        await client.start()
        if not await client.is_logged_in():
            print("❌ Не залогинен! Сначала: ./run.sh login")
            return
        detail = await client.get_resume_boost_status(
            resume_id=config.HH_PRIMARY_RESUME_ID,
            resume_title=config.HH_PRIMARY_RESUME_TITLE,
        )
        _print_hh_resume_boost_detail(detail)
    except Exception as e:
        log.error("HH resume boost status failed: %s", e, exc_info=True)
        print(f"❌ Ошибка: {e}")
    finally:
        await client.stop()


async def do_hh_resume_boost(confirm: str):
    """Ручное поднятие HH-резюме с явным подтверждением."""
    if not config.HH_RESUME_BOOST_ENABLED:
        print("❌ Поднятие отключено: выставь HH_RESUME_BOOST_ENABLED=1 для ручного запуска.")
        return
    if confirm != config.HH_RESUME_BOOST_CONFIRM_TEXT:
        print(f"❌ Для клика нужно подтверждение: ./run.sh resume-boost {config.HH_RESUME_BOOST_CONFIRM_TEXT}")
        return

    client = HHClient()
    try:
        await client.start()
        if not await client.is_logged_in():
            print("❌ Не залогинен! Сначала: ./run.sh login")
            return
        detail = await client.boost_resume(
            resume_id=config.HH_PRIMARY_RESUME_ID,
            resume_title=config.HH_PRIMARY_RESUME_TITLE,
            confirm=confirm,
        )
        _print_hh_resume_boost_detail(detail)
    except Exception as e:
        log.error("HH resume boost failed: %s", e, exc_info=True)
        print(f"❌ Ошибка: {e}")
    finally:
        await client.stop()


async def do_manual_apply_token(token: str) -> dict:
    """Отправить подтвержденный человеком yellow-zone отклик по token из очереди."""
    item = manual_apply_queue.get_candidate(token)
    if not item:
        print(f"❌ Заявка не найдена или устарела: {token}")
        return {"ok": False, "message": "manual apply token not found"}

    vacancy = dict(item.get("vacancy") or {})
    evaluation = dict(item.get("evaluation") or {})
    details = str(item.get("details") or "")
    source = vacancy.get("source", "hh")
    score = int(evaluation.get("score") or 0)
    reason = str(evaluation.get("reason") or "")

    if source != "hh":
        message = f"ИИ-отклик по кнопке пока поддержан только для hh.ru, источник: {source}."
        manual_apply_queue.mark_candidate(token, "unsupported", message)
        await notify_needs_manual(vacancy, score, reason, note=f"{message} Открой вручную.")
        print(f"❌ {message}")
        return {"ok": False, "message": message}

    run_id = analytics.new_run_id("manual-ai-apply")
    hh_client = HHClient()
    try:
        await hh_client.start()
        if not await hh_client.is_logged_in():
            raise RuntimeError("Не залогинен в hh.ru")

        can_apply, guard_note = hh_guard.can_auto_apply()
        if not can_apply:
            manual_apply_queue.mark_candidate(token, "deferred", guard_note)
            await notify_needs_manual(
                vacancy,
                score,
                reason,
                note=f"ИИ-отклик не отправил: {guard_note}. Открой вручную или повтори позже.",
            )
            print(f"⏸ {guard_note}")
            return {"ok": False, "message": guard_note, "deferred": True}

        if not details:
            details = await apply_orchestrator.fetch_vacancy_details(vacancy, hh_client=hh_client)
        if _looks_like_closed_or_archived(vacancy, details):
            message = "Вакансия закрыта или в архиве"
            seen.mark_seen(vacancy.get("id", token), vacancy, "manual_ai_archived")
            hh_pipeline.mark_terminal(vacancy.get("id", token), "closed_or_archived")
            manual_apply_queue.mark_candidate(token, "archived", message)
            analytics.record_decision(
                run_id=run_id,
                vacancy=vacancy,
                decision=DECISION_SKIPPED_LOW_SCORE,
                evaluation={**evaluation, "should_apply": False, "red_flags": ["closed_or_archived"]},
                details=details,
                note="manual_ai:closed_or_archived",
            )
            print(f"ℹ️ {message}")
            return {"ok": False, "message": message, "closed_or_archived": True}

        vacancy["details"] = details
        cover_limit = apply_orchestrator.get_cover_letter_limit(source)
        cover = await generate_cover_letter(vacancy, details)
        cover = cover or ""
        if len(cover) > cover_limit:
            cover = cover[:cover_limit]
        cover_evaluation = _evaluation_with_cover_letter(evaluation, cover)
        if not (cover or "").strip():
            message = "ИИ-сопровод не сгенерировался; отклик без текста не отправляю."
            manual_apply_queue.mark_candidate(token, "failed_no_cover", message)
            analytics.record_decision(
                run_id=run_id,
                vacancy=vacancy,
                decision=DECISION_APPLY_FAILED,
                evaluation=_evaluation_with_cover_letter(
                    _evaluation_with_guard_flag(evaluation, "no_cover_letter"),
                    cover,
                ),
                details=details,
                note="manual_ai:no_cover_letter",
            )
            await notify_needs_manual(
                vacancy,
                score,
                reason,
                note=f"{message} Открой вручную или повтори позже.",
            )
            print(f"❌ {message}")
            return {"ok": False, "message": message, "no_cover": True}

        apply_result = await apply_orchestrator.dispatch_apply(vacancy, cover, hh_client=hh_client)
        _record_hh_questionnaire_analytics(
            run_id=run_id,
            vacancy=vacancy,
            apply_result=apply_result,
            success=bool(apply_result.get("ok")),
        )
        apply_notes = apply_result.get("notes") or []
        apply_note_text = "; ".join(str(item) for item in apply_notes if item)
        question_answer_note = _format_hh_question_answers_for_note(apply_result)
        if question_answer_note:
            apply_note_text = (apply_note_text + "\n\n" + question_answer_note).strip()
        apply_message = str(apply_result.get("message", ""))

        if apply_result.get("closed_or_archived") or _looks_like_closed_or_archived(vacancy, apply_message):
            seen.mark_seen(vacancy.get("id", token), vacancy, "manual_ai_archived")
            hh_pipeline.mark_terminal(vacancy.get("id", token), "closed_or_archived")
            manual_apply_queue.mark_candidate(token, "archived", apply_message or "closed_or_archived")
            print("ℹ️ Вакансия уже закрыта или в архиве")
            return {"ok": False, "message": apply_message, "closed_or_archived": True}

        if apply_result.get("already_applied"):
            seen.mark_seen(vacancy.get("id", token), vacancy, "already_applied")
            hh_pipeline.mark_terminal(vacancy.get("id", token), "already_applied")
            manual_apply_queue.mark_candidate(token, "already_applied", apply_message or "already applied")
            print("ℹ️ Уже откликались ранее")
            return {"ok": True, "already_applied": True, "message": apply_message}

        if apply_result.get("ok"):
            seen.mark_seen(vacancy.get("id", token), vacancy, "manual_ai_applied")
            hh_guard.record_apply_success()
            hh_pipeline.record_successful_apply(vacancy, {"name": "manual_ai", "title": "", "id": ""})
            manual_apply_queue.mark_candidate(token, "applied", apply_message or "ok")
            analytics.record_decision(
                run_id=run_id,
                vacancy=vacancy,
                decision=DECISION_APPLIED_AUTO,
                evaluation={**cover_evaluation, "should_apply": True},
                details=details,
                note="manual_ai_yellow_zone",
            )
            await notify_application(
                vacancy,
                score,
                cover,
                note=("Ручной AI-отклик из yellow-zone." + (f" {apply_note_text}" if apply_note_text else "")),
            )
            print(f"✅ ИИ-отклик отправлен: {vacancy.get('title', '—')} @ {vacancy.get('company', '—')}")
            return {"ok": True, "message": apply_message or "ok"}

        message = apply_message or "unknown apply failure"
        manual_apply_queue.mark_candidate(token, "failed", message)
        await notify_needs_manual(
            vacancy,
            score,
            reason,
            note=f"ИИ-отклик не завершился: {message}. Открой вручную.",
        )
        print(f"❌ ИИ-отклик не завершился: {message}")
        return {"ok": False, "message": message}

    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        manual_apply_queue.mark_candidate(token, "failed", message)
        await notify_needs_manual(
            vacancy,
            score,
            reason,
            note=f"ИИ-отклик упал: {message}. Открой вручную.",
        )
        log.exception("Manual AI apply failed for token %s", token)
        print(f"❌ ИИ-отклик упал: {message}")
        return {"ok": False, "message": message}
    finally:
        try:
            await hh_client.stop()
        except Exception:
            pass


async def _mark_manual(
    status_msg: str,
    seen_action: str,
    decision: str,
    manual_note: str,
    v: dict,
    vid: str,
    score: int,
    reason: str,
    evaluation: dict,
    details: str,
    result: dict,
    bucket: dict,
    run_id: str,
    set_hunter_status,
    resume_variant: dict | None = None,
    analytics_note: str = "",
    reply_markup: dict | None = None,
    **extra_analytics,
) -> None:
    """Общий хелпер для пометки вакансии как manual (ручной разбор)."""
    source = v.get("source", "unknown")
    source_label = v.get("source_label") or _source_label(source)
    await set_hunter_status("search_manual", status_msg, "busy")
    seen.mark_seen(vid, v, seen_action)
    result["skipped"] += 1
    bucket["manual"] += 1
    analytics.record_decision(
        run_id=run_id,
        vacancy=v,
        decision=decision,
        evaluation=evaluation,
        details=details,
        resume_variant=resume_variant,
        note=analytics_note,
        **extra_analytics,
    )
    create_task(
        f"Ручной отклик: {v['title']} @ {v['company']}",
        (
            f"Источник: {source_label}\n"
            f"Score: {score}/100\n"
            f"{reason}\n"
            f"URL: {v.get('url', '')}\n"
            f"Причина: {manual_note}"
        ),
        "medium",
    )
    await notify_needs_manual(v, score, reason, note=manual_note, reply_markup=reply_markup)


def _build_hh_retry_cover_letter(v: dict, resume_variant: dict | None = None) -> str:
    title = str(v.get("title") or "вакансию").strip()
    company = str(v.get("company") or "").strip()
    company_part = f" в {company}" if company else ""
    resume_title = str((resume_variant or {}).get("title") or "другой вариант резюме").strip()
    return (
        f"Здравствуйте! Направляю {resume_title} на вакансию «{title}»{company_part}. "
        "Готов обсудить опыт ручного тестирования, проверки web/API, работы с DevTools/Postman "
        "и участия в подготовке автотестов. Спасибо."
    )


def _evaluation_with_cover_letter(
    evaluation: dict,
    cover_letter: str,
    *,
    fallback: bool | None = None,
    overclaim_guard: bool | None = None,
) -> dict:
    cover_meta = analyze_cover_letter(
        cover_letter,
        cover_style=str(evaluation.get("cover_style") or ""),
        fallback=fallback,
        overclaim_guard=overclaim_guard,
    )
    return {**evaluation, **cover_meta}


def _record_hh_questionnaire_analytics(
    *,
    run_id: str,
    vacancy: dict,
    apply_result: dict,
    success: bool,
) -> None:
    question_answers = apply_result.get("question_answers") or []
    if not question_answers:
        return
    try:
        analytics.record_questionnaire(
            run_id=run_id,
            vacancy=vacancy,
            question_answers=question_answers,
            success=success,
            reason=str(apply_result.get("message") or ""),
        )
    except Exception as exc:
        log.warning("failed to record HH questionnaire analytics: %s", exc)


def _format_hh_question_answers_for_note(apply_result: dict, *, limit: int = 8) -> str:
    items = apply_result.get("question_answers") or []
    if not items:
        return ""

    def shorten(value: str, max_len: int) -> str:
        value = " ".join(str(value or "").split())
        if len(value) <= max_len:
            return value
        return value[: max_len - 3].rstrip() + "..."

    lines = ["Анкета HH заполнена:"]
    for idx, item in enumerate(items[:limit], start=1):
        question = shorten(item.get("question") or "вопрос", 140)
        answer = shorten(item.get("answer") or "—", 260)
        suffix_parts = []
        if item.get("best_guess"):
            suffix_parts.append("best-guess")
        if item.get("skipped"):
            suffix_parts.append("пропущено")
        if item.get("skip_reason"):
            suffix_parts.append(str(item.get("skip_reason")))
        suffix = f" [{' | '.join(suffix_parts)}]" if suffix_parts else ""
        lines.append(f"{idx}. {question} -> {answer}{suffix}")
    if len(items) > limit:
        lines.append(f"...ещё {len(items) - limit} ответ(ов)")
    return "\n".join(lines)


async def do_search(dry_run: bool = False) -> dict:
    """
    Один прогон поиска + откликов.
    Возвращает {"found": int, "applied": int, "skipped": int}
    """
    result: dict = {"found": 0, "applied": 0, "skipped": 0, "source_stats": {}, "note": "", "_run_id": ""}
    await notifier.notify_stale_cookies()
    hh_client: HHClient | None = HHClient() if config.HH_ENABLED else None
    superjob_client: SuperJobClient | None = SuperJobClient() if config.SUPERJOB_ENABLED else None
    habr_client: HabrCareerClient | None = HabrCareerClient() if config.HABR_ENABLED else None
    geekjob_client: GeekJobClient | None = GeekJobClient() if config.GEEKJOB_ENABLED else None
    last_office_status: tuple[str, str, str] | None = None
    runtime_mode = "dry-run" if dry_run else "search"
    run_id = analytics.new_run_id(runtime_mode)
    result["_run_id"] = run_id
    last_apply_attempt_started_at_by_source = defaultdict(float)
    hh_retry_vacancies: list[dict] = []
    hh_auto_apply_guard_note = ""

    async def set_hunter_status(action: str, message: str, status: str) -> None:
        nonlocal last_office_status
        payload = (action, message, status)
        if payload == last_office_status:
            return
        last_office_status = payload
        _write_runtime_status(action, message, status, runtime_mode, {"dry_run": dry_run})
        await office_log(action, message, status)

    async def wait_before_auto_apply(source: str, min_interval_seconds: int) -> None:
        if min_interval_seconds <= 0:
            return

        last_started_at = last_apply_attempt_started_at_by_source[source]
        if last_started_at <= 0:
            return

        now = asyncio.get_running_loop().time()
        remaining = min_interval_seconds - (now - last_started_at)
        if remaining <= 0:
            return

        wait_seconds = max(1, int(remaining) if remaining.is_integer() else int(remaining) + 1)
        await set_hunter_status(
            "search_apply_wait",
            f"Пауза {_source_label(source, short=True)} {wait_seconds}с",
            "thinking",
        )
        log.info(
            "Waiting %.1fs before next %s auto-apply attempt",
            remaining,
            source,
        )
        await asyncio.sleep(remaining)

    try:
        if hh_client is not None:
            await hh_client.start()
            try:
                if await hh_client.is_logged_in():
                    negotiation_statuses = await hh_client.get_negotiation_statuses()
                    analytics.record_negotiation_statuses(negotiation_statuses)
                    if hh_pipeline.enabled():
                        resumes = await hh_client.get_resume_ids()
                        hh_pipeline.remember_resolved_variants(
                            hh_pipeline.resolve_variants(resumes)
                        )
                        hh_pipeline.sync_negotiation_statuses(negotiation_statuses)
                        hh_retry_vacancies = hh_pipeline.get_retry_candidates()
                        if hh_retry_vacancies:
                            log.info(
                                "Prepared %d hh retry candidates for staged resumes",
                                len(hh_retry_vacancies),
                            )
            except Exception as e:
                log.warning("Failed to prepare hh staged resume pipeline: %s", e)

        await set_hunter_status("search_start", "Старт поиска", "working")
        enabled_sources = []
        if config.HH_ENABLED:
            enabled_sources.append("hh.ru")
        if config.SUPERJOB_ENABLED:
            enabled_sources.append("SuperJob")
        if config.HABR_ENABLED:
            enabled_sources.append("Хабр Карьера")
        if config.GEEKJOB_ENABLED:
            enabled_sources.append("GeekJob")

        analytics.record_search_started(
            run_id=run_id,
            mode=runtime_mode,
            enabled_sources=enabled_sources,
        )

        if not dry_run:
            await notify_search_started(enabled_sources)

        all_vacancies = await search_pipeline.collect_all(
            hh_client, superjob_client, habr_client, geekjob_client,
            hh_retry_vacancies=hh_retry_vacancies,
            source_stats=result["source_stats"],
            status_callback=set_hunter_status,
        )

        if not config.HH_ENABLED and not config.SUPERJOB_ENABLED and not config.HABR_ENABLED and not config.GEEKJOB_ENABLED:
            await set_hunter_status("search_done", "Все источники отключены", "idle")
            _record_search_run(result, dry_run=dry_run, ok=True)
            return result

        await set_hunter_status(
            "search_collect_done",
            f"Собрал {len(all_vacancies)} вакансий",
            "working",
        )

        # Дедупликация
        raw_count = len(all_vacancies)
        await set_hunter_status("search_dedupe", f"Убираю дубли {raw_count}", "thinking")
        all_vacancies = search_pipeline.deduplicate(all_vacancies)
        for vacancy in all_vacancies:
            if not vacancy.get("_hh_retry"):
                search_pipeline.get_source_bucket(result["source_stats"], vacancy)["new"] += 1

        log.info("Found %d unique vacancies before keyword filter", len(all_vacancies))
        await set_hunter_status("search_filter", f"Фильтр {len(all_vacancies)} вакансий", "thinking")

        # Keyword-фильтрация
        all_vacancies = search_pipeline.keyword_filter(all_vacancies, result["source_stats"], run_id)
        result["found"] = len(all_vacancies)

        log.info("Found %d relevant vacancies", len(all_vacancies))
        relevant_counts = {}
        for source in SOURCE_ORDER:
            enabled = (
                (source == "hh" and config.HH_ENABLED)
                or (source == "habr" and config.HABR_ENABLED)
                or (source == "geekjob" and config.GEEKJOB_ENABLED)
                or (source == "superjob" and config.SUPERJOB_ENABLED)
            )
            if enabled:
                relevant_counts[source] = result["source_stats"].get(source, {}).get("relevant", 0)
        await set_hunter_status(
            "search_results",
            f"К оценке {_format_compact_source_counts(relevant_counts)}",
            "working",
        )

        if not all_vacancies:
            result["note"] = _format_no_new_vacancies_note(result["source_stats"])
            await set_hunter_status("search_done", result["note"], "idle")
            _record_search_run(result, dry_run=dry_run, ok=True)
            return result

        if config.HH_ENABLED:
            hh_can_auto_apply, hh_guard_note = hh_guard.can_auto_apply()
            if not hh_can_auto_apply:
                hh_auto_apply_guard_note = hh_guard_note

        applied_count = 0
        auto_applied_count_by_source = defaultdict(int)
        llm_issue_alert_sent = False
        processed_by_source = defaultdict(int)
        habr_logged_in: bool | None = None
        superjob_ready: bool | None = None
        geekjob_ready: bool | None = None
        geekjob_ready_message = ""

        for v in all_vacancies:
            if (
                config.MAX_APPLICATIONS_PER_RUN > 0
                and applied_count >= config.MAX_APPLICATIONS_PER_RUN
            ):
                log.info("Reached max applications limit (%d)", config.MAX_APPLICATIONS_PER_RUN)
                break

            vid = v["id"]
            source = v.get("source", "hh")
            bucket = search_pipeline.get_source_bucket(result["source_stats"], v)
            processed_by_source[source] += 1
            source_index = processed_by_source[source]
            source_total = relevant_counts.get(source, 0)

            log.info("Evaluating [%s]: %s @ %s", source, v["title"], v["company"])
            if source_index == 1 or source_index == source_total or source_index % 5 == 0:
                await set_hunter_status(
                    "search_evaluate",
                    _format_source_progress("Проверяю", source, source_index, source_total),
                    "thinking",
                )

            # Получаем детали
            details = await apply_orchestrator.fetch_vacancy_details(
                v, hh_client, superjob_client, habr_client, geekjob_client,
            )

            if _looks_like_closed_or_archived(v, details):
                log.info("  Skipped (closed/archived vacancy)")
                evaluation = {
                    "score": 0,
                    "reason": "Вакансия закрыта или находится в архиве",
                    "red_flags": ["closed_or_archived"],
                    "should_apply": False,
                }
                seen.mark_seen(vid, v, "skipped_archived")
                if source == "hh":
                    hh_pipeline.mark_terminal(vid, "closed_or_archived")
                result["skipped"] += 1
                bucket["rejected"] += 1
                analytics.record_decision(
                    run_id=run_id,
                    vacancy=v,
                    decision=DECISION_SKIPPED_LOW_SCORE,
                    evaluation=evaluation,
                    details=details,
                    note=f"{source}:closed_or_archived",
                )
                continue

            # LLM-оценка. Retry-кандидаты уже проходили матчинг при первом отклике,
            # поэтому здесь только проверяем, что вакансия не закрыта, и пробуем
            # следующий вариант резюме в рамках staged pipeline.
            if v.get("_hh_retry"):
                retry_reason = v.get("_hh_retry_reason") or "retry"
                last_status = v.get("_hh_last_status") or "нет ответа"
                evaluation = {
                    "score": max(70, int(getattr(config, "HH_MATCHER_AUTO_APPLY_MIN_SCORE", 58) or 58)),
                    "reason": (
                        "Повторный отклик другим резюме через hh staged resume pipeline "
                        f"после статуса: {last_status} ({retry_reason})."
                    ),
                    "red_flags": [],
                    "guard_flags": ["hh_resume_retry"],
                    "should_apply": True,
                }
            else:
                evaluation = await evaluate_vacancy(v, details)
            score = evaluation.get("score", 0)
            reason = evaluation.get("reason", "")
            red_flags = evaluation.get("red_flags", [])

            log.info("  Score: %d | %s | Flags: %s", score, reason, red_flags)

            if (
                not llm_issue_alert_sent
                and score <= 0
                and evaluation.get("error_kind") in {"llm_limits_exhausted", "llm_error"}
            ):
                await notifier.notify_llm_issue(
                    v,
                    evaluation,
                    source_index=source_index,
                    source_total=source_total,
                )
                llm_issue_alert_sent = True

            if red_flags:
                log.warning("  Red flags: %s", red_flags)
                seen.mark_seen(vid, v, "skipped_red_flags")
                result["skipped"] += 1
                bucket["rejected"] += 1
                analytics.record_decision(
                    run_id=run_id,
                    vacancy=v,
                    decision=DECISION_SKIPPED_RED_FLAGS,
                    evaluation=evaluation,
                    details=details,
                )
                continue

            if not evaluation.get("should_apply", False):
                if is_manual_review_candidate(evaluation):
                    profile_name = profile_mod.active().name
                    candidate = manual_apply_queue.create_candidate(
                        v,
                        evaluation,
                        details,
                        profile_name=profile_name,
                    )
                    token = candidate.get("token", "")
                    reply_markup = manual_apply_queue.build_manual_apply_markup(v, profile_name, token)
                    if source == "hh":
                        note = (
                            "Желтая зона: вакансия не прошла автоотклик, но score достаточно высокий для ручного решения. "
                            "Можно открыть самому или нажать 'Откликнуться с ИИ'."
                        )
                    else:
                        note = (
                            "Желтая зона: вакансия не прошла автоотклик, но score достаточно высокий для ручного решения. "
                            "Для этого источника оставляю ссылку на ручную проверку."
                        )
                    log.info("  Manual review yellow-zone: token=%s", token)
                    await _mark_manual(
                        f"Ручной {_source_label(source, short=True)}: yellow-zone",
                        f"manual_yellow_{source}",
                        DECISION_MANUAL_REVIEW,
                        note,
                        v,
                        vid,
                        score,
                        reason,
                        evaluation,
                        details,
                        result,
                        bucket,
                        run_id,
                        set_hunter_status,
                        reply_markup=reply_markup,
                        analytics_note="yellow_zone",
                    )
                    continue

                log.info("  Skipped (low score)")
                seen.mark_seen(vid, v, "skipped_low_score")
                result["skipped"] += 1
                bucket["rejected"] += 1
                analytics.record_decision(
                    run_id=run_id,
                    vacancy=v,
                    decision=DECISION_SKIPPED_LOW_SCORE,
                    evaluation=evaluation,
                    details=details,
                )
                continue

            hh_resume_variant = None
            if source == "hh" and hh_pipeline.enabled():
                if v.get("_hh_resume_variant"):
                    hh_resume_variant = hh_pipeline.get_variant_by_name(v["_hh_resume_variant"])
                if hh_resume_variant is None:
                    hh_resume_variant = hh_pipeline.get_next_variant(vid, evaluation.get("cluster"))

            if dry_run:
                log.info("  [DRY RUN] Would handle: %s @ %s (score=%d)", v["title"], v["company"], score)
                seen.mark_seen(vid, v, f"dry_run_{source}")
                result["applied"] += 1
                bucket["applied"] += 1
                analytics.record_decision(
                    run_id=run_id,
                    vacancy=v,
                    decision=DECISION_DRY_RUN_MATCH,
                    evaluation=evaluation,
                    details=details,
                    dry_run=True,
                    resume_variant=hh_resume_variant,
                )
                await set_hunter_status(
                    "search_dry_run",
                    _format_source_progress("Подходит", source, source_index, source_total),
                    "working",
                )
                continue

            # ── Единый apply-flow для всех источников ──
            source_label = v.get("source_label") or _source_label(source)
            short_label = _source_label(source, short=True)

            # 1. Проверяем, включён ли автоотклик для источника
            auto_apply_enabled = apply_orchestrator.is_auto_apply_enabled(source)

            source_client = {
                "hh": hh_client,
                "superjob": superjob_client,
                "habr": habr_client,
                "geekjob": geekjob_client,
            }.get(source)

            if source == "hh":
                if not hh_auto_apply_guard_note:
                    hh_can_auto_apply, hh_guard_note = hh_guard.can_auto_apply()
                    if not hh_can_auto_apply:
                        hh_auto_apply_guard_note = hh_guard_note
                if hh_auto_apply_guard_note:
                    # hh на anti-bot/rolling-limit guard: тихо откладываем без manual-задачи и notify.
                    # Не помечаем seen → вакансия подхватится на следующем прогоне после паузы.
                    log.info(
                        "  hh deferred (%s): %s @ %s",
                        hh_auto_apply_guard_note,
                        v.get("title", ""), v.get("company", ""),
                    )
                    result["skipped"] += 1
                    bucket["deferred"] = bucket.get("deferred", 0) + 1
                    continue

            if source_client is None or not auto_apply_enabled:
                await _mark_manual(
                    f"Ручной {short_label}: выкл",
                    f"manual_{source}", f"manual_{source}_disabled",
                    f"{source_label} отключён для автоотклика.",
                    v, vid, score, reason, evaluation, details,
                    result, bucket, run_id, set_hunter_status,
                    resume_variant=hh_resume_variant,
                )
                continue

            # 2. Проверяем готовность сессии
            if source == "superjob":
                if superjob_ready is None:
                    try:
                        superjob_ready = await superjob_client.is_auto_apply_ready()
                    except Exception as e:
                        log.warning("SuperJob readiness check failed: %s", e)
                        superjob_ready = False
                if not superjob_ready:
                    await _mark_manual(
                        f"Ручной {short_label}: сессия",
                        f"manual_{source}", f"manual_{source}_session",
                        f"Нет активной сессии SuperJob. Запусти ./run.sh superjob-login.",
                        v, vid, score, reason, evaluation, details,
                        result, bucket, run_id, set_hunter_status,
                        resume_variant=hh_resume_variant,
                    )
                    continue
            elif source == "habr":
                if habr_logged_in is None:
                    try:
                        habr_logged_in = await habr_client.is_logged_in()
                    except Exception as e:
                        log.warning("Habr login check failed: %s", e)
                        habr_logged_in = False
                if not habr_logged_in:
                    await _mark_manual(
                        f"Ручной {short_label}: сессия",
                        f"manual_{source}", f"manual_{source}_session",
                        f"Нет активной сессии Хабр Карьеры. Запусти ./run.sh habr-login.",
                        v, vid, score, reason, evaluation, details,
                        result, bucket, run_id, set_hunter_status,
                        resume_variant=hh_resume_variant,
                    )
                    continue
            elif source == "geekjob":
                if geekjob_ready is None:
                    try:
                        geekjob_ready, geekjob_ready_message = await geekjob_client.is_auto_apply_ready(
                            v.get("url", "")
                        )
                    except Exception as e:
                        log.warning("GeekJob readiness check failed: %s", e)
                        geekjob_ready = False
                        geekjob_ready_message = f"Не удалось проверить GeekJob: {e}"
                if not geekjob_ready:
                    await _mark_manual(
                        f"Ручной {short_label}: сессия",
                        f"manual_{source}", f"manual_{source}_session",
                        geekjob_ready_message or "Нет активной сессии GeekJob. Запусти ./run.sh geekjob-login.",
                        v, vid, score, reason, evaluation, details,
                        result, bucket, run_id, set_hunter_status,
                        resume_variant=hh_resume_variant,
                        analytics_note=geekjob_ready_message,
                    )
                    continue

            # 3. Проверяем лимит автооткликов
            if (
                config.MAX_AUTO_APPLICATIONS_PER_SOURCE > 0
                and auto_applied_count_by_source[source] >= config.MAX_AUTO_APPLICATIONS_PER_SOURCE
            ):
                if source == "hh":
                    # Лимит по hh достигнут — активируем guard на остаток прогона:
                    # все последующие hh-вакансии тихо уйдут в deferred (без notify, без seen),
                    # подхватятся на следующем прогоне.
                    if not hh_auto_apply_guard_note:
                        hh_auto_apply_guard_note = (
                            f"hh: лимит автооткликов за прогон достигнут "
                            f"({config.MAX_AUTO_APPLICATIONS_PER_SOURCE}). "
                            "Остальные отложены до следующего прогона."
                        )
                    log.info(
                        "  hh deferred (per-run limit): %s @ %s",
                        v.get("title", ""), v.get("company", ""),
                    )
                    result["skipped"] += 1
                    bucket["deferred"] = bucket.get("deferred", 0) + 1
                    continue
                await _mark_manual(
                    f"Ручной {short_label}: лимит",
                    f"manual_{source}", f"manual_{source}_limit",
                    (
                        f"Достигнут лимит автооткликов по {source_label} "
                        f"({config.MAX_AUTO_APPLICATIONS_PER_SOURCE} за прогон)."
                    ),
                    v, vid, score, reason, evaluation, details,
                    result, bucket, run_id, set_hunter_status,
                    resume_variant=hh_resume_variant,
                )
                continue

            # 4. Генерируем cover letter
            await set_hunter_status(
                "search_apply",
                _format_source_progress("Отклик", source, source_index, source_total),
                "working",
            )
            cover_limit = apply_orchestrator.get_cover_letter_limit(source)
            cover = await generate_cover_letter(v, details)
            cover = cover or ""
            cover_fallback_used = False
            if not (cover or "").strip() and v.get("_hh_retry"):
                cover = _build_hh_retry_cover_letter(v, hh_resume_variant)
                cover_fallback_used = True
                log.info("  hh retry fallback cover letter used for %s", vid)
            if len(cover) > cover_limit:
                cover = cover[:cover_limit]
            cover_evaluation = _evaluation_with_cover_letter(
                evaluation,
                cover,
                fallback=cover_fallback_used or None,
            )
            if not (cover or "").strip():
                manual_note = (
                    "LLM не сгенерировал сопроводительное письмо; "
                    "автоотклик без текста не отправляю. Проверь вручную."
                )
                log.warning("  %s cover letter empty; auto apply blocked for %s", source_label, vid)
                await _mark_manual(
                    f"Ручной {short_label}: нет сопровода",
                    f"manual_{source}_no_cover",
                    DECISION_APPLY_FAILED,
                    manual_note,
                    v, vid, score, reason,
                    _evaluation_with_cover_letter(
                        _evaluation_with_guard_flag(evaluation, "no_cover_letter"),
                        cover,
                    ),
                    details,
                    result, bucket, run_id, set_hunter_status,
                    resume_variant=hh_resume_variant,
                    analytics_note=f"{source}:no_cover_letter",
                )
                continue
            log.info("  %s cover letter: %s", source_label, cover[:100] if cover else "(empty)")

            # 5. Пауза перед откликом
            if source == "hh":
                await wait_before_auto_apply(source, config.HH_MIN_SECONDS_BETWEEN_APPLICATIONS)
                last_apply_attempt_started_at_by_source[source] = asyncio.get_running_loop().time()
            elif source == "habr":
                await wait_before_auto_apply(source, config.HABR_MIN_SECONDS_BETWEEN_APPLICATIONS)
                last_apply_attempt_started_at_by_source[source] = asyncio.get_running_loop().time()

            # 6. Отправляем отклик
            try:
                apply_result = await apply_orchestrator.dispatch_apply(
                    v, cover,
                    hh_client, superjob_client, habr_client, geekjob_client,
                    preferred_resume_title=(hh_resume_variant or {}).get("title", ""),
                    preferred_resume_id=(hh_resume_variant or {}).get("id", ""),
                )
            except Exception as e:
                snapshot = await _save_autoapply_failure_snapshot(
                    source,
                    vid,
                    _autoapply_page_for_source(
                        source,
                        hh_client,
                        superjob_client,
                        habr_client,
                        geekjob_client,
                    ),
                )
                snapshot_hint = (
                    f"\nСнимок: {snapshot['screenshot']}"
                    if snapshot.get("screenshot")
                    else ""
                )
                if source == "hh" and _snapshot_looks_like_closed_or_archived(
                    v,
                    snapshot,
                    f"{details}\n{type(e).__name__}: {e}",
                ):
                    _mark_closed_or_archived_after_apply_attempt(
                        vid=vid,
                        vacancy=v,
                        source=source,
                        evaluation=cover_evaluation,
                        details=details,
                        resume_variant=hh_resume_variant,
                        result=result,
                        bucket=bucket,
                        run_id=run_id,
                        note=f"hh:closed_or_archived_after_exception:{type(e).__name__}",
                    )
                    await set_hunter_status(
                        "search_skip_existing",
                        "HH вакансия в архиве, ручную задачу не создаю",
                        "working",
                    )
                    log.info("  hh skipped closed/archived vacancy after apply exception for %s", vid)
                    continue
                guard_suffix = ""
                anti_bot_kind = None
                if source == "hh":
                    anti_bot_kind = hh_guard.detect_antibot_kind(f"{type(e).__name__}: {e}")
                    if anti_bot_kind:
                        hh_status = hh_guard.record_antibot(
                            kind=anti_bot_kind,
                            raw_message=f"{type(e).__name__}: {e}",
                            stage="apply_exception",
                        )
                        hh_auto_apply_guard_note = hh_guard.format_block_note(hh_status)
                        guard_suffix = f"\n{hh_auto_apply_guard_note}"
                if source == "hh" and anti_bot_kind:
                    # Captcha во время apply — guard уже включился, остальные hh-вакансии
                    # уйдут в deferred. Эту тоже тихо откладываем (без notify, без seen).
                    log.info(
                        "  hh deferred (captcha during apply): %s @ %s",
                        v.get("title", ""), v.get("company", ""),
                    )
                    result["skipped"] += 1
                    bucket["deferred"] = bucket.get("deferred", 0) + 1
                    continue
                await set_hunter_status("search_manual", f"Ручной {short_label}: ошибка", "busy")
                seen.mark_seen(vid, v, f"apply_failed_exception:{type(e).__name__}")
                result["skipped"] += 1
                bucket["manual"] += 1
                analytics.record_decision(
                    run_id=run_id,
                    vacancy=v,
                    decision=DECISION_APPLY_FAILED_EXCEPTION,
                    evaluation=cover_evaluation,
                    details=details,
                    resume_variant=hh_resume_variant,
                    note=f"{source}:{type(e).__name__}" + (f"; {hh_auto_apply_guard_note}" if guard_suffix else ""),
                )
                log.exception("  %s apply crashed for %s: %s", source_label, vid, e)
                create_task(
                    f"Ручной отклик: {v['title']} @ {v['company']}",
                    (
                        f"Источник: {source_label}\n"
                        f"Score: {score}/100\n"
                        f"{reason}\n"
                        f"URL: {v.get('url', '')}\n"
                        f"Автоотклик упал: {type(e).__name__}: {e}"
                        f"{snapshot_hint}"
                        f"{guard_suffix}"
                    ),
                    "medium",
                )
                await notify_needs_manual(
                    v, score, reason,
                    note=(
                        f"Автоотклик {source_label} упал: {type(e).__name__}. Проверь вручную."
                        + (f" {hh_auto_apply_guard_note}" if guard_suffix else "")
                        + (f" Снимок: {snapshot['screenshot']}" if snapshot.get("screenshot") else "")
                    ),
                )
                continue

            log.info("  %s apply result: %s", source_label, apply_result)
            if source == "hh":
                _record_hh_questionnaire_analytics(
                    run_id=run_id,
                    vacancy=v,
                    apply_result=apply_result,
                    success=bool(apply_result.get("ok")),
                )
            apply_notes = apply_result.get("notes") or []
            apply_note_text = "; ".join(str(item) for item in apply_notes if item)
            question_answer_note = _format_hh_question_answers_for_note(apply_result)
            if question_answer_note:
                apply_note_text = (apply_note_text + "\n\n" + question_answer_note).strip()
            if source == "hh" and v.get("_hh_retry"):
                retry_note = f"Повторный отклик: {v.get('_hh_retry_reason') or 'retry'}"
                if v.get("_hh_last_status"):
                    retry_note += f"; статус: {v.get('_hh_last_status')}"
                if hh_resume_variant and hh_resume_variant.get("title"):
                    retry_note += f"; резюме: {hh_resume_variant.get('title')}"
                apply_note_text = (apply_note_text + "\n" + retry_note).strip()
            apply_message_text = str(apply_result.get("message", ""))

            if source == "hh" and (
                apply_result.get("closed_or_archived")
                or _looks_like_closed_or_archived(v, apply_message_text)
            ):
                _mark_closed_or_archived_after_apply_attempt(
                    vid=vid,
                    vacancy=v,
                    source=source,
                    evaluation=cover_evaluation,
                    details=details,
                    resume_variant=hh_resume_variant,
                    result=result,
                    bucket=bucket,
                    run_id=run_id,
                    note=f"hh:closed_or_archived_after_apply_result:{apply_message_text or 'closed_or_archived'}",
                )
                await set_hunter_status(
                    "search_skip_existing",
                    "HH вакансия в архиве, ручную задачу не создаю",
                    "working",
                )
                log.info("  hh skipped closed/archived vacancy after apply result for %s", vid)
                continue

            # 7. hh-специфика: вопросы работодателя
            if source == "hh" and "пропускаем" in apply_result.get("message", "").lower():
                snapshot = await _save_autoapply_failure_snapshot(
                    source,
                    vid,
                    _autoapply_page_for_source(
                        source,
                        hh_client,
                        superjob_client,
                        habr_client,
                        geekjob_client,
                    ),
                )
                await set_hunter_status("search_manual", "Ручной hh: вопросы", "busy")
                seen.mark_seen(vid, v, "skipped_questions")
                result["skipped"] += 1
                bucket["manual"] += 1
                analytics.record_decision(
                    run_id=run_id,
                    vacancy=v,
                    decision=DECISION_QUESTIONS_REQUIRED,
                    evaluation=cover_evaluation,
                    details=details,
                    resume_variant=hh_resume_variant,
                )
                log.info("  Skipped: employer requires extra questions")
                await notify_needs_manual(
                    v,
                    score,
                    reason,
                    note=(
                        f"Нужен ручной отклик: работодатель добавил вопросы."
                        + (f" {apply_note_text}." if apply_note_text else "")
                        + (f" Снимок: {snapshot['screenshot']}" if snapshot.get("screenshot") else "")
                    ),
                )
                continue

            # 8. Обработка результата
            if apply_result.get("already_applied"):
                seen.mark_seen(vid, v, "already_applied")
                if source == "hh":
                    hh_pipeline.mark_terminal(vid, "already_applied")
                result["skipped"] += 1
                bucket["rejected"] += 1
                analytics.record_decision(
                    run_id=run_id,
                    vacancy=v,
                    decision=DECISION_ALREADY_APPLIED,
                    evaluation=cover_evaluation,
                    details=details,
                    resume_variant=hh_resume_variant,
                    note=f"{source}:{apply_result.get('message', 'already applied')}",
                )
                await set_hunter_status(
                    "search_skip_existing",
                    f"Уже откликался {short_label}",
                    "working",
                )
                log.info("  %s vacancy already has a response: %s", source_label, apply_result.get("message", "already applied"))
                continue

            if apply_result.get("ok"):
                seen.mark_seen(vid, v, "applied")
                if source == "hh" and hh_resume_variant is not None:
                    hh_pipeline.record_successful_apply(v, hh_resume_variant)
                if source == "hh":
                    hh_guard.record_apply_success()
                result["applied"] += 1
                applied_count += 1
                bucket["applied"] += 1
                auto_applied_count_by_source[source] += 1
                analytics.record_decision(
                    run_id=run_id,
                    vacancy=v,
                    decision=DECISION_APPLIED_AUTO,
                    evaluation=cover_evaluation,
                    details=details,
                    resume_variant=hh_resume_variant,
                    note=f"{source}:{apply_note_text}" if apply_note_text else "",
                )
                await set_hunter_status(
                    "search_apply_done",
                    f"Отправил {short_label} {auto_applied_count_by_source[source]}",
                    "working",
                )

                task_id = create_task(
                    f"Отклик: {v['title']} @ {v['company']}",
                    (
                        f"Источник: {source_label}\n"
                        f"Score: {score}/100\n"
                        f"{reason}\n"
                        f"URL: {v.get('url', '')}\n"
                        f"Cover: {cover}"
                        + (f"\nAuto-answer: {apply_note_text}" if apply_note_text else "")
                    ),
                    "low",
                )
                if task_id:
                    await task_complete(task_id, f"Отклик {source_label} отправлен (score {score})")

                await notify_application(v, score, cover, note=apply_note_text or None)
            else:
                apply_message = apply_result.get("message", "unknown")
                if source == "hh" and "не удалось подтвердить отклик" in str(apply_message).casefold():
                    seen.mark_seen(vid, v, "apply_unconfirmed_no_manual")
                    hh_pipeline.mark_terminal(vid, "apply_unconfirmed")
                    result["skipped"] += 1
                    bucket["rejected"] += 1
                    analytics.record_decision(
                        run_id=run_id,
                        vacancy=v,
                        decision=DECISION_ALREADY_APPLIED,
                        evaluation=cover_evaluation,
                        details=details,
                        resume_variant=hh_resume_variant,
                        note=f"hh:{apply_message}; suppressed_manual",
                    )
                    await set_hunter_status(
                        "search_skip_existing",
                        "HH отклик не подтверждён, ручную задачу не создаю",
                        "working",
                    )
                    log.info(
                        "  hh apply unconfirmed; suppressing manual task for %s: %s",
                        vid,
                        apply_message,
                    )
                    continue
                snapshot = await _save_autoapply_failure_snapshot(
                    source,
                    vid,
                    _autoapply_page_for_source(
                        source,
                        hh_client,
                        superjob_client,
                        habr_client,
                        geekjob_client,
                    ),
                )
                snapshot_hint = (
                    f"\nСнимок: {snapshot['screenshot']}"
                    if snapshot.get("screenshot")
                    else ""
                )
                if source == "hh" and _snapshot_looks_like_closed_or_archived(
                    v,
                    snapshot,
                    f"{details}\n{apply_message}",
                ):
                    _mark_closed_or_archived_after_apply_attempt(
                        vid=vid,
                        vacancy=v,
                        source=source,
                        evaluation=cover_evaluation,
                        details=details,
                        resume_variant=hh_resume_variant,
                        result=result,
                        bucket=bucket,
                        run_id=run_id,
                        note=f"hh:closed_or_archived_after_apply_failure_snapshot:{apply_message}",
                    )
                    await set_hunter_status(
                        "search_skip_existing",
                        "HH вакансия в архиве, ручную задачу не создаю",
                        "working",
                    )
                    log.info("  hh skipped closed/archived vacancy after apply failure snapshot for %s", vid)
                    continue
                guard_suffix = ""
                anti_bot_kind = None
                if source == "hh":
                    anti_bot_kind = apply_result.get("anti_bot_kind") or hh_guard.detect_antibot_kind(apply_message)
                    if anti_bot_kind:
                        hh_status = hh_guard.record_antibot(
                            kind=anti_bot_kind,
                            raw_message=apply_message,
                            stage="apply_result",
                        )
                        hh_auto_apply_guard_note = hh_guard.format_block_note(hh_status)
                        guard_suffix = f"\n{hh_auto_apply_guard_note}"
                # geekjob-специфика: сброс готовности при ошибке авторизации
                if source == "geekjob" and (
                    "не авториз" in apply_message.lower() or "не гик" in apply_message.lower()
                ):
                    geekjob_ready = False
                    geekjob_ready_message = apply_message

                if source == "hh" and anti_bot_kind:
                    # Captcha при отклике — guard уже включился, тихо откладываем эту вакансию.
                    log.info(
                        "  hh deferred (captcha at apply): %s @ %s",
                        v.get("title", ""), v.get("company", ""),
                    )
                    result["skipped"] += 1
                    bucket["deferred"] = bucket.get("deferred", 0) + 1
                    continue

                await set_hunter_status("search_manual", f"Ручной {short_label}: не ушёл", "busy")
                seen.mark_seen(vid, v, f"apply_failed:{apply_message}")
                result["skipped"] += 1
                bucket["manual"] += 1
                analytics.record_decision(
                    run_id=run_id,
                    vacancy=v,
                    decision=DECISION_APPLY_FAILED,
                    evaluation=cover_evaluation,
                    details=details,
                    resume_variant=hh_resume_variant,
                    note=f"{source}:{apply_message}" + (f"; {hh_auto_apply_guard_note}" if guard_suffix else ""),
                )
                log.warning("  %s apply failed: %s", source_label, apply_message)
                create_task(
                    f"Ручной отклик: {v['title']} @ {v['company']}",
                    (
                        f"Источник: {source_label}\n"
                        f"Score: {score}/100\n"
                        f"{reason}\n"
                        f"URL: {v.get('url', '')}\n"
                        f"Автоотклик не завершился: {apply_message}"
                        f"{snapshot_hint}"
                        f"{guard_suffix}"
                    ),
                    "medium",
                )
                await notify_needs_manual(
                    v, score, reason,
                    note=(
                        f"Автоотклик {source_label} не завершился: {apply_message}"
                        + (f" {hh_auto_apply_guard_note}" if guard_suffix else "")
                        + (f" Снимок: {snapshot['screenshot']}" if snapshot.get("screenshot") else "")
                    ),
                )

            # Пауза между откликами
            await asyncio.sleep(3)

        status_msg = f"Поиск завершён: найдено {result['found']}, откликов {result['applied']}, пропущено {result['skipped']}"
        await set_hunter_status("search_done", status_msg, "idle")
        await notify_summary(
            result["found"],
            result["applied"],
            result["skipped"],
            result["source_stats"],
        )
        if not dry_run and result["applied"] > 0 and config.TELEGRAM_NOTIFY_AUTO_DIGEST:
            await notify_digest(analytics.summarize())
        _record_search_run(result, dry_run=dry_run, ok=True)

        # После поиска — заодно отвечаем в hh-чатах AI-помощникам,
        # пока браузер уже открыт. Включается флагом HH_CHAT_RESPONDER_ENABLED.
        if not dry_run and config.HH_CHAT_RESPONDER_ENABLED and hh_client is not None:
            try:
                import hh_chat_responder as cr
                chat_summary = await cr.process_all(hh_client)
                log.info(
                    "chat-respond piggyback: scanned=%d with_ai=%d sent=%d skipped=%d read_failed=%d",
                    chat_summary.get("chats_scanned", 0),
                    chat_summary.get("with_ai", 0),
                    chat_summary.get("answers_sent", 0),
                    chat_summary.get("skipped", 0),
                    chat_summary.get("read_failures", 0),
                )
            except Exception as exc:
                log.warning("chat-responder failed: %s", exc)

    except Exception as e:
        log.error("Search failed: %s", e, exc_info=True)
        await set_hunter_status("error", f"Ошибка поиска: {e}", "idle")
        _record_search_run(result, dry_run=dry_run, ok=False, error=str(e))
    finally:
        if hh_client:
            await hh_client.stop()
        if superjob_client:
            await superjob_client.stop()
        if habr_client:
            await habr_client.stop()
            await habr_client.stop_browser()
        if geekjob_client:
            await geekjob_client.stop()

    return result


async def do_check_invitations():
    """Проверить приглашения."""
    client = HHClient()
    try:
        await client.start()

        if not await client.is_logged_in():
            log.error("Не залогинен!")
            return

        _write_runtime_status("check_invitations", "Проверяю инвайты", "thinking", "check")
        await office_log("check_invitations", "Проверяю инвайты", "thinking")

        sync_result = await invitation_sync.check_invitations(client)
        invitations = sync_result["invitations"]

        if invitations:
            log.info("Found %d invitations!", len(invitations))
            _write_runtime_status(
                "invitations_found",
                f"Инвайты: {len(invitations)} новых",
                "working",
                "check",
            )
            await office_log(
                "invitations_found",
                f"Инвайты: {len(invitations)} новых",
                "working",
            )

            for inv in invitations:
                create_task(
                    f"🎉 Приглашение: {inv['title']} @ {inv['company']}",
                    f"URL: {inv.get('url', '')}\nОтветить и назначить время!",
                    "urgent",
                )
                await notify_invitation(inv)
                log.info("  Invitation: %s @ %s", inv["title"], inv["company"])
        else:
            log.info("No new invitations")
            _write_runtime_status("no_invitations", "Инвайтов нет", "idle", "check")
            await office_log("no_invitations", "Инвайтов нет", "idle")

    except Exception as e:
        log.error("Invitation check failed: %s", e, exc_info=True)
        _write_runtime_status("error", f"Ошибка проверки инвайтов: {e}", "idle", "check")
    finally:
        await client.stop()


async def do_fresh_search() -> dict:
    """Lightweight HH-only scan for fresh vacancies between full search cycles."""
    if not config.HH_ENABLED or not getattr(config, "HH_FRESH_SEARCH_ENABLED", True):
        return {"found": 0, "applied": 0, "skipped": 0, "source_stats": {}, "note": "fresh hh disabled"}

    queries = [str(q).strip() for q in getattr(config, "HH_FRESH_SEARCH_QUERIES", []) if str(q).strip()]
    if not queries:
        queries = list(config.SEARCH_QUERIES)
    pages = max(1, int(getattr(config, "HH_FRESH_SEARCH_PAGES", 1) or 1))

    log.info(
        "Running fresh HH search: %d queries, %d page(s), interval=%d min",
        len(queries),
        pages,
        getattr(config, "HH_FRESH_SEARCH_INTERVAL_MIN", 0),
    )
    await office_log("fresh_search_start", f"Свежий HH: {len(queries)} запросов", "working")
    with _temporary_config_values(
        SEARCH_QUERIES=queries,
        SEARCH_PAGES=pages,
        SUPERJOB_ENABLED=False,
        HABR_ENABLED=False,
        GEEKJOB_ENABLED=False,
    ):
        result = await do_search()
    await office_log("fresh_search_done", f"Свежий HH: найдено {result.get('found', 0)}", "idle")
    return result


async def do_daemon():
    """Основной цикл демона: поиск + проверка приглашений."""
    log.info("Starting daemon mode")
    log.info("  Search interval: %d min", config.SEARCH_INTERVAL_MIN)
    log.info("  Fresh HH interval: %d min", getattr(config, "HH_FRESH_SEARCH_INTERVAL_MIN", 0))
    log.info("  Invite check interval: %d min", config.INVITE_CHECK_INTERVAL_MIN)
    runtime_control.register_current_process(
        config.DAEMON_PID_FILE,
        expected_tokens=runtime_control.AGENT_DAEMON_TOKENS,
    )
    try:
        _write_runtime_status("daemon_start", "Job Hunter запущен в режиме демона", "idle", "daemon")
        await office_log("daemon_start", "Job Hunter запущен в режиме демона", "idle")

        search_interval = config.SEARCH_INTERVAL_MIN * 60
        fresh_interval = max(0, int(getattr(config, "HH_FRESH_SEARCH_INTERVAL_MIN", 0) or 0)) * 60
        invite_interval = config.INVITE_CHECK_INTERVAL_MIN * 60

        last_search = 0
        last_fresh_search = asyncio.get_event_loop().time()
        last_invite_check = 0

        stop_event = asyncio.Event()

        def _signal_handler(*_):
            log.info("Received stop signal")
            stop_event.set()

        loop = asyncio.get_event_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, _signal_handler)

        while not stop_event.is_set():
            now = asyncio.get_event_loop().time()

            # Полный поиск
            if now - last_search >= search_interval:
                log.info("Running search cycle...")
                try:
                    await do_search()
                except Exception as e:
                    log.error("Search cycle failed: %s", e)
                last_search = asyncio.get_event_loop().time()
                last_fresh_search = last_search

            # Легкий HH fresh-поиск между полными циклами
            elif fresh_interval > 0 and now - last_fresh_search >= fresh_interval:
                log.info("Running fresh HH search cycle...")
                try:
                    await do_fresh_search()
                except Exception as e:
                    log.error("Fresh HH search cycle failed: %s", e)
                last_fresh_search = asyncio.get_event_loop().time()

            # Проверка приглашений
            if now - last_invite_check >= invite_interval:
                log.info("Running invitation check...")
                try:
                    await do_check_invitations()
                except Exception as e:
                    log.error("Invite check failed: %s", e)
                last_invite_check = asyncio.get_event_loop().time()

            # Ждём 60 секунд или до сигнала остановки
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=60)
            except TimeoutError:
                pass

        _write_runtime_status("daemon_stop", "Job Hunter остановлен", "offline", "daemon")
        await office_log("daemon_stop", "Job Hunter остановлен", "offline")
        log.info("Daemon stopped")
    finally:
        runtime_control.unregister_current_process(config.DAEMON_PID_FILE)


async def do_digest():
    """Отправить дайджест с воронкой и A/B в Telegram."""
    summary = analytics.summarize()
    reporting.print_stats()
    await notify_digest(summary)
    print("📨 Дайджест отправлен в Telegram")


async def do_stats():
    """Показать статистику."""
    reporting.print_stats()


async def do_analytics_report(days: int | None = None):
    """Показать аналитику за заданное число дней."""
    reporting.print_stats(days=days)


async def do_filter_audit(days: int | None = None):
    """Replay-аудит текущих фильтров по analytics history."""
    reporting.print_filter_audit(days=days)


def do_hh_retry_preview() -> None:
    """Показать HH retry-кандидатов без отправки откликов."""
    if not hh_pipeline.enabled():
        print("HH retry pipeline disabled or resume variants are not configured.")
        return
    candidates = hh_pipeline.get_retry_candidates()
    print(reporting.format_hh_retry_preview(candidates))


def do_hh_retry_company_guard(action: str, company: str = "") -> None:
    """Управление company denylist для HH staged resume retry."""
    company = str(company or "").strip()
    if action == "list":
        items = hh_pipeline.list_blocked_companies()
        if not items:
            print("HH retry company blocklist пуст.")
            return
        print("HH retry company blocklist:")
        for item in items:
            reason = item.get("reason") or "manual"
            created_at = item.get("created_at") or ""
            print(f"  - {item.get('company') or item.get('key')} | {reason} | {created_at}")
        return

    if not company:
        raise ValueError("company is required")
    if action == "block":
        ok = hh_pipeline.block_company_retry(company, "manual_cli")
        print(f"{'✅' if ok else '❌'} HH retry block company: {company}")
        return
    if action == "unblock":
        ok = hh_pipeline.unblock_company_retry(company)
        print(f"{'✅' if ok else '⚪'} HH retry unblock company: {company}")
        return
    raise ValueError(f"unknown retry company action: {action}")


async def do_analytics_backfill():
    """Аккуратно подтянуть исторические hh-статусы и seen-решения в аналитику."""
    run_id = analytics.new_run_id("analytics-backfill")
    seen_entries = seen.all_entries()
    seen_backfill = analytics.backfill_seen_decisions(seen_entries, run_id=run_id)

    tracked_hh_ids = set()
    tracked_hh_keys = set()
    for vacancy_id, payload in seen_entries.items():
        if ":" in vacancy_id:
            source = vacancy_id.split(":", 1)[0]
            local_id = vacancy_id.split(":", 1)[1]
        elif str(vacancy_id).isdigit():
            source = "hh"
            local_id = str(vacancy_id)
        else:
            source = "unknown"
            local_id = str(vacancy_id)

        if source != "hh":
            continue

        tracked_hh_ids.add(local_id)
        tracked_hh_keys.add(search_pipeline.vacancy_match_key(payload.get("title", ""), payload.get("company", "")))

    for vacancy_id, payload in hh_pipeline.all_entries().items():
        tracked_hh_ids.add(str(vacancy_id))
        tracked_hh_keys.add(search_pipeline.vacancy_match_key(payload.get("title", ""), payload.get("company", "")))

    client = HHClient()
    filtered_statuses = []
    filtered_invitations = []
    try:
        await client.start()

        if not await client.is_logged_in():
            print("❌ Не залогинен в hh.ru. Исторические статусы не подтянуты.")
            print(f"   Seen-backfill: {seen_backfill['added']} событий")
            return

        negotiation_statuses = await client.get_negotiation_statuses()
        filtered_statuses = [
            item
            for item in negotiation_statuses
            if (
                str(item.get("id") or "").strip() in tracked_hh_ids
                or search_pipeline.vacancy_match_key(item.get("title", ""), item.get("company", "")) in tracked_hh_keys
            )
        ]
        analytics.record_negotiation_statuses(filtered_statuses)
        if hh_pipeline.enabled():
            hh_pipeline.sync_negotiation_statuses(filtered_statuses)

        negotiations = await client.check_negotiations()
        invitations = negotiations.get("invitations", [])
        filtered_invitations = [
            item
            for item in invitations
            if (
                str(item.get("id") or "").strip() in tracked_hh_ids
                or search_pipeline.vacancy_match_key(item.get("title", ""), item.get("company", "")) in tracked_hh_keys
            )
        ]
        analytics.record_invitations(filtered_invitations)
    finally:
        await client.stop()

    print("\n🧠 Analytics backfill complete")
    print(f"  Seen decisions backfilled: {seen_backfill['added']}")
    print(f"  HH statuses matched:       {len(filtered_statuses)}")
    print(f"  HH invitations matched:   {len(filtered_invitations)}")
    if seen_backfill["by_decision"]:
        print("  Historical decisions:")
        for action, count in list(seen_backfill["by_decision"].items())[:8]:
            print(f"    {action:<28} {count:>4}")
    print()


async def main():
    parser = argparse.ArgumentParser(
        description="Job Hunter Agent — автопоиск работы на hh.ru, SuperJob, Хабр Карьере и GeekJob"
    )
    parser.add_argument(
        "--profile", default="default",
        help="Имя профиля (default = из env vars; иначе из ~/.job-hunter/profiles/<name>/profile.env)",
    )
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--login", action="store_true", help="Ручной логин (сохранение cookies)")
    group.add_argument("--google-login", action="store_true", help="Ручной логин в Google для Google Forms")
    group.add_argument("--superjob-login", action="store_true", help="Логин в SuperJob")
    group.add_argument("--habr-login", action="store_true", help="Ручной логин в Хабр Карьере")
    group.add_argument("--geekjob-login", action="store_true", help="Ручной логин в GeekJob")
    group.add_argument("--search", action="store_true", help="Один прогон поиска + откликов")
    group.add_argument("--fresh-search", action="store_true", help="Легкий HH-поиск свежих вакансий")
    group.add_argument("--check", action="store_true", help="Проверить приглашения")
    group.add_argument("--daemon", action="store_true", help="Демон: поиск + проверка в цикле")
    group.add_argument("--stats", action="store_true", help="Статистика")
    group.add_argument("--digest", action="store_true", help="Отправить дайджест в Telegram")
    group.add_argument("--analytics-backfill", action="store_true", help="Подтянуть историю в аналитику")
    group.add_argument("--analytics-report", nargs="?", const=config.ANALYTICS_RECENT_DAYS, type=int, help="Отчет аналитики за N дней")
    group.add_argument("--filter-audit", nargs="?", const=config.ANALYTICS_RECENT_DAYS, type=int, help="Replay-аудит текущих фильтров по analytics history за N дней")
    group.add_argument("--hh-retry-preview", action="store_true", help="Показать HH retry-кандидатов без откликов")
    group.add_argument("--hh-retry-block-company", metavar="COMPANY", help="Не отправлять retry-отклики в компанию")
    group.add_argument("--hh-retry-unblock-company", metavar="COMPANY", help="Убрать компанию из retry blocklist")
    group.add_argument("--hh-retry-list-blocked-companies", action="store_true", help="Показать retry blocklist компаний")
    group.add_argument("--hh-resume-boost-status", action="store_true", help="Проверить кнопку поднятия HH-резюме без клика")
    group.add_argument("--hh-resume-boost", action="store_true", help="Ручное поднятие HH-резюме")
    group.add_argument("--dry-run", action="store_true", help="Поиск без откликов")
    group.add_argument("--grab-resume", action="store_true", help="Скачать резюме с hh.ru")
    group.add_argument("--create-profile", metavar="NAME", help="Создать новый профиль")
    group.add_argument("--list-profiles", action="store_true", help="Список профилей")
    group.add_argument("--analyze-resume", action="store_true", help="Анализ резюме (LLM)")
    group.add_argument("--extract-facts", action="store_true", help="LLM извлекает структурированные факты из резюме в facts.json")
    group.add_argument("--chat-respond", action="store_true", help="Ответить на сообщения AI-помощника в чатах hh.ru (dry-run если HH_CHAT_AUTOSEND=0)")
    group.add_argument("--chat-list-candidates", action="store_true", help="Список последних входящих HH-чатов для ручного AI-ответа")
    group.add_argument("--chat-respond-one", metavar="CHAT_ID", help="Ответить в конкретном hh-чате после ручного подтверждения")
    group.add_argument("--google-form-preview", metavar="CHAT_ID", help="Подготовить заполнение Google Form из HH-чата")
    group.add_argument("--google-form-submit", metavar="TOKEN", help="Отправить ранее подготовленную Google Form по токену")
    group.add_argument("--manual-apply-token", metavar="TOKEN", help="Отправить yellow-zone отклик по Telegram token")
    parser.add_argument("--chat-message-id", default="", help="ID сообщения в hh-чате для --chat-respond-one")
    parser.add_argument("--chat-allow-suspicious", action="store_true", help="Разрешить ответ на подозрительное HR-сообщение без явного AI-маркера")
    parser.add_argument("--chat-allow-any", action="store_true", help="Для ручного запуска разрешить AI-preview по любому последнему входящему сообщению")
    parser.add_argument("--chat-force-send", action="store_true", help="Для --chat-respond-one отправить ответ сразу, без dry-run preview")
    parser.add_argument("--chat-list-limit", type=int, default=8, help="Сколько HH-чатов показать для ручного AI-ответа")
    parser.add_argument("--chat-list-max-scan", type=int, default=25, help="Сколько свежих HH-чатов просмотреть для списка ручного AI-ответа")
    parser.add_argument("--hh-resume-boost-confirm", default="", help="Слово подтверждения для --hh-resume-boost")

    args = parser.parse_args()

    # Команды управления профилями (не требуют активации)
    if args.create_profile:
        try:
            p = profile_mod.create_profile(args.create_profile)
            print(f"✅ Профиль '{p.name}' создан: {p.home_dir}")
            print(f"   Настройки: {p.home_dir}/profile.env")
            print(f"\nСледующие шаги:")
            print(f"  1. Отредактируй profile.env (запросы, источники, уведомления)")
            print(f"  2. Залогинься: ./run.sh --profile {p.name} login")
            print(f"  3. Запусти:    ./run.sh --profile {p.name} search")
        except (ValueError, FileExistsError) as e:
            print(f"❌ {e}")
            sys.exit(1)
        return

    if args.list_profiles:
        profiles = profile_mod.list_profiles()
        default_profile = (os.getenv("JOB_HUNTER_DEFAULT_PROFILE", "default") or "default").strip() or "default"
        print(f"Профили ({len(profiles)}):")
        for name in profiles:
            marker = " (активный)" if name == default_profile else ""
            p = profile_mod.load_profile(name)
            sources = []
            if p.hh.enabled: sources.append("hh")
            if p.superjob.enabled: sources.append("sj")
            if p.habr.enabled: sources.append("habr")
            if p.geekjob.enabled: sources.append("geekjob")
            print(f"  {name}{marker} — {', '.join(sources) or 'нет источников'} — {p.home_dir}")
        return

    # Активируем профиль (патчит config.* для всех модулей). One-shot chat reply
    # запускается из Telegram callback и не должен конфликтовать с daemon lock.
    if (
        args.chat_respond_one
        or args.chat_list_candidates
        or args.google_form_preview
        or args.google_form_submit
        or args.manual_apply_token
        or args.filter_audit
        or args.hh_resume_boost_status
    ):
        profile_mod.activate_no_lock(args.profile)
    else:
        profile_mod.activate(args.profile)
    _configure_logging(force=True)
    if args.profile != "default":
        log.info("Activated profile: %s", args.profile)

    try:
        if args.login:
            await do_login()
        elif args.google_login:
            await do_google_login()
        elif args.superjob_login:
            await do_superjob_login()
        elif args.habr_login:
            await do_habr_login()
        elif args.geekjob_login:
            await do_geekjob_login()
        elif args.grab_resume:
            await do_grab_resume()
        elif args.search:
            result = await do_search()
            print(f"\n✅ Найдено: {result['found']} | Откликов: {result['applied']} | Пропущено: {result['skipped']}")
            if result.get("note"):
                print(f"ℹ️ {result['note']}")
        elif args.check:
            await do_check_invitations()
        elif args.daemon:
            await do_daemon()
        elif args.stats:
            await do_stats()
        elif args.digest:
            await do_digest()
        elif args.analytics_backfill:
            await do_analytics_backfill()
        elif args.analytics_report is not None:
            await do_analytics_report(args.analytics_report)
        elif args.filter_audit is not None:
            await do_filter_audit(args.filter_audit)
        elif args.hh_retry_preview:
            do_hh_retry_preview()
        elif args.hh_retry_block_company:
            do_hh_retry_company_guard("block", args.hh_retry_block_company)
        elif args.hh_retry_unblock_company:
            do_hh_retry_company_guard("unblock", args.hh_retry_unblock_company)
        elif args.hh_retry_list_blocked_companies:
            do_hh_retry_company_guard("list")
        elif args.hh_resume_boost_status:
            await do_hh_resume_boost_status()
        elif args.hh_resume_boost:
            await do_hh_resume_boost(args.hh_resume_boost_confirm)
        elif args.extract_facts:
            import facts as facts_mod
            await facts_mod.do_extract_facts()
        elif args.chat_respond:
            import hh_chat_responder as cr
            client = HHClient()
            try:
                summary = await cr.process_all(client)
            finally:
                try:
                    await client.stop()
                except Exception:
                    pass
            print("📋 Chat-respond summary:")
            print(f"  Чатов проверено: {summary.get('chats_scanned', 0)}")
            print(f"  С AI-помощником: {summary.get('with_ai', 0)}")
            print(f"  Подозрительных HR-сообщений: {summary.get('suspicious', 0)}")
            print(f"  Уведомлений на подтверждение: {summary.get('suspicious_notified', 0)}")
            print(
                "  Google Forms: "
                f"найдено {summary.get('google_forms_found', 0)} | "
                f"preview {summary.get('google_forms_prepared', 0)} | "
                f"ошибок {summary.get('google_forms_failed', 0)}"
            )
            print(f"  Подготовлено ответов: {summary.get('answers_drafted', 0)}")
            print(f"  Отправлено: {summary.get('answers_sent', 0)}")
            print(f"  Пропущено: {summary.get('skipped', 0)}")
            print(f"  Ошибок чтения: {summary.get('read_failures', 0)}")
            for d in summary.get("details", []):
                print(f"\n  → {d.get('vacancy')} @ {d.get('company')} ({d.get('chat_id')})")
                if d.get("google_form"):
                    print(f"    Google Form: {'preview готов' if d.get('ok') else 'ошибка'}")
                    print(f"    Форма: {d.get('form_url') or '-'}")
                    if d.get("token"):
                        print(f"    Токен: {d.get('token')}")
                    if not d.get("ok"):
                        print(f"    Причина: {d.get('message') or '-'}")
                    continue
                if d.get("suspicious"):
                    print(f"    Подозрительно: {d.get('question','')[:140]}")
                    print(f"    Уведомление: {'да' if d.get('notified') else 'нет'}")
                    continue
                print(f"    AI: {d.get('question','')[:140]}")
                print(f"    Ответ: {d.get('answer','')[:140]}")
                if d.get("dry_run"):
                    print(f"    [DRY-RUN, скрин: {d.get('preview',{}).get('screenshot_path','-')}]")
                elif d.get("sent"):
                    print("    [SENT ✓]")
        elif args.chat_list_candidates:
            import hh_chat_responder as cr
            client = HHClient()
            try:
                summary = await cr.list_reply_candidates(
                    client,
                    limit=max(1, args.chat_list_limit),
                    max_scan=max(1, args.chat_list_max_scan),
                )
            finally:
                try:
                    await client.stop()
                except Exception:
                    pass
            print(json.dumps({"chat_candidates": summary}, ensure_ascii=False))
        elif args.google_form_preview:
            await google_form_commands.preview(
                args.google_form_preview,
                message_id=args.chat_message_id,
                profile_name=args.profile,
            )
        elif args.google_form_submit:
            await google_form_commands.submit(args.google_form_submit)
        elif args.manual_apply_token:
            result = await do_manual_apply_token(args.manual_apply_token)
            if not result.get("ok"):
                sys.exit(1)
        elif args.chat_respond_one:
            import hh_chat_responder as cr
            client = HHClient()
            try:
                detail = await cr.process_one(
                    client,
                    args.chat_respond_one,
                    message_id=args.chat_message_id,
                    allow_suspicious=args.chat_allow_suspicious,
                    allow_any=args.chat_allow_any,
                    dry_run=False if args.chat_force_send else True,
                    notify=True,
                )
            finally:
                try:
                    await client.stop()
                except Exception:
                    pass
            print("📋 Chat one-shot summary:")
            print(f"  Чат: {detail.get('chat_id', args.chat_respond_one)}")
            print(f"  OK: {detail.get('ok')}")
            print(f"  Сообщение: {detail.get('message', '')}")
            if detail.get('already_replied'):
                print("  Уже отвечали на это сообщение")
            if detail.get('question'):
                print(f"  Вопрос: {detail.get('question','')[:220]}")
            if detail.get('answer'):
                print(f"  Ответ: {detail.get('answer','')[:400]}")
            if detail.get('dry_run'):
                print(f"  DRY-RUN скрин: {(detail.get('preview') or {}).get('screenshot_path','-')}")
            elif detail.get('sent'):
                print("  SENT: yes")
        elif args.analyze_resume:
            import resume_analyzer
            resume_path = config.RESUME_FILE
            if not os.path.isfile(resume_path):
                print(f"❌ Резюме не найдено: {resume_path}")
                print(f"   Загрузи резюме: ./run.sh --profile {args.profile} grab-resume")
                print(f"   Или положи файл вручную: {resume_path}")
                sys.exit(1)
            print(f"Анализирую резюме: {resume_path}")
            print(f"Модель: {config.LLM_MODEL}")
            print("Это может занять 30-60 секунд...\n")
            result_text = await resume_analyzer.analyze_resume_file(resume_path)
            print(result_text)
            # Сохраняем анализ рядом с резюме
            analysis_path = resume_path.replace(".md", "_analysis.md")
            if analysis_path == resume_path:
                analysis_path = resume_path + ".analysis.md"
            with open(analysis_path, "w", encoding="utf-8") as f:
                f.write(result_text)
            print(f"\n📄 Анализ сохранён: {analysis_path}")
        elif args.dry_run:
            result = await do_search(dry_run=True)
            print(f"\n🔍 [DRY RUN] Найдено: {result['found']} | Подходящих: {result['applied']} | Отфильтровано: {result['skipped']}")
            if result.get("note"):
                print(f"ℹ️ {result['note']}")
    finally:
        await close_office_session()
        await close_notify_session()


if __name__ == "__main__":
    asyncio.run(main())
