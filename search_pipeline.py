"""
Сбор вакансий из всех источников, дедупликация и keyword-фильтрация.

Извлечено из agent.py (A-001).
"""
from query_normalization import clean_query_list
from hh.search import HHSearchAuthError, HHSearchStopped
from hh.ui import HHUnexpectedUI

import inspect
import logging
import re
import time
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
_HH_AUTH_NOTICE_LAST_TS = 0.0
_HH_AUTH_NOTICE_COOLDOWN_S = 30 * 60


def _active_profile_name() -> str:
    try:
        import profile as profile_mod
        return str(getattr(profile_mod.active(), "name", "") or "default")
    except Exception:
        return "default"


async def _notify_hh_auth_required_once(reason: str) -> None:
    global _HH_AUTH_NOTICE_LAST_TS
    now = time.time()
    if now - _HH_AUTH_NOTICE_LAST_TS < _HH_AUTH_NOTICE_COOLDOWN_S:
        return
    _HH_AUTH_NOTICE_LAST_TS = now
    try:
        import notifier
        await notifier.notify_hh_session_required(_active_profile_name(), reason=reason)
    except Exception as exc:
        log.warning("failed to notify HH auth required: %s", exc)


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

def _collection_progress(bucket: dict | None, combinations: int) -> dict:
    progress = {"combinations_planned": combinations, "combinations_started": 0,
                "pages_attempted": 0, "vacancies_buffered": 0}
    if bucket is not None:
        bucket["collection"] = progress
    return progress


def _record_collection_stop(source: str, bucket: dict, error: Exception, *, discarded: bool) -> None:
    progress = bucket.setdefault("collection", {})
    planned, started = progress.get("combinations_planned"), progress.get("combinations_started")
    progress["combinations_unattempted"] = max(0, planned - started) if planned is not None and started is not None else None
    buffered = progress.get("vacancies_buffered")
    progress.update(source_stopped=True, source_discarded=discarded,
                    vacancies_retained=0 if discarded else buffered,
                    vacancies_discarded=buffered if discarded else 0)
    analytics.record_failure("collection", error, source=source, continued=False, source_stopped=True,
                             reason_code="SOURCE_DISCARDED" if discarded else "SOURCE_STOPPED",
                             collection=progress)
    log.warning("source=%s stage=collection error_kind=%s source_stopped=true source_discarded=%s "
                "combinations_unattempted=%s pages_attempted=%s vacancies_retained=%s vacancies_discarded=%s",
                source, type(error).__name__, str(discarded).lower(), progress["combinations_unattempted"],
                progress.get("pages_attempted"), progress["vacancies_retained"], progress["vacancies_discarded"])


async def collect_hh_vacancies(client: HHClient | None, *, scan_stats: dict | None = None) -> list[dict]:
    if client is None:
        return []
    if not config.HH_ENABLED:
        log.info("HH_ENABLED=0, skipping hh.ru source")
        return []
    can_collect, collect_note = hh_guard.can_collect()
    if not can_collect:
        if scan_stats is not None:
            scan_stats.setdefault("hh", {})["stop_reason"] = collect_note
        log.warning("Skipping hh.ru collection during cooldown: %s", collect_note)
        await office_log("hh_skipped", collect_note, "thinking")
        return []

    if not await client.is_logged_in():
        if scan_stats is not None:
            scan_stats.setdefault("hh", {})["stop_reason"] = "HH: авторизация потеряна; источник остановлен"
        log.warning("hh.ru is not logged in, skipping hh source")
        await office_log("hh_skipped", "hh.ru пропущен: нет авторизации", "thinking")
        await _notify_hh_auth_required_once("is_logged_in() returned false before HH collection")
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
    search_profiles = config.SEARCH_PROFILES
    if getattr(config, "HH_SEARCH_REMOTE_ONLY", False):
        search_profiles = [profile for profile in search_profiles if profile.get("schedule") == "remote"]
    progress = _collection_progress(bucket, len(search_profiles) * len(clean_query_list(config.SEARCH_QUERIES)))
    for profile in search_profiles:
        area = profile["area"]
        schedule = profile.get("schedule", "")
        area_label = f"area={area}" + (f",schedule={schedule}" if schedule else "")
        for query in clean_query_list(config.SEARCH_QUERIES):
            progress["combinations_started"] += 1
            for page_num in range(config.SEARCH_PAGES):
                log.info("HH search: %s [%s] page %d", query, area_label, page_num)
                progress["pages_attempted"] += 1
                page_error = None
                try:
                    vacancies = await client.search_vacancies(
                        query, page=page_num, area=area, schedule=schedule,
                    )
                except HHUnexpectedUI:
                    raise
                except HHSearchStopped as exc:
                    collect_note = f"HH: поиск остановлен: {exc}"
                    if bucket is not None:
                        bucket["stop_reason"] = collect_note
                    _record_collection_stop("hh", bucket if bucket is not None else {"collection": progress}, exc, discarded=False)
                    await office_log("hh_skipped", collect_note, "warning")
                    if isinstance(exc, HHSearchAuthError):
                        await _notify_hh_auth_required_once(str(exc))
                    return all_vacancies
                except Exception as exc:
                    page_error = exc
                anti_bot_signal = client.consume_antibot_signal()
                if anti_bot_signal:
                    status = hh_guard.record_antibot(
                        kind=anti_bot_signal.get("kind", ""),
                        raw_message=anti_bot_signal.get("message", ""),
                        stage=anti_bot_signal.get("stage", "search"),
                    )
                    collect_note = hh_guard.format_block_note(status)
                    if bucket is not None:
                        bucket["stop_reason"] = collect_note
                    _record_collection_stop("hh", bucket if bucket is not None else {"collection": progress},
                                            RuntimeError("antibot"), discarded=False)
                    await office_log("hh_skipped", collect_note, "thinking")
                    return all_vacancies
                if page_error is not None:
                    if bucket is not None:
                        bucket["pages_failed"] = bucket.get("pages_failed", 0) + 1
                    analytics.record_failure("collection", page_error, source="hh", continued=True)
                    log.warning(
                        "source=hh stage=collection query=%r [%s] page=%d error_kind=%s continued=true",
                        query, area_label, page_num, type(page_error).__name__,
                    )
                    continue
                if bucket is not None:
                    bucket["fetched"] += len(vacancies)

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
                    progress["vacancies_buffered"] = len(all_vacancies)

                if not vacancies:
                    break
                # A short page whose state listed every card is the last one: the next
                # request would only return «0 вакансий». Without the state (SSR shows
                # only the first ~20 cards) a short page proves nothing.
                if getattr(vacancies, "complete", False) and len(vacancies) < int(
                        0.8 * int(getattr(config, "HH_SEARCH_PAGE_SIZE", 20) or 20)):
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
    progress = _collection_progress(bucket, len(config.SUPERJOB_SEARCH_PROFILES) * len(clean_query_list(config.SUPERJOB_SEARCH_QUERIES)))
    for profile in config.SUPERJOB_SEARCH_PROFILES:
        profile_label = profile.get("label", "default")
        for query in clean_query_list(config.SUPERJOB_SEARCH_QUERIES):
            progress["combinations_started"] += 1
            for page_num in range(config.SUPERJOB_SEARCH_PAGES):
                log.info("SuperJob search: %s [%s] page %d", query, profile_label, page_num)
                progress["pages_attempted"] += 1
                vacancies, more = await client.search_vacancies(query, page=page_num, profile=profile)
                if bucket is not None:
                    bucket["fetched"] += len(vacancies)

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
                    progress["vacancies_buffered"] = len(all_vacancies)

                if not more:
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
    progress = _collection_progress(bucket, len(config.HABR_SEARCH_PATHS))
    for path in config.HABR_SEARCH_PATHS:
        progress["combinations_started"] += 1
        total_pages = 1
        for page_num in range(1, config.HABR_SEARCH_PAGES + 1):
            if page_num > total_pages:
                break

            log.info("Habr Career search: %s page %d", path, page_num)
            progress["pages_attempted"] += 1
            vacancies, total_pages = await client.search_vacancies(path=path, page=page_num)
            if bucket is not None:
                bucket["fetched"] += len(vacancies)

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
                progress["vacancies_buffered"] = len(all_vacancies)

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
    progress = _collection_progress(bucket, 1)
    progress["combinations_started"] = int(config.GEEKJOB_SEARCH_PAGES > 0)
    total_pages = 1
    for page_num in range(1, config.GEEKJOB_SEARCH_PAGES + 1):
        if page_num > total_pages:
            break

        log.info("GeekJob search: page %d", page_num)
        progress["pages_attempted"] += 1
        vacancies, total_pages = await client.search_vacancies(page=page_num)
        if bucket is not None:
            bucket["fetched"] += len(vacancies)

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
            progress["vacancies_buffered"] = len(all_vacancies)

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
    for counter in ("fetched", "already_seen", "new", "relevant", "applied", "manual", "rejected"):
        bucket.setdefault(counter, 0)
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
        if v.get("_hh_retry"):
            bucket["relevant"] += 1  # Deprecated compatibility alias for keyword_pass.
            filtered.append(v)
            continue
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
            bucket["relevant"] += 1  # Deprecated compatibility alias for keyword_pass.
            filtered.append(v)
    # One increment point for the retained keyword-stage output, including retries.
    for vacancy in filtered:
        bucket = get_source_bucket(source_stats, vacancy)
        bucket["keyword_pass"] = bucket.get("keyword_pass", 0) + 1
        analytics.count_stage("keyword_pass", vacancy)
    return filtered


async def collect_all(
    hh_client: HHClient | None,
    superjob_client: SuperJobClient | None,
    habr_client: HabrCareerClient | None,
    geekjob_client: GeekJobClient | None,
    hh_retry_vacancies: list[dict] | None = None,
    source_stats: dict | None = None,
    status_callback: Callable[[str, str, str], Awaitable[None]] | None = None,
    hh_stop_reason: str = "",
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
        if source_key == "hh" and hh_stop_reason:
            scan_stats.setdefault("hh", {})["stop_reason"] = hh_stop_reason
            return []
        if enabled:
            await _status("search_collect", f"Собираю {label}", "working")
        try:
            with analytics.event_context(source=source_key, stage="collection", vacancy_id=""):
                vacancies = await collector()
                bucket = scan_stats.get(source_key, {})
                if "collection" in bucket:
                    bucket["collection"]["vacancies_retained"] = len(vacancies)
                return vacancies
        except HHUnexpectedUI as exc:
            if source_key != "hh":
                raise
            bucket = scan_stats.setdefault("hh", {})
            bucket["stop_reason"] = "HH: нестандартный UI при сборе вакансий; источник остановлен"
            bucket["stop_fingerprint"] = getattr(exc, "fingerprint", "")
            _record_collection_stop("hh", bucket, exc, discarded=True)
            return []
        except Exception as exc:
            bucket = scan_stats.setdefault(source_key, {})
            bucket["stop_reason"] = "SOURCE_DISCARDED"
            _record_collection_stop(source_key, bucket, exc, discarded=True)
            await office_log(f"{source_key}_collect_failed", f"{label}: источник отброшен ({type(exc).__name__})", "warning")
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
