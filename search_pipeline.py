"""
Сбор вакансий из всех источников, дедупликация и keyword-фильтрация.

Извлечено из agent.py (A-001).
"""
import inspect
import logging
import re
from typing import Callable, Awaitable

import config
import filters
import seen
import analytics
import hh_guard
from office_bridge import office_log

from hh_client import HHClient
from superjob_client import SuperJobClient
from habr_career_client import HabrCareerClient
from geekjob_client import GeekJobClient

log = logging.getLogger("agent")


# ── Дедупликация ──

def vacancy_dedupe_key(vacancy: dict) -> str:
    url = (vacancy.get("url") or "").split("?", 1)[0].strip().casefold()
    title = re.sub(r"\s+", " ", vacancy.get("title", "").casefold()).strip()
    company = re.sub(r"\s+", " ", vacancy.get("company", "").casefold()).strip()
    location = re.sub(r"\s+", " ", vacancy.get("location", "").casefold()).strip()
    if title and company:
        return f"{title}|{company}|{location}"
    if url:
        return url
    return vacancy.get("id", "")


def normalize_match_value(value: str) -> str:
    return " ".join((value or "").casefold().split())


def vacancy_match_key(title: str, company: str) -> str:
    return f"{normalize_match_value(title)}|{normalize_match_value(company)}"


# ── Сбор вакансий ──

async def collect_hh_vacancies(client: HHClient | None, *, scan_stats: dict | None = None) -> list[dict]:
    if client is None:
        return []
    if not config.HH_ENABLED:
        log.info("HH_ENABLED=0, skipping hh.ru source")
        return []
    can_collect, collect_note = hh_guard.can_collect()
    if not can_collect:
        log.warning("Skipping hh.ru collection during cooldown: %s", collect_note)
        await office_log("hh_skipped", collect_note, "thinking")
        return []

    if not await client.is_logged_in():
        log.warning("hh.ru is not logged in, skipping hh source")
        await office_log("hh_skipped", "hh.ru пропущен: нет авторизации", "thinking")
        return []

    all_vacancies = []
    bucket = None
    if scan_stats is not None:
        bucket = scan_stats.setdefault(
            "hh",
            {
                "label": "hh.ru",
                "fetched": 0,
                "already_seen": 0,
                "new": 0,
                "relevant": 0,
                "applied": 0,
                "manual": 0,
                "rejected": 0,
            },
        )
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
                anti_bot_signal = client.consume_antibot_signal()
                if anti_bot_signal:
                    status = hh_guard.record_antibot(
                        kind=anti_bot_signal.get("kind", ""),
                        raw_message=anti_bot_signal.get("message", ""),
                        stage=anti_bot_signal.get("stage", "search"),
                    )
                    collect_note = hh_guard.format_block_note(status)
                    log.warning("Stopping hh.ru collection after anti-bot signal: %s", collect_note)
                    await office_log("hh_skipped", collect_note, "thinking")
                    return all_vacancies
                if bucket is not None:
                    bucket["fetched"] += len(vacancies)

                new_on_page = 0
                for vacancy in vacancies:
                    vid = vacancy.get("id")
                    if not vid:
                        continue
                    if seen.is_seen(vid):
                        if bucket is not None:
                            bucket["already_seen"] += 1
                        continue
                    vacancy["source"] = "hh"
                    vacancy["source_label"] = "hh.ru"
                    vacancy["apply_mode"] = "auto"
                    vacancy["_search_query"] = query
                    vacancy["_search_profile"] = area_label
                    all_vacancies.append(vacancy)
                    new_on_page += 1

                if len(vacancies) < 10:
                    break

    return all_vacancies


async def collect_superjob_vacancies(client: SuperJobClient | None, *, scan_stats: dict | None = None) -> list[dict]:
    if client is None:
        return []
    if not config.SUPERJOB_ENABLED:
        return []
    if not config.SUPERJOB_API_KEY:
        log.info("SUPERJOB_API_KEY is not configured, skipping SuperJob source")
        return []

    all_vacancies = []
    bucket = None
    if scan_stats is not None:
        bucket = scan_stats.setdefault(
            "superjob",
            {
                "label": "SuperJob",
                "fetched": 0,
                "already_seen": 0,
                "new": 0,
                "relevant": 0,
                "applied": 0,
                "manual": 0,
                "rejected": 0,
            },
        )
    for profile in config.SUPERJOB_SEARCH_PROFILES:
        profile_label = profile.get("label", "default")
        for query in config.SUPERJOB_SEARCH_QUERIES:
            for page_num in range(config.SUPERJOB_SEARCH_PAGES):
                log.info("SuperJob search: %s [%s] page %d", query, profile_label, page_num)
                vacancies, more = await client.search_vacancies(query, page=page_num, profile=profile)
                if bucket is not None:
                    bucket["fetched"] += len(vacancies)

                new_on_page = 0
                for vacancy in vacancies:
                    vid = vacancy.get("id")
                    if not vid:
                        continue
                    if seen.is_seen(vid):
                        if bucket is not None:
                            bucket["already_seen"] += 1
                        continue
                    vacancy["_search_query"] = query
                    vacancy["_search_profile"] = profile_label
                    all_vacancies.append(vacancy)
                    new_on_page += 1

                if not vacancies or not more or new_on_page == 0:
                    break

    return all_vacancies


async def collect_habr_vacancies(client: HabrCareerClient | None, *, scan_stats: dict | None = None) -> list[dict]:
    if client is None:
        return []
    if not config.HABR_ENABLED:
        return []

    all_vacancies = []
    bucket = None
    if scan_stats is not None:
        bucket = scan_stats.setdefault(
            "habr",
            {
                "label": "Хабр Карьера",
                "fetched": 0,
                "already_seen": 0,
                "new": 0,
                "relevant": 0,
                "applied": 0,
                "manual": 0,
                "rejected": 0,
            },
        )
    for path in config.HABR_SEARCH_PATHS:
        total_pages = 1
        for page_num in range(1, config.HABR_SEARCH_PAGES + 1):
            if page_num > total_pages:
                break

            log.info("Habr Career search: %s page %d", path, page_num)
            vacancies, total_pages = await client.search_vacancies(path=path, page=page_num)
            if bucket is not None:
                bucket["fetched"] += len(vacancies)

            new_on_page = 0
            for vacancy in vacancies:
                vid = vacancy.get("id")
                if not vid:
                    continue
                if seen.is_seen(vid):
                    if bucket is not None:
                        bucket["already_seen"] += 1
                    continue
                vacancy["_search_path"] = path
                vacancy["_search_profile"] = path
                all_vacancies.append(vacancy)
                new_on_page += 1

            if not vacancies or new_on_page == 0:
                break

    return all_vacancies


async def collect_geekjob_vacancies(client: GeekJobClient | None, *, scan_stats: dict | None = None) -> list[dict]:
    if client is None:
        return []
    if not config.GEEKJOB_ENABLED:
        return []

    all_vacancies = []
    bucket = None
    if scan_stats is not None:
        bucket = scan_stats.setdefault(
            "geekjob",
            {
                "label": "GeekJob",
                "fetched": 0,
                "already_seen": 0,
                "new": 0,
                "relevant": 0,
                "applied": 0,
                "manual": 0,
                "rejected": 0,
            },
        )
    total_pages = 1
    for page_num in range(1, config.GEEKJOB_SEARCH_PAGES + 1):
        if page_num > total_pages:
            break

        log.info("GeekJob search: page %d", page_num)
        vacancies, total_pages = await client.search_vacancies(page=page_num)
        if bucket is not None:
            bucket["fetched"] += len(vacancies)

        new_on_page = 0
        for vacancy in vacancies:
            vid = vacancy.get("id")
            if not vid:
                continue
            if seen.is_seen(vid):
                if bucket is not None:
                    bucket["already_seen"] += 1
                continue
            vacancy["_search_profile"] = f"page={page_num}"
            all_vacancies.append(vacancy)
            new_on_page += 1

        if not vacancies or new_on_page == 0:
            break

    return all_vacancies


# ── Основной pipeline: collect + dedupe + filter ──

def get_source_bucket(stats: dict, vacancy: dict) -> dict:
    source = vacancy.get("source") or "unknown"
    label = vacancy.get("source_label") or source
    bucket = stats.setdefault(
        source,
        {
            "label": label,
            "fetched": 0,
            "already_seen": 0,
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


def deduplicate(all_vacancies: list[dict]) -> list[dict]:
    """Дедупликация с предпочтением hh.ru."""
    unique = {}
    for vacancy in all_vacancies:
        key = vacancy_dedupe_key(vacancy)
        existing = unique.get(key)
        if existing is None:
            unique[key] = vacancy
            continue
        if existing.get("source") != "hh" and vacancy.get("source") == "hh":
            unique[key] = vacancy
    return list(unique.values())


def keyword_filter(
    vacancies: list[dict],
    source_stats: dict,
    run_id: str,
) -> list[dict]:
    """Keyword-фильтрация перед LLM. Возвращает прошедшие вакансии."""
    filtered = []
    for v in vacancies:
        bucket = get_source_bucket(source_stats, v)
        reject_reason = filters.check_vacancy(v)
        if reject_reason:
            seen.mark_seen(v["id"], v, "skipped_keyword_filter")
            analytics.record_decision(
                run_id=run_id,
                vacancy=v,
                decision="skipped_keyword_filter",
                note=reject_reason,
            )
        else:
            bucket["relevant"] += 1
            filtered.append(v)
    return filtered


async def collect_all(
    hh_client: HHClient | None,
    superjob_client: SuperJobClient | None,
    habr_client: HabrCareerClient | None,
    geekjob_client: GeekJobClient | None,
    hh_retry_vacancies: list[dict] | None = None,
    source_stats: dict | None = None,
    status_callback: Callable[[str, str, str], Awaitable[None]] | None = None,
) -> list[dict]:
    """Собрать вакансии из всех включённых источников."""
    scan_stats = source_stats if source_stats is not None else {}

    async def _status(action: str, msg: str, status: str) -> None:
        if status_callback:
            await status_callback(action, msg, status)

    async def _invoke_collector(collector, client) -> list[dict]:
        try:
            params = inspect.signature(collector).parameters
        except (TypeError, ValueError):
            params = {}
        if "scan_stats" in params:
            return await collector(client, scan_stats=scan_stats)
        return await collector(client)

    async def _collect_source(
        *,
        source_key: str,
        enabled: bool,
        label: str,
        collector,
    ) -> list[dict]:
        if enabled:
            await _status("search_collect", f"Собираю {label}", "working")
        try:
            return await collector()
        except Exception as exc:
            log.warning("%s collection failed: %s", label, exc, exc_info=True)
            await office_log(f"{source_key}_collect_failed", f"{label} пропущен: {exc}", "warning")
            if enabled:
                await _status("search_collect_error", f"{label}: ошибка, продолжаю без источника", "working")
            return []

    hh_vacancies = await _collect_source(
        source_key="hh",
        enabled=config.HH_ENABLED,
        label="hh.ru",
        collector=lambda: _invoke_collector(collect_hh_vacancies, hh_client),
    )

    superjob_vacancies = await _collect_source(
        source_key="superjob",
        enabled=config.SUPERJOB_ENABLED,
        label="SuperJob",
        collector=lambda: _invoke_collector(collect_superjob_vacancies, superjob_client),
    )

    habr_vacancies = await _collect_source(
        source_key="habr",
        enabled=config.HABR_ENABLED,
        label="Хабр",
        collector=lambda: _invoke_collector(collect_habr_vacancies, habr_client),
    )

    geekjob_vacancies = await _collect_source(
        source_key="geekjob",
        enabled=config.GEEKJOB_ENABLED,
        label="GeekJob",
        collector=lambda: _invoke_collector(collect_geekjob_vacancies, geekjob_client),
    )

    all_vacancies = (
        hh_vacancies
        + (hh_retry_vacancies or [])
        + superjob_vacancies
        + habr_vacancies
        + geekjob_vacancies
    )

    return all_vacancies
