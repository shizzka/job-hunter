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
import signal
import sys
from collections import defaultdict
from datetime import datetime

import config
import filters
import hh_resume_pipeline as hh_pipeline
import reporting
import seen
import analytics
import search_pipeline
import apply_orchestrator
import invitation_sync
from outcome import (
    DECISION_APPLIED_AUTO,
    DECISION_APPLY_FAILED,
    DECISION_APPLY_FAILED_EXCEPTION,
    DECISION_DRY_RUN_MATCH,
    DECISION_QUESTIONS_REQUIRED,
    DECISION_SKIPPED_LOW_SCORE,
    DECISION_SKIPPED_RED_FLAGS,
)
from geekjob_client import GeekJobClient
from habr_career_client import HabrCareerClient
from hh_client import HHClient
from matcher import evaluate_vacancy, generate_cover_letter
from office_bridge import office_log, create_task, task_progress, task_complete
from office_bridge import close_session as close_office_session
from notifier import (
    notify_application, notify_invitation, notify_search_started, notify_summary, notify_digest, notify_needs_manual,
    close_session as close_notify_session,
)
from superjob_client import SuperJobClient


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


logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=_build_logging_handlers(),
)
log = logging.getLogger("agent")
SOURCE_ORDER = reporting.SOURCE_ORDER
_source_label = reporting.source_label
_format_compact_source_counts = reporting.format_compact_source_counts
_format_source_progress = reporting.format_source_progress



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
        "error": error,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    _append_run_history(entry)
    analytics.record_search_finished(
        run_id=result.get("_run_id", ""),
        mode=mode,
        result={**result, "ok": ok},
    )





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

    resumes = await client.get_resume_ids()
    if not resumes:
        print("❌ Резюме не найдены на hh.ru")
        return

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


async def _mark_manual(
    status_msg: str,
    seen_action: str,
    decision: str,
    note: str,
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
        **extra_analytics,
    )
    create_task(
        f"Ручной отклик: {v['title']} @ {v['company']}",
        (
            f"Источник: {source_label}\n"
            f"Score: {score}/100\n"
            f"{reason}\n"
            f"URL: {v.get('url', '')}\n"
            f"Причина: {note}"
        ),
        "medium",
    )
    await notify_needs_manual(v, score, reason, note=note)


async def do_search(dry_run: bool = False) -> dict:
    """
    Один прогон поиска + откликов.
    Возвращает {"found": int, "applied": int, "skipped": int}
    """
    result: dict = {"found": 0, "applied": 0, "skipped": 0, "source_stats": {}, "_run_id": ""}
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
            await set_hunter_status("search_done", "Новых вакансий нет", "idle")
            _record_search_run(result, dry_run=dry_run, ok=True)
            return result

        applied_count = 0
        auto_applied_count_by_source = defaultdict(int)
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

            # LLM-оценка
            evaluation = await evaluate_vacancy(v, details)
            score = evaluation.get("score", 0)
            reason = evaluation.get("reason", "")
            red_flags = evaluation.get("red_flags", [])

            log.info("  Score: %d | %s | Flags: %s", score, reason, red_flags)

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
                    hh_resume_variant = hh_pipeline.get_next_variant(vid)

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
                        note=geekjob_ready_message,
                    )
                    continue

            # 3. Проверяем лимит автооткликов
            if (
                config.MAX_AUTO_APPLICATIONS_PER_SOURCE > 0
                and auto_applied_count_by_source[source] >= config.MAX_AUTO_APPLICATIONS_PER_SOURCE
            ):
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
            if len(cover) > cover_limit:
                cover = cover[:cover_limit]
            log.info("  %s cover letter: %s", source_label, cover[:100] if cover else "(empty)")

            # 5. Пауза перед откликом (habr)
            if source == "habr":
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
                await set_hunter_status("search_manual", f"Ручной {short_label}: ошибка", "busy")
                seen.mark_seen(vid, v, f"apply_failed_exception:{type(e).__name__}")
                result["skipped"] += 1
                bucket["manual"] += 1
                analytics.record_decision(
                    run_id=run_id,
                    vacancy=v,
                    decision=DECISION_APPLY_FAILED_EXCEPTION,
                    evaluation=evaluation,
                    details=details,
                    resume_variant=hh_resume_variant,
                    note=f"{source}:{type(e).__name__}",
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
                    ),
                    "medium",
                )
                await notify_needs_manual(
                    v, score, reason,
                    note=f"Автоотклик {source_label} упал: {type(e).__name__}. Проверь вручную.",
                )
                continue

            log.info("  %s apply result: %s", source_label, apply_result)

            # 7. hh-специфика: вопросы работодателя
            if source == "hh" and "пропускаем" in apply_result.get("message", "").lower():
                await set_hunter_status("search_manual", "Ручной hh: вопросы", "busy")
                seen.mark_seen(vid, v, "skipped_questions")
                result["skipped"] += 1
                bucket["manual"] += 1
                analytics.record_decision(
                    run_id=run_id,
                    vacancy=v,
                    decision=DECISION_QUESTIONS_REQUIRED,
                    evaluation=evaluation,
                    details=details,
                    resume_variant=hh_resume_variant,
                )
                log.info("  Skipped: employer requires extra questions")
                await notify_needs_manual(v, score, reason)
                continue

            # 8. Обработка результата
            if apply_result.get("ok"):
                seen.mark_seen(vid, v, "applied")
                if source == "hh" and hh_resume_variant is not None:
                    hh_pipeline.record_successful_apply(v, hh_resume_variant)
                result["applied"] += 1
                applied_count += 1
                bucket["applied"] += 1
                auto_applied_count_by_source[source] += 1
                analytics.record_decision(
                    run_id=run_id,
                    vacancy=v,
                    decision=DECISION_APPLIED_AUTO,
                    evaluation=evaluation,
                    details=details,
                    resume_variant=hh_resume_variant,
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
                    ),
                    "low",
                )
                if task_id:
                    await task_complete(task_id, f"Отклик {source_label} отправлен (score {score})")

                await notify_application(v, score, cover)
            else:
                apply_message = apply_result.get("message", "unknown")
                # geekjob-специфика: сброс готовности при ошибке авторизации
                if source == "geekjob" and (
                    "не авториз" in apply_message.lower() or "не гик" in apply_message.lower()
                ):
                    geekjob_ready = False
                    geekjob_ready_message = apply_message

                await set_hunter_status("search_manual", f"Ручной {short_label}: не ушёл", "busy")
                seen.mark_seen(vid, v, f"apply_failed:{apply_message}")
                result["skipped"] += 1
                bucket["manual"] += 1
                analytics.record_decision(
                    run_id=run_id,
                    vacancy=v,
                    decision=DECISION_APPLY_FAILED,
                    evaluation=evaluation,
                    details=details,
                    resume_variant=hh_resume_variant,
                    note=f"{source}:{apply_message}",
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
                    ),
                    "medium",
                )
                await notify_needs_manual(
                    v, score, reason,
                    note=f"Автоотклик {source_label} не завершился: {apply_message}",
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
        if not dry_run and result["applied"] > 0:
            await notify_digest(analytics.summarize())
        _record_search_run(result, dry_run=dry_run, ok=True)

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


async def do_daemon():
    """Основной цикл демона: поиск + проверка приглашений."""
    log.info("Starting daemon mode")
    log.info("  Search interval: %d min", config.SEARCH_INTERVAL_MIN)
    log.info("  Invite check interval: %d min", config.INVITE_CHECK_INTERVAL_MIN)

    _write_runtime_status("daemon_start", "Job Hunter запущен в режиме демона", "idle", "daemon")
    await office_log("daemon_start", "Job Hunter запущен в режиме демона", "idle")

    search_interval = config.SEARCH_INTERVAL_MIN * 60
    invite_interval = config.INVITE_CHECK_INTERVAL_MIN * 60

    last_search = 0
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

        # Поиск
        if now - last_search >= search_interval:
            log.info("Running search cycle...")
            try:
                await do_search()
            except Exception as e:
                log.error("Search cycle failed: %s", e)
            last_search = asyncio.get_event_loop().time()

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


async def do_digest():
    """Отправить дайджест с воронкой и A/B в Telegram."""
    summary = analytics.summarize()
    reporting.print_stats()
    await notify_digest(summary)
    print("📨 Дайджест отправлен в Telegram")


async def do_stats():
    """Показать статистику."""
    reporting.print_stats()


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
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--login", action="store_true", help="Ручной логин (сохранение cookies)")
    group.add_argument("--superjob-login", action="store_true", help="Логин в SuperJob")
    group.add_argument("--habr-login", action="store_true", help="Ручной логин в Хабр Карьере")
    group.add_argument("--geekjob-login", action="store_true", help="Ручной логин в GeekJob")
    group.add_argument("--search", action="store_true", help="Один прогон поиска + откликов")
    group.add_argument("--check", action="store_true", help="Проверить приглашения")
    group.add_argument("--daemon", action="store_true", help="Демон: поиск + проверка в цикле")
    group.add_argument("--stats", action="store_true", help="Статистика")
    group.add_argument("--digest", action="store_true", help="Отправить дайджест в Telegram")
    group.add_argument("--analytics-backfill", action="store_true", help="Подтянуть историю в аналитику")
    group.add_argument("--dry-run", action="store_true", help="Поиск без откликов")
    group.add_argument("--grab-resume", action="store_true", help="Скачать резюме с hh.ru")

    args = parser.parse_args()

    try:
        if args.login:
            await do_login()
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
        elif args.dry_run:
            result = await do_search(dry_run=True)
            print(f"\n🔍 [DRY RUN] Найдено: {result['found']} | Подходящих: {result['applied']} | Отфильтровано: {result['skipped']}")
    finally:
        await close_office_session()
        await close_notify_session()


if __name__ == "__main__":
    asyncio.run(main())
