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
from datetime import datetime

import config
import hh_resume_pipeline as hh_pipeline
import seen
import analytics
from geekjob_client import GeekJobClient
from habr_career_client import HabrCareerClient
from hh_client import HHClient
from matcher import evaluate_vacancy, generate_cover_letter
from office_bridge import office_log, create_task, task_progress, task_complete
from office_bridge import close_session as close_office_session
from notifier import (
    notify_application, notify_invitation, notify_search_started, notify_summary, notify_needs_manual,
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
SOURCE_ORDER = ("hh", "habr", "geekjob", "superjob")
SOURCE_LABELS = {
    "hh": "hh.ru",
    "habr": "Хабр",
    "geekjob": "GeekJob",
    "superjob": "SuperJob",
    "unknown": "unknown",
}
SOURCE_SHORT_LABELS = {
    "hh": "hh",
    "habr": "Хабр",
    "geekjob": "GJ",
    "superjob": "SJ",
    "unknown": "?",
}


def _source_label(source: str, short: bool = False) -> str:
    labels = SOURCE_SHORT_LABELS if short else SOURCE_LABELS
    return labels.get(source, source)


def _format_compact_source_counts(counts: dict[str, int]) -> str:
    parts = []
    for source in SOURCE_ORDER:
        if source not in counts:
            continue
        parts.append(f"{_source_label(source, short=True)} {counts[source]}")
    return " ".join(parts) or "0"


def _format_source_progress(prefix: str, source: str, current: int, total: int) -> str:
    label = _source_label(source, short=True)
    if total > 0:
        return f"{prefix} {label} {current}/{total}"
    return f"{prefix} {label}"


def _vacancy_dedupe_key(vacancy: dict) -> str:
    url = (vacancy.get("url") or "").split("?", 1)[0].strip().casefold()
    title = re.sub(r"\s+", " ", vacancy.get("title", "").casefold()).strip()
    company = re.sub(r"\s+", " ", vacancy.get("company", "").casefold()).strip()
    location = re.sub(r"\s+", " ", vacancy.get("location", "").casefold()).strip()
    if title and company:
        return f"{title}|{company}|{location}"
    if url:
        return url
    return vacancy.get("id", "")


def _normalize_match_value(value: str) -> str:
    return " ".join((value or "").casefold().split())


def _vacancy_match_key(title: str, company: str) -> str:
    return f"{_normalize_match_value(title)}|{_normalize_match_value(company)}"


def _get_source_bucket(stats: dict, vacancy: dict) -> dict:
    source = vacancy.get("source") or "unknown"
    label = vacancy.get("source_label") or source
    bucket = stats.setdefault(
        source,
        {
            "label": label,
            "new": 0,
            "relevant": 0,
            "applied": 0,
            "manual": 0,
            "rejected": 0,
        },
    )
    if not bucket.get("label"):
        bucket["label"] = label
    return bucket


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
    entry = {
        "kind": "search",
        "ok": ok,
        "mode": "dry-run" if dry_run else "search",
        "found": result.get("found", 0),
        "applied": result.get("applied", 0),
        "skipped": result.get("skipped", 0),
        "source_stats": result.get("source_stats", {}),
        "error": error,
        "created_at": datetime.now().isoformat(timespec="seconds"),
    }
    _append_run_history(entry)


def _load_recent_run_history(limit: int = 5) -> list[dict]:
    if limit <= 0 or not os.path.exists(config.RUN_HISTORY_FILE):
        return []

    try:
        with open(config.RUN_HISTORY_FILE, encoding="utf-8") as f:
            lines = [line.strip() for line in f if line.strip()]
    except Exception as exc:
        log.warning("Failed to read run history: %s", exc)
        return []

    items = []
    for line in lines[-limit:]:
        try:
            items.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return list(reversed(items))


def _format_stats_source_breakdown(by_source: dict) -> list[str]:
    lines = []
    for source in SOURCE_ORDER:
        bucket = by_source.get(source)
        if not bucket:
            continue
        lines.append(
            "  "
            f"{_source_label(source):<10} total {bucket.get('total', 0):>4} | "
            f"applied {bucket.get('applied', 0):>3} | "
            f"manual {bucket.get('manual', 0):>3} | "
            f"skipped {bucket.get('skipped', 0):>3}"
        )

    for source, bucket in by_source.items():
        if source in SOURCE_ORDER:
            continue
        lines.append(
            "  "
            f"{source:<10} total {bucket.get('total', 0):>4} | "
            f"applied {bucket.get('applied', 0):>3} | "
            f"manual {bucket.get('manual', 0):>3} | "
            f"skipped {bucket.get('skipped', 0):>3}"
        )

    return lines


def _format_run_source_stats(source_stats: dict) -> str:
    if not source_stats:
        return "-"

    parts = []
    for source in SOURCE_ORDER:
        bucket = source_stats.get(source)
        if not bucket:
            continue
        parts.append(
            f"{_source_label(source, short=True)} "
            f"new {bucket.get('new', 0)}"
            f"/rel {bucket.get('relevant', 0)}"
            f"/app {bucket.get('applied', 0)}"
            f"/man {bucket.get('manual', 0)}"
        )
    return "; ".join(parts) or "-"


def _format_analytics_source_breakdown(by_source: dict) -> list[str]:
    lines = []
    for source in SOURCE_ORDER:
        bucket = by_source.get(source)
        if not bucket:
            continue
        lines.append(
            "  "
            f"{_source_label(source):<10} dec {bucket.get('decisions', 0):>4} | "
            f"auto {bucket.get('auto_applied', 0):>3} | "
            f"manual {bucket.get('manual', 0):>3} | "
            f"pos {bucket.get('positive', 0):>3} | "
            f"rej {bucket.get('rejected', 0):>3}"
        )

    for source, bucket in by_source.items():
        if source in SOURCE_ORDER:
            continue
        lines.append(
            "  "
            f"{source:<10} dec {bucket.get('decisions', 0):>4} | "
            f"auto {bucket.get('auto_applied', 0):>3} | "
            f"manual {bucket.get('manual', 0):>3} | "
            f"pos {bucket.get('positive', 0):>3} | "
            f"rej {bucket.get('rejected', 0):>3}"
        )
    return lines


def _format_top_query_breakdown(by_query: dict, limit: int = 5) -> list[str]:
    items = sorted(
        by_query.items(),
        key=lambda item: (
            -item[1].get("positive", 0),
            -item[1].get("auto_applied", 0),
            -item[1].get("decisions", 0),
            item[0],
        ),
    )[:limit]
    return [
        "  "
        f"{query[:42]:<42} | dec {bucket.get('decisions', 0):>3} | "
        f"auto {bucket.get('auto_applied', 0):>3} | "
        f"pos {bucket.get('positive', 0):>3} | "
        f"rej {bucket.get('rejected', 0):>3}"
        for query, bucket in items
    ]


def _format_resume_variant_breakdown(by_resume_variant: dict) -> list[str]:
    items = sorted(
        by_resume_variant.items(),
        key=lambda item: (
            -item[1].get("positive", 0),
            -item[1].get("applications", 0),
            item[0],
        ),
    )
    return [
        "  "
        f"{variant:<12} app {bucket.get('applications', 0):>3} | "
        f"pos {bucket.get('positive', 0):>3} | "
        f"rej {bucket.get('rejected', 0):>3}"
        for variant, bucket in items
    ]


async def _collect_hh_vacancies(client: HHClient | None) -> list[dict]:
    if client is None:
        return []
    if not config.HH_ENABLED:
        log.info("HH_ENABLED=0, skipping hh.ru source")
        return []

    if not await client.is_logged_in():
        log.warning("hh.ru is not logged in, skipping hh source")
        await office_log("hh_skipped", "hh.ru пропущен: нет авторизации", "thinking")
        return []

    all_vacancies = []
    for profile in config.SEARCH_PROFILES:
        area = profile["area"]
        schedule = profile.get("schedule", "")
        area_label = f"area={area}" + (f",schedule={schedule}" if schedule else "")
        for query in config.SEARCH_QUERIES:
            for page_num in range(config.SEARCH_PAGES):
                log.info("HH search: %s [%s] page %d", query, area_label, page_num)
                vacancies = await client.search_vacancies(
                    query, page=page_num, area=area, schedule=schedule,
                )

                new_on_page = 0
                for vacancy in vacancies:
                    vid = vacancy.get("id")
                    if not vid or seen.is_seen(vid):
                        continue
                    vacancy["source"] = "hh"
                    vacancy["source_label"] = "hh.ru"
                    vacancy["apply_mode"] = "auto"
                    vacancy["_search_query"] = query
                    vacancy["_search_profile"] = area_label
                    all_vacancies.append(vacancy)
                    new_on_page += 1

                if len(vacancies) < 10 or new_on_page == 0:
                    break

    return all_vacancies


async def _collect_superjob_vacancies(client: SuperJobClient | None) -> list[dict]:
    if client is None:
        return []
    if not config.SUPERJOB_ENABLED:
        return []
    if not config.SUPERJOB_API_KEY:
        log.info("SUPERJOB_API_KEY is not configured, skipping SuperJob source")
        return []

    all_vacancies = []
    for profile in config.SUPERJOB_SEARCH_PROFILES:
        profile_label = profile.get("label", "default")
        for query in config.SUPERJOB_SEARCH_QUERIES:
            for page_num in range(config.SUPERJOB_SEARCH_PAGES):
                log.info("SuperJob search: %s [%s] page %d", query, profile_label, page_num)
                vacancies, more = await client.search_vacancies(query, page=page_num, profile=profile)

                new_on_page = 0
                for vacancy in vacancies:
                    vid = vacancy.get("id")
                    if not vid or seen.is_seen(vid):
                        continue
                    vacancy["_search_query"] = query
                    vacancy["_search_profile"] = profile_label
                    all_vacancies.append(vacancy)
                    new_on_page += 1

                if not vacancies or not more or new_on_page == 0:
                    break

    return all_vacancies


async def _collect_habr_vacancies(client: HabrCareerClient | None) -> list[dict]:
    if client is None:
        return []
    if not config.HABR_ENABLED:
        return []

    all_vacancies = []
    for path in config.HABR_SEARCH_PATHS:
        total_pages = 1
        for page_num in range(1, config.HABR_SEARCH_PAGES + 1):
            if page_num > total_pages:
                break

            log.info("Habr Career search: %s page %d", path, page_num)
            vacancies, total_pages = await client.search_vacancies(path=path, page=page_num)

            new_on_page = 0
            for vacancy in vacancies:
                vid = vacancy.get("id")
                if not vid or seen.is_seen(vid):
                    continue
                vacancy["_search_path"] = path
                vacancy["_search_profile"] = path
                all_vacancies.append(vacancy)
                new_on_page += 1

            if not vacancies or new_on_page == 0:
                break

    return all_vacancies


async def _collect_geekjob_vacancies(client: GeekJobClient | None) -> list[dict]:
    if client is None:
        return []
    if not config.GEEKJOB_ENABLED:
        return []

    all_vacancies = []
    total_pages = 1
    for page_num in range(1, config.GEEKJOB_SEARCH_PAGES + 1):
        if page_num > total_pages:
            break

        log.info("GeekJob search: page %d", page_num)
        vacancies, total_pages = await client.search_vacancies(page=page_num)

        new_on_page = 0
        for vacancy in vacancies:
            vid = vacancy.get("id")
            if not vid or seen.is_seen(vid):
                continue
            vacancy["_search_profile"] = f"page={page_num}"
            all_vacancies.append(vacancy)
            new_on_page += 1

        if not vacancies or new_on_page == 0:
            break

    return all_vacancies


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


async def do_search(dry_run: bool = False) -> dict:
    """
    Один прогон поиска + откликов.
    Возвращает {"found": int, "applied": int, "skipped": int}
    """
    result = {"found": 0, "applied": 0, "skipped": 0, "source_stats": {}}
    hh_client: HHClient | None = HHClient() if config.HH_ENABLED else None
    superjob_client: SuperJobClient | None = SuperJobClient() if config.SUPERJOB_ENABLED else None
    habr_client: HabrCareerClient | None = HabrCareerClient() if config.HABR_ENABLED else None
    geekjob_client: GeekJobClient | None = GeekJobClient() if config.GEEKJOB_ENABLED else None
    last_office_status: tuple[str, str, str] | None = None
    runtime_mode = "dry-run" if dry_run else "search"
    run_id = analytics.new_run_id(runtime_mode)
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
        if not dry_run:
            enabled_sources = []
            if config.HH_ENABLED:
                enabled_sources.append("hh.ru")
            if config.SUPERJOB_ENABLED:
                enabled_sources.append("SuperJob")
            if config.HABR_ENABLED:
                enabled_sources.append("Хабр Карьера")
            if config.GEEKJOB_ENABLED:
                enabled_sources.append("GeekJob")
            await notify_search_started(enabled_sources)

        if config.HH_ENABLED:
            await set_hunter_status("search_collect", "Собираю hh.ru", "working")
        hh_vacancies = await _collect_hh_vacancies(hh_client)

        if config.SUPERJOB_ENABLED:
            await set_hunter_status("search_collect", "Собираю SuperJob", "working")
        superjob_vacancies = await _collect_superjob_vacancies(superjob_client)

        if config.HABR_ENABLED:
            await set_hunter_status("search_collect", "Собираю Хабр", "working")
        habr_vacancies = await _collect_habr_vacancies(habr_client)

        if config.GEEKJOB_ENABLED:
            await set_hunter_status("search_collect", "Собираю GeekJob", "working")
        geekjob_vacancies = await _collect_geekjob_vacancies(geekjob_client)
        all_vacancies = (
            hh_vacancies
            + hh_retry_vacancies
            + superjob_vacancies
            + habr_vacancies
            + geekjob_vacancies
        )

        if not config.HH_ENABLED and not config.SUPERJOB_ENABLED and not config.HABR_ENABLED and not config.GEEKJOB_ENABLED:
            await set_hunter_status("search_done", "Все источники отключены", "idle")
            _record_search_run(result, dry_run=dry_run, ok=True)
            return result

        collected_counts = {}
        if config.HH_ENABLED:
            collected_counts["hh"] = len(hh_vacancies) + len(hh_retry_vacancies)
        if config.HABR_ENABLED:
            collected_counts["habr"] = len(habr_vacancies)
        if config.GEEKJOB_ENABLED:
            collected_counts["geekjob"] = len(geekjob_vacancies)
        if config.SUPERJOB_ENABLED:
            collected_counts["superjob"] = len(superjob_vacancies)
        await set_hunter_status(
            "search_collect_done",
            f"Собрал {_format_compact_source_counts(collected_counts)}",
            "working",
        )

        # Дедупликация между источниками по нормализованному ключу
        raw_count = len(all_vacancies)
        await set_hunter_status("search_dedupe", f"Убираю дубли {raw_count}", "thinking")
        unique = {}
        for vacancy in all_vacancies:
            dedupe_key = _vacancy_dedupe_key(vacancy)
            existing = unique.get(dedupe_key)
            if existing is None:
                unique[dedupe_key] = vacancy
                continue
            if existing.get("source") != "hh" and vacancy.get("source") == "hh":
                unique[dedupe_key] = vacancy
        all_vacancies = list(unique.values())
        for vacancy in all_vacancies:
            if not vacancy.get("_hh_retry"):
                _get_source_bucket(result["source_stats"], vacancy)["new"] += 1

        log.info(
            "Found %d unique vacancies before keyword filter (hh=%d, superjob=%d, habr=%d, geekjob=%d)",
            len(all_vacancies),
            len(hh_vacancies),
            len(superjob_vacancies),
            len(habr_vacancies),
            len(geekjob_vacancies),
        )
        await set_hunter_status("search_filter", f"Фильтр {len(all_vacancies)} вакансий", "thinking")

        # Быстрый фильтр по ключевым словам — отсеиваем явно нерелевантные
        # до LLM (экономит время и запросы)
        RELEVANT_KEYWORDS = {
            "тестиров", "qa", "quality", "тест ", "test",
            "автоматиз", "ручн", "manual", "sdet",
        }
        SUPERJOB_TITLE_KEYWORDS = {
            "тест",
            "qa",
            "quality engineer",
            "quality assurance",
        }
        SUPERJOB_QUALITY_TITLE_KEYWORDS = {"качеств"}
        SUPERJOB_IT_CONTEXT_KEYWORDS = {
            "программ",
            "software",
            "qa",
            "тест",
            "api",
            "web",
            "веб",
            "прилож",
            "frontend",
            "backend",
            "mobile",
            "автоматиз",
            "manual",
            "selenium",
            "postman",
            "sql",
        }
        EXCLUDE_KEYWORDS = {
            "директор магазин", "продавец", "кассир", "менеджер по продажам",
            "бухгалтер", "повар", "водитель", "курьер", "охранник",
            "уборщ", "грузчик", "кладовщик",
        }
        filtered = []
        for v in all_vacancies:
            bucket = _get_source_bucket(result["source_stats"], v)
            title_lower = v["title"].lower()
            snippet_lower = v.get("snippet", "").lower()
            combined = title_lower + " " + snippet_lower

            # Сначала проверяем исключения
            if any(ex in combined for ex in EXCLUDE_KEYWORDS):
                seen.mark_seen(v["id"], v, "skipped_keyword_filter")
                analytics.record_decision(
                    run_id=run_id,
                    vacancy=v,
                    decision="skipped_keyword_filter",
                    note="exclude_keywords",
                )
                continue

            if v.get("source") == "superjob":
                # У SuperJob релевантность в сниппете часто ложноположительная,
                # поэтому по умолчанию опираемся на заголовок вакансии.
                # Для "инженер по качеству" требуем явный IT-контекст.
                if any(kw in title_lower for kw in SUPERJOB_TITLE_KEYWORDS):
                    bucket["relevant"] += 1
                    filtered.append(v)
                elif (
                    any(kw in title_lower for kw in SUPERJOB_QUALITY_TITLE_KEYWORDS)
                    and any(kw in combined for kw in SUPERJOB_IT_CONTEXT_KEYWORDS)
                ):
                    bucket["relevant"] += 1
                    filtered.append(v)
                else:
                    seen.mark_seen(v["id"], v, "skipped_keyword_filter")
                    analytics.record_decision(
                        run_id=run_id,
                        vacancy=v,
                        decision="skipped_keyword_filter",
                        note="superjob_title_filter",
                    )
                continue

            # Потом проверяем релевантность
            if any(kw in combined for kw in RELEVANT_KEYWORDS):
                bucket["relevant"] += 1
                filtered.append(v)
            else:
                # Не содержит ключевых слов QA/тест — пропускаем
                seen.mark_seen(v["id"], v, "skipped_keyword_filter")
                analytics.record_decision(
                    run_id=run_id,
                    vacancy=v,
                    decision="skipped_keyword_filter",
                    note="relevant_keywords",
                )

        log.info("After keyword filter: %d → %d vacancies", len(all_vacancies), len(filtered))
        all_vacancies = filtered
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
            bucket = _get_source_bucket(result["source_stats"], v)
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
            details = v.get("details", "")
            if source == "hh" and hh_client is not None and v.get("url"):
                try:
                    details = await hh_client.get_vacancy_details(v["url"])
                except Exception as e:
                    log.warning("Failed to get details for %s: %s", vid, e)
            elif source == "habr" and habr_client is not None and v.get("url"):
                try:
                    details = await habr_client.get_vacancy_details(v["url"])
                except Exception as e:
                    log.warning("Failed to get Habr details for %s: %s", vid, e)
            elif source == "geekjob" and geekjob_client is not None and v.get("url"):
                try:
                    details = await geekjob_client.get_vacancy_details(v["url"])
                except Exception as e:
                    log.warning("Failed to get GeekJob details for %s: %s", vid, e)

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
                    decision="skipped_red_flags",
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
                    decision="skipped_low_score",
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
                    decision="dry_run_match",
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

            if source == "superjob":
                if superjob_client is None or not config.SUPERJOB_AUTO_APPLY:
                    await set_hunter_status("search_manual", "Ручной SJ: выкл", "busy")
                    seen.mark_seen(vid, v, "manual_superjob")
                    result["skipped"] += 1
                    bucket["manual"] += 1
                    analytics.record_decision(
                        run_id=run_id,
                        vacancy=v,
                        decision="manual_superjob_disabled",
                        evaluation=evaluation,
                        details=details,
                    )
                    create_task(
                        f"Ручной отклик: {v['title']} @ {v['company']}",
                        (
                            f"Источник: SuperJob\n"
                            f"Score: {score}/100\n"
                            f"{reason}\n"
                            f"URL: {v.get('url', '')}"
                        ),
                        "medium",
                    )
                    await notify_needs_manual(
                        v,
                        score,
                        reason,
                        note="SuperJob отключён для автоотклика.",
                    )
                    continue

                if superjob_ready is None:
                    try:
                        superjob_ready = await superjob_client.is_auto_apply_ready()
                    except Exception as e:
                        log.warning("SuperJob readiness check failed before apply: %s", e)
                        superjob_ready = False

                if not superjob_ready:
                    await set_hunter_status("search_manual", "Ручной SJ: сессия", "busy")
                    seen.mark_seen(vid, v, "manual_superjob")
                    result["skipped"] += 1
                    bucket["manual"] += 1
                    analytics.record_decision(
                        run_id=run_id,
                        vacancy=v,
                        decision="manual_superjob_session",
                        evaluation=evaluation,
                        details=details,
                    )
                    create_task(
                        f"Ручной отклик: {v['title']} @ {v['company']}",
                        (
                            f"Источник: SuperJob\n"
                            f"Score: {score}/100\n"
                            f"{reason}\n"
                            f"URL: {v.get('url', '')}\n"
                            "Причина: нет активной сессии SuperJob или не выбрано резюме"
                        ),
                        "medium",
                    )
                    await notify_needs_manual(
                        v,
                        score,
                        reason,
                        note="Нет активной сессии SuperJob. Запусти ./run.sh superjob-login.",
                    )
                    continue

                if (
                    config.MAX_AUTO_APPLICATIONS_PER_SOURCE > 0
                    and auto_applied_count_by_source[source] >= config.MAX_AUTO_APPLICATIONS_PER_SOURCE
                ):
                    await set_hunter_status("search_manual", "Ручной SJ: лимит", "busy")
                    seen.mark_seen(vid, v, "manual_superjob")
                    result["skipped"] += 1
                    bucket["manual"] += 1
                    analytics.record_decision(
                        run_id=run_id,
                        vacancy=v,
                        decision="manual_superjob_limit",
                        evaluation=evaluation,
                        details=details,
                    )
                    create_task(
                        f"Ручной отклик: {v['title']} @ {v['company']}",
                        (
                            f"Источник: SuperJob\n"
                            f"Score: {score}/100\n"
                            f"{reason}\n"
                            f"URL: {v.get('url', '')}\n"
                            f"Причина: достигнут лимит автооткликов ({config.MAX_AUTO_APPLICATIONS_PER_SOURCE})"
                        ),
                        "medium",
                    )
                    await notify_needs_manual(
                        v,
                        score,
                        reason,
                        note=(
                            "Достигнут лимит автооткликов по SuperJob "
                            f"({config.MAX_AUTO_APPLICATIONS_PER_SOURCE} за прогон)."
                        ),
                    )
                    continue

                await set_hunter_status(
                    "search_apply",
                    _format_source_progress("Отклик", source, source_index, source_total),
                    "working",
                )
                cover = await generate_cover_letter(v, details)
                if len(cover) > 1900:
                    cover = cover[:1900]
                log.info("  SuperJob cover letter: %s", cover[:100] if cover else "(empty)")

                try:
                    apply_result = await superjob_client.apply_to_vacancy(v, cover)
                except Exception as e:
                    await set_hunter_status("search_manual", "Ручной SJ: ошибка", "busy")
                    seen.mark_seen(vid, v, f"apply_failed_exception:{type(e).__name__}")
                    result["skipped"] += 1
                    bucket["manual"] += 1
                    analytics.record_decision(
                        run_id=run_id,
                        vacancy=v,
                        decision="apply_failed_exception",
                        evaluation=evaluation,
                        details=details,
                        note=f"superjob:{type(e).__name__}",
                    )
                    log.exception("  SuperJob apply crashed for %s: %s", vid, e)
                    create_task(
                        f"Ручной отклик: {v['title']} @ {v['company']}",
                        (
                            f"Источник: SuperJob\n"
                            f"Score: {score}/100\n"
                            f"{reason}\n"
                            f"URL: {v.get('url', '')}\n"
                            f"Автоотклик упал: {type(e).__name__}: {e}"
                        ),
                        "medium",
                    )
                    await notify_needs_manual(
                        v,
                        score,
                        reason,
                        note=f"Автоотклик SuperJob упал: {type(e).__name__}. Проверь вручную.",
                    )
                    continue

                log.info("  SuperJob apply result: %s", apply_result)
                if apply_result.get("ok"):
                    seen.mark_seen(vid, v, "applied")
                    result["applied"] += 1
                    applied_count += 1
                    bucket["applied"] += 1
                    auto_applied_count_by_source[source] += 1
                    analytics.record_decision(
                        run_id=run_id,
                        vacancy=v,
                        decision="applied_auto",
                        evaluation=evaluation,
                        details=details,
                    )
                    await set_hunter_status(
                        "search_apply_done",
                        f"Отправил SJ {auto_applied_count_by_source[source]}",
                        "working",
                    )

                    task_id = create_task(
                        f"Отклик: {v['title']} @ {v['company']}",
                        (
                            f"Источник: SuperJob\n"
                            f"Score: {score}/100\n"
                            f"{reason}\n"
                            f"URL: {v.get('url', '')}\n"
                            f"Cover: {cover}"
                        ),
                        "low",
                    )
                    if task_id:
                        await task_complete(task_id, f"Отклик SuperJob отправлен (score {score})")

                    await notify_application(v, score, cover)
                else:
                    await set_hunter_status("search_manual", "Ручной SJ: не ушёл", "busy")
                    seen.mark_seen(vid, v, f"apply_failed:{apply_result.get('message', 'unknown')}")
                    result["skipped"] += 1
                    bucket["manual"] += 1
                    analytics.record_decision(
                        run_id=run_id,
                        vacancy=v,
                        decision="apply_failed",
                        evaluation=evaluation,
                        details=details,
                        note=f"superjob:{apply_result.get('message', 'unknown')}",
                    )
                    create_task(
                        f"Ручной отклик: {v['title']} @ {v['company']}",
                        (
                            f"Источник: SuperJob\n"
                            f"Score: {score}/100\n"
                            f"{reason}\n"
                            f"URL: {v.get('url', '')}\n"
                            f"Автоотклик не завершился: {apply_result.get('message', 'unknown')}"
                        ),
                        "medium",
                    )
                    await notify_needs_manual(
                        v,
                        score,
                        reason,
                        note=f"Автоотклик SuperJob не завершился: {apply_result.get('message', 'unknown')}",
                    )
                continue

            if source == "geekjob":
                if geekjob_client is None or not config.GEEKJOB_AUTO_APPLY:
                    await set_hunter_status("search_manual", "Ручной GJ: выкл", "busy")
                    seen.mark_seen(vid, v, "manual_geekjob")
                    result["skipped"] += 1
                    bucket["manual"] += 1
                    analytics.record_decision(
                        run_id=run_id,
                        vacancy=v,
                        decision="manual_geekjob_disabled",
                        evaluation=evaluation,
                        details=details,
                    )
                    create_task(
                        f"Ручной отклик: {v['title']} @ {v['company']}",
                        (
                            f"Источник: GeekJob\n"
                            f"Score: {score}/100\n"
                            f"{reason}\n"
                            f"URL: {v.get('url', '')}"
                        ),
                        "medium",
                    )
                    await notify_needs_manual(
                        v,
                        score,
                        reason,
                        note="GeekJob отключён для автоотклика.",
                    )
                    continue

                if geekjob_ready is None:
                    try:
                        geekjob_ready, geekjob_ready_message = await geekjob_client.is_auto_apply_ready(
                            v.get("url", "")
                        )
                    except Exception as e:
                        log.warning("GeekJob readiness check failed before apply: %s", e)
                        geekjob_ready = False
                        geekjob_ready_message = f"Не удалось проверить GeekJob: {e}"

                if not geekjob_ready:
                    await set_hunter_status("search_manual", "Ручной GJ: сессия", "busy")
                    seen.mark_seen(vid, v, "manual_geekjob")
                    result["skipped"] += 1
                    bucket["manual"] += 1
                    analytics.record_decision(
                        run_id=run_id,
                        vacancy=v,
                        decision="manual_geekjob_session",
                        evaluation=evaluation,
                        details=details,
                        note=geekjob_ready_message,
                    )
                    create_task(
                        f"Ручной отклик: {v['title']} @ {v['company']}",
                        (
                            f"Источник: GeekJob\n"
                            f"Score: {score}/100\n"
                            f"{reason}\n"
                            f"URL: {v.get('url', '')}\n"
                            f"Причина: {geekjob_ready_message or 'нет активной сессии GeekJob'}"
                        ),
                        "medium",
                    )
                    await notify_needs_manual(
                        v,
                        score,
                        reason,
                        note=(
                            geekjob_ready_message
                            or "Нет активной сессии GeekJob. Запусти ./run.sh geekjob-login."
                        ),
                    )
                    continue

                if (
                    config.MAX_AUTO_APPLICATIONS_PER_SOURCE > 0
                    and auto_applied_count_by_source[source] >= config.MAX_AUTO_APPLICATIONS_PER_SOURCE
                ):
                    await set_hunter_status("search_manual", "Ручной GJ: лимит", "busy")
                    seen.mark_seen(vid, v, "manual_geekjob")
                    result["skipped"] += 1
                    bucket["manual"] += 1
                    analytics.record_decision(
                        run_id=run_id,
                        vacancy=v,
                        decision="manual_geekjob_limit",
                        evaluation=evaluation,
                        details=details,
                    )
                    create_task(
                        f"Ручной отклик: {v['title']} @ {v['company']}",
                        (
                            f"Источник: GeekJob\n"
                            f"Score: {score}/100\n"
                            f"{reason}\n"
                            f"URL: {v.get('url', '')}\n"
                            f"Причина: достигнут лимит автооткликов ({config.MAX_AUTO_APPLICATIONS_PER_SOURCE})"
                        ),
                        "medium",
                    )
                    await notify_needs_manual(
                        v,
                        score,
                        reason,
                        note=(
                            "Достигнут лимит автооткликов по GeekJob "
                            f"({config.MAX_AUTO_APPLICATIONS_PER_SOURCE} за прогон)."
                        ),
                    )
                    continue

                await set_hunter_status(
                    "search_apply",
                    _format_source_progress("Отклик", source, source_index, source_total),
                    "working",
                )
                cover = await generate_cover_letter(v, details)
                log.info("  GeekJob cover letter: %s", cover[:100] if cover else "(empty)")

                try:
                    apply_result = await geekjob_client.apply_to_vacancy(v, cover)
                except Exception as e:
                    await set_hunter_status("search_manual", "Ручной GJ: ошибка", "busy")
                    seen.mark_seen(vid, v, f"apply_failed_exception:{type(e).__name__}")
                    result["skipped"] += 1
                    bucket["manual"] += 1
                    analytics.record_decision(
                        run_id=run_id,
                        vacancy=v,
                        decision="apply_failed_exception",
                        evaluation=evaluation,
                        details=details,
                        note=f"geekjob:{type(e).__name__}",
                    )
                    log.exception("  GeekJob apply crashed for %s: %s", vid, e)
                    create_task(
                        f"Ручной отклик: {v['title']} @ {v['company']}",
                        (
                            f"Источник: GeekJob\n"
                            f"Score: {score}/100\n"
                            f"{reason}\n"
                            f"URL: {v.get('url', '')}\n"
                            f"Автоотклик упал: {type(e).__name__}: {e}"
                        ),
                        "medium",
                    )
                    await notify_needs_manual(
                        v,
                        score,
                        reason,
                        note=f"Автоотклик GeekJob упал: {type(e).__name__}. Проверь вручную.",
                    )
                    continue

                log.info("  GeekJob apply result: %s", apply_result)
                if apply_result.get("ok"):
                    seen.mark_seen(vid, v, "applied")
                    result["applied"] += 1
                    applied_count += 1
                    bucket["applied"] += 1
                    auto_applied_count_by_source[source] += 1
                    analytics.record_decision(
                        run_id=run_id,
                        vacancy=v,
                        decision="applied_auto",
                        evaluation=evaluation,
                        details=details,
                    )
                    await set_hunter_status(
                        "search_apply_done",
                        f"Отправил GJ {auto_applied_count_by_source[source]}",
                        "working",
                    )

                    task_id = create_task(
                        f"Отклик: {v['title']} @ {v['company']}",
                        (
                            f"Источник: GeekJob\n"
                            f"Score: {score}/100\n"
                            f"{reason}\n"
                            f"URL: {v.get('url', '')}\n"
                            f"Cover: {cover}"
                        ),
                        "low",
                    )
                    if task_id:
                        await task_complete(task_id, f"Отклик GeekJob отправлен (score {score})")

                    await notify_application(v, score, cover)
                else:
                    apply_message = apply_result.get("message", "unknown")
                    if "не авториз" in apply_message.lower() or "не гик" in apply_message.lower():
                        geekjob_ready = False
                        geekjob_ready_message = apply_message

                    await set_hunter_status("search_manual", "Ручной GJ: не ушёл", "busy")
                    seen.mark_seen(vid, v, f"apply_failed:{apply_message}")
                    result["skipped"] += 1
                    bucket["manual"] += 1
                    analytics.record_decision(
                        run_id=run_id,
                        vacancy=v,
                        decision="apply_failed",
                        evaluation=evaluation,
                        details=details,
                        note=f"geekjob:{apply_message}",
                    )
                    create_task(
                        f"Ручной отклик: {v['title']} @ {v['company']}",
                        (
                            f"Источник: GeekJob\n"
                            f"Score: {score}/100\n"
                            f"{reason}\n"
                            f"URL: {v.get('url', '')}\n"
                            f"Автоотклик не завершился: {apply_message}"
                        ),
                        "medium",
                    )
                    await notify_needs_manual(
                        v,
                        score,
                        reason,
                        note=f"Автоотклик GeekJob не завершился: {apply_message}",
                    )
                continue

            if source == "habr":
                if habr_client is None or not config.HABR_AUTO_APPLY:
                    await set_hunter_status("search_manual", "Ручной Хабр: выкл", "busy")
                    seen.mark_seen(vid, v, "manual_habr")
                    result["skipped"] += 1
                    bucket["manual"] += 1
                    analytics.record_decision(
                        run_id=run_id,
                        vacancy=v,
                        decision="manual_habr_disabled",
                        evaluation=evaluation,
                        details=details,
                    )
                    create_task(
                        f"Ручной отклик: {v['title']} @ {v['company']}",
                        (
                            f"Источник: {v.get('source_label', source)}\n"
                            f"Score: {score}/100\n"
                            f"{reason}\n"
                            f"URL: {v.get('url', '')}"
                        ),
                        "medium",
                    )
                    await notify_needs_manual(
                        v,
                        score,
                        reason,
                        note="Хабр Карьера отключена для автоотклика.",
                    )
                    continue

                if habr_logged_in is None:
                    try:
                        habr_logged_in = await habr_client.is_logged_in()
                    except Exception as e:
                        log.warning("Habr login check failed before apply: %s", e)
                        habr_logged_in = False

                if not habr_logged_in:
                    await set_hunter_status("search_manual", "Ручной Хабр: сессия", "busy")
                    seen.mark_seen(vid, v, "manual_habr")
                    result["skipped"] += 1
                    bucket["manual"] += 1
                    analytics.record_decision(
                        run_id=run_id,
                        vacancy=v,
                        decision="manual_habr_session",
                        evaluation=evaluation,
                        details=details,
                    )
                    create_task(
                        f"Ручной отклик: {v['title']} @ {v['company']}",
                        (
                            f"Источник: Хабр Карьера\n"
                            f"Score: {score}/100\n"
                            f"{reason}\n"
                            f"URL: {v.get('url', '')}\n"
                            "Причина: нет активной сессии Хабр Карьеры"
                        ),
                        "medium",
                    )
                    await notify_needs_manual(
                        v,
                        score,
                        reason,
                        note="Нет активной сессии Хабр Карьеры. Запусти ./run.sh habr-login.",
                    )
                    continue

                if (
                    config.MAX_AUTO_APPLICATIONS_PER_SOURCE > 0
                    and auto_applied_count_by_source[source] >= config.MAX_AUTO_APPLICATIONS_PER_SOURCE
                ):
                    await set_hunter_status("search_manual", "Ручной Хабр: лимит", "busy")
                    seen.mark_seen(vid, v, "manual_habr")
                    result["skipped"] += 1
                    bucket["manual"] += 1
                    analytics.record_decision(
                        run_id=run_id,
                        vacancy=v,
                        decision="manual_habr_limit",
                        evaluation=evaluation,
                        details=details,
                    )
                    create_task(
                        f"Ручной отклик: {v['title']} @ {v['company']}",
                        (
                            f"Источник: Хабр Карьера\n"
                            f"Score: {score}/100\n"
                            f"{reason}\n"
                            f"URL: {v.get('url', '')}\n"
                            f"Причина: достигнут лимит автооткликов ({config.MAX_AUTO_APPLICATIONS_PER_SOURCE})"
                        ),
                        "medium",
                    )
                    await notify_needs_manual(
                        v,
                        score,
                        reason,
                        note=(
                            "Достигнут лимит автооткликов по Хабр Карьере "
                            f"({config.MAX_AUTO_APPLICATIONS_PER_SOURCE} за прогон)."
                        ),
                    )
                    continue

                await set_hunter_status(
                    "search_apply",
                    _format_source_progress("Отклик", source, source_index, source_total),
                    "working",
                )
                cover = await generate_cover_letter(v, details)
                if len(cover) > 1500:
                    cover = cover[:1500]
                log.info("  Habr cover letter: %s", cover[:100] if cover else "(empty)")

                await wait_before_auto_apply(
                    source,
                    config.HABR_MIN_SECONDS_BETWEEN_APPLICATIONS,
                )
                last_apply_attempt_started_at_by_source[source] = asyncio.get_running_loop().time()
                try:
                    apply_result = await habr_client.apply_to_vacancy(v["url"], cover)
                except Exception as e:
                    await set_hunter_status("search_manual", "Ручной Хабр: ошибка", "busy")
                    seen.mark_seen(vid, v, f"apply_failed_exception:{type(e).__name__}")
                    result["skipped"] += 1
                    bucket["manual"] += 1
                    analytics.record_decision(
                        run_id=run_id,
                        vacancy=v,
                        decision="apply_failed_exception",
                        evaluation=evaluation,
                        details=details,
                        note=f"habr:{type(e).__name__}",
                    )
                    log.exception("  Habr apply crashed for %s: %s", vid, e)
                    create_task(
                        f"Ручной отклик: {v['title']} @ {v['company']}",
                        (
                            f"Источник: Хабр Карьера\n"
                            f"Score: {score}/100\n"
                            f"{reason}\n"
                            f"URL: {v.get('url', '')}\n"
                            f"Автоотклик упал: {type(e).__name__}: {e}"
                        ),
                        "medium",
                    )
                    await notify_needs_manual(
                        v,
                        score,
                        reason,
                        note=f"Автоотклик Хабр Карьеры упал: {type(e).__name__}. Проверь вручную.",
                    )
                    continue

                log.info("  Habr apply result: %s", apply_result)
                if apply_result.get("ok"):
                    seen.mark_seen(vid, v, "applied")
                    result["applied"] += 1
                    applied_count += 1
                    bucket["applied"] += 1
                    auto_applied_count_by_source[source] += 1
                    analytics.record_decision(
                        run_id=run_id,
                        vacancy=v,
                        decision="applied_auto",
                        evaluation=evaluation,
                        details=details,
                    )
                    await set_hunter_status(
                        "search_apply_done",
                        f"Отправил Хабр {auto_applied_count_by_source[source]}",
                        "working",
                    )

                    task_id = create_task(
                        f"Отклик: {v['title']} @ {v['company']}",
                        (
                            f"Источник: Хабр Карьера\n"
                            f"Score: {score}/100\n"
                            f"{reason}\n"
                            f"URL: {v.get('url', '')}\n"
                            f"Cover: {cover}"
                        ),
                        "low",
                    )
                    if task_id:
                        await task_complete(task_id, f"Отклик Хабр Карьеры отправлен (score {score})")

                    await notify_application(v, score, cover)
                else:
                    await set_hunter_status("search_manual", "Ручной Хабр: не ушёл", "busy")
                    seen.mark_seen(vid, v, f"apply_failed:{apply_result.get('message', 'unknown')}")
                    result["skipped"] += 1
                    bucket["manual"] += 1
                    analytics.record_decision(
                        run_id=run_id,
                        vacancy=v,
                        decision="apply_failed",
                        evaluation=evaluation,
                        details=details,
                        note=f"habr:{apply_result.get('message', 'unknown')}",
                    )
                    create_task(
                        f"Ручной отклик: {v['title']} @ {v['company']}",
                        (
                            f"Источник: Хабр Карьера\n"
                            f"Score: {score}/100\n"
                            f"{reason}\n"
                            f"URL: {v.get('url', '')}\n"
                            f"Автоотклик не завершился: {apply_result.get('message', 'unknown')}"
                        ),
                        "medium",
                    )
                    await notify_needs_manual(
                        v,
                        score,
                        reason,
                        note=f"Автоотклик Хабр Карьеры не завершился: {apply_result.get('message', 'unknown')}",
                    )
                continue

            # Генерируем сопроводительное (hh.ru лимит — 2000 символов)
            if (
                config.MAX_AUTO_APPLICATIONS_PER_SOURCE > 0
                and auto_applied_count_by_source[source] >= config.MAX_AUTO_APPLICATIONS_PER_SOURCE
            ):
                await set_hunter_status("search_manual", "Ручной hh: лимит", "busy")
                seen.mark_seen(vid, v, "manual_hh")
                result["skipped"] += 1
                bucket["manual"] += 1
                analytics.record_decision(
                    run_id=run_id,
                    vacancy=v,
                    decision="manual_hh_limit",
                    evaluation=evaluation,
                    details=details,
                    resume_variant=hh_resume_variant,
                )
                create_task(
                    f"Ручной отклик: {v['title']} @ {v['company']}",
                    (
                        f"Источник: hh.ru\n"
                        f"Score: {score}/100\n"
                        f"{reason}\n"
                        f"URL: {v.get('url', '')}\n"
                        f"Причина: достигнут лимит автооткликов ({config.MAX_AUTO_APPLICATIONS_PER_SOURCE})"
                    ),
                    "medium",
                )
                await notify_needs_manual(
                    v,
                    score,
                    reason,
                    note=(
                        "Достигнут лимит автооткликов по hh.ru "
                        f"({config.MAX_AUTO_APPLICATIONS_PER_SOURCE} за прогон)."
                    ),
                )
                continue

            await set_hunter_status(
                "search_apply",
                _format_source_progress("Отклик", source, source_index, source_total),
                "working",
            )
            cover = await generate_cover_letter(v, details)
            if len(cover) > 1900:
                cover = cover[:1900]
            log.info("  Cover letter: %s", cover[:100] if cover else "(empty)")

            try:
                apply_result = await hh_client.apply_to_vacancy(
                    v["url"],
                    cover,
                    response_url=v.get("response_url", ""),
                    preferred_resume_title=(hh_resume_variant or {}).get("title", ""),
                    preferred_resume_id=(hh_resume_variant or {}).get("id", ""),
                )
            except Exception as e:
                await set_hunter_status("search_manual", "Ручной hh: ошибка", "busy")
                seen.mark_seen(vid, v, f"apply_failed_exception:{type(e).__name__}")
                result["skipped"] += 1
                bucket["manual"] += 1
                analytics.record_decision(
                    run_id=run_id,
                    vacancy=v,
                    decision="apply_failed_exception",
                    evaluation=evaluation,
                    details=details,
                    resume_variant=hh_resume_variant,
                    note=f"hh:{type(e).__name__}",
                )
                log.exception("  Apply crashed for %s: %s", vid, e)
                create_task(
                    f"Ручной отклик: {v['title']} @ {v['company']}",
                    (
                        f"Источник: hh.ru\n"
                        f"Score: {score}/100\n"
                        f"{reason}\n"
                        f"URL: {v.get('url', '')}\n"
                        f"Автоотклик упал: {type(e).__name__}: {e}"
                    ),
                    "medium",
                )
                await notify_needs_manual(
                    v,
                    score,
                    reason,
                    note=f"Автоотклик hh.ru упал: {type(e).__name__}. Проверь вручную.",
                )
                continue
            log.info("  Apply result: %s", apply_result)

            if "пропускаем" in apply_result.get("message", "").lower():
                await set_hunter_status("search_manual", "Ручной hh: вопросы", "busy")
                seen.mark_seen(vid, v, "skipped_questions")
                result["skipped"] += 1
                bucket["manual"] += 1
                analytics.record_decision(
                    run_id=run_id,
                    vacancy=v,
                    decision="questions_required",
                    evaluation=evaluation,
                    details=details,
                    resume_variant=hh_resume_variant,
                )
                log.info("  Skipped: employer requires extra questions")
                await notify_needs_manual(v, score, reason)
                continue

            if apply_result["ok"]:
                seen.mark_seen(vid, v, "applied")
                if hh_resume_variant is not None:
                    hh_pipeline.record_successful_apply(v, hh_resume_variant)
                result["applied"] += 1
                applied_count += 1
                bucket["applied"] += 1
                auto_applied_count_by_source[source] += 1
                analytics.record_decision(
                    run_id=run_id,
                    vacancy=v,
                    decision="applied_auto",
                    evaluation=evaluation,
                    details=details,
                    resume_variant=hh_resume_variant,
                )
                await set_hunter_status(
                    "search_apply_done",
                    f"Отправил hh {auto_applied_count_by_source[source]}",
                    "working",
                )

                # AI Office: создаём задачу
                task_id = create_task(
                    f"Отклик: {v['title']} @ {v['company']}",
                    f"Score: {score}/100\n{reason}\nURL: {v.get('url', '')}\nCover: {cover}",
                    "low",
                )
                if task_id:
                    await task_complete(task_id, f"Отклик отправлен (score {score})")

                # Telegram
                await notify_application(v, score, cover)
            else:
                await set_hunter_status("search_manual", "Ручной hh: не ушёл", "busy")
                seen.mark_seen(vid, v, f"apply_failed:{apply_result['message']}")
                result["skipped"] += 1
                bucket["manual"] += 1
                analytics.record_decision(
                    run_id=run_id,
                    vacancy=v,
                    decision="apply_failed",
                    evaluation=evaluation,
                    details=details,
                    resume_variant=hh_resume_variant,
                    note=f"hh:{apply_result['message']}",
                )
                log.warning("  Apply failed: %s", apply_result["message"])
                await notify_needs_manual(
                    v,
                    score,
                    reason,
                    note=f"Автоотклик hh.ru не завершился: {apply_result['message']}",
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

        try:
            negotiation_statuses = await client.get_negotiation_statuses()
            analytics.record_negotiation_statuses(negotiation_statuses)
            if hh_pipeline.enabled():
                hh_pipeline.sync_negotiation_statuses(negotiation_statuses)
        except Exception as e:
            log.warning("Failed to sync hh staged resume statuses during check: %s", e)

        negotiations = await client.check_negotiations()
        invitations = negotiations.get("invitations", [])
        analytics.record_invitations(invitations)

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
                # AI Office: задача с высоким приоритетом
                task_id = create_task(
                    f"🎉 Приглашение: {inv['title']} @ {inv['company']}",
                    f"URL: {inv.get('url', '')}\nОтветить и назначить время!",
                    "urgent",
                )

                # Telegram: срочное уведомление
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


async def do_stats():
    """Показать статистику."""
    s = seen.stats()
    recent_runs = _load_recent_run_history(limit=5)
    analytics_summary = analytics.summarize()
    print(f"\n📊 Статистика Job Hunter")
    print(f"{'='*40}")
    print(f"  Всего обработано: {s['total']}")
    print(f"  Откликов:        {s['applied']}")
    print(f"  Ручной разбор:   {s['manual']}")
    print(f"  Пропущено:       {s['skipped']}")
    print()

    by_source = s.get("by_source", {})
    if by_source:
        print("По площадкам:")
        for line in _format_stats_source_breakdown(by_source):
            print(line)
        print()

    by_action = s.get("by_action", {})
    if by_action:
        print("Топ действий:")
        for action, count in sorted(by_action.items(), key=lambda item: (-item[1], item[0]))[:8]:
            print(f"  {action:<28} {count:>4}")
        print()

    print("Последние прогоны:")
    if not recent_runs:
        print("  пока нет истории запусков")
        print()
        return

    for run in recent_runs:
        status = "ok" if run.get("ok") else "err"
        created_at = run.get("created_at", "—")
        mode = run.get("mode", "search")
        found = run.get("found", 0)
        applied = run.get("applied", 0)
        skipped = run.get("skipped", 0)
        print(
            f"  {created_at} | {mode:<7} | {status:<3} | "
            f"found {found:<3} | applied {applied:<3} | skipped {skipped:<3}"
        )
        print(f"    {_format_run_source_stats(run.get('source_stats', {}))}")
        if run.get("error"):
            print(f"    error: {run['error']}")

    print()
    print(f"Аналитика за {analytics_summary['days']} дн.:")
    print(
        "  "
        f"Событий: {analytics_summary['events']} | "
        f"решений: {analytics_summary['decisions']} | "
        f"автооткликов: {analytics_summary['auto_applied']} | "
        f"manual: {analytics_summary['manual']}"
    )
    print(
        "  "
        f"keyword skip: {analytics_summary['keyword_filtered']} | "
        f"red flags: {analytics_summary['red_flagged']} | "
        f"low score: {analytics_summary['low_score']}"
    )
    print(
        "  "
        f"инвайтов: {analytics_summary['invitations']} | "
        f"positive: {analytics_summary['positive_statuses']} | "
        f"rejected: {analytics_summary['rejected_statuses']} | "
        f"pending: {analytics_summary['pending_statuses']}"
    )

    if analytics_summary["events"] == 0:
        print("  Аналитика начнёт заполняться со следующего search/check.")

    if analytics_summary.get("by_source"):
        print()
        print("Аналитика по площадкам:")
        for line in _format_analytics_source_breakdown(analytics_summary["by_source"]):
            print(line)

    if analytics_summary.get("by_query"):
        print()
        print("Топ запросов:")
        for line in _format_top_query_breakdown(analytics_summary["by_query"]):
            print(line)

    if analytics_summary.get("by_resume_variant"):
        print()
        print("По вариантам резюме:")
        for line in _format_resume_variant_breakdown(analytics_summary["by_resume_variant"]):
            print(line)

    if analytics_summary.get("top_decisions"):
        print()
        print("Топ решений:")
        for action, count in analytics_summary["top_decisions"]:
            print(f"  {action:<28} {count:>4}")
    print()


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
        tracked_hh_keys.add(_vacancy_match_key(payload.get("title", ""), payload.get("company", "")))

    for vacancy_id, payload in hh_pipeline.all_entries().items():
        tracked_hh_ids.add(str(vacancy_id))
        tracked_hh_keys.add(_vacancy_match_key(payload.get("title", ""), payload.get("company", "")))

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
                or _vacancy_match_key(item.get("title", ""), item.get("company", "")) in tracked_hh_keys
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
                or _vacancy_match_key(item.get("title", ""), item.get("company", "")) in tracked_hh_keys
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
