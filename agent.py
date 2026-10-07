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
import hashlib
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
from functools import wraps

import config
import company_blacklist
from relevance_verifier import ShadowVerifier
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
from state_store.json_store import JsonStore, atomic_write_text
from state_store.private_journal import append_json
from private_logging import PrivateFileHandler, OperationalFormatter, chat_log_path
from state_store.protected import ProtectedJsonStore
from state_store.matcher_deferred import MatcherDeferredQueue
from llm_client import close_llm_client
from outcome import (
    DECISION_APPLIED_AUTO,
    DECISION_ALREADY_APPLIED,
    DECISION_APPLY_UNCERTAIN,
    DECISION_APPLY_FAILED,
    DECISION_APPLY_FAILED_EXCEPTION,
    DECISION_DRY_RUN_MATCH,
    DECISION_QUESTIONS_REQUIRED,
    DECISION_MANUAL_REVIEW,
    DECISION_DEFERRED_UNSCORED,
    DECISION_SKIPPED_LOW_SCORE,
    DECISION_SKIPPED_RED_FLAGS,
    apply_result_is_uncertain,
)
from geekjob_client import GeekJobClient
from habr_career_client import HabrCareerClient
from hh_client import HHClient
from hh.ui import HHUnexpectedUI
from matcher import analyze_cover_letter, evaluate_vacancy, generate_cover_letter, is_manual_review_candidate, is_deferred_evaluation
from office_bridge import office_log, create_task, task_progress, task_complete
from office_bridge import close_session as close_office_session
import notifier
from notifier import (
    notify_application, notify_invitation, notify_search_started, notify_summary, notify_digest, notify_needs_manual,
    close_session as close_notify_session,
)
from superjob_client import SuperJobClient
from commands import chats as chat_commands
from commands import google_forms as google_form_commands
from commands.resume import (
    _print_hh_resume_boost_detail,
    do_hh_resume_boost,
    do_hh_resume_boost_status,
)


class HHRecoveryBreaker:
    """Per-search HH recovery budget; never affects other sources."""
    def __init__(self):
        self.incidents = 0
        self.fingerprints = set()
        self.stopped = False
        self.reason = ""

    def hard_stop(self, reason: str) -> None:
        self.stopped = True
        self.reason = reason

    def recovered_incident(self, fingerprint: str) -> None:
        self.incidents += 1
        if fingerprint:
            self.fingerprints.add(fingerprint)
        if self.incidents >= 5:
            self.hard_stop("пятый восстанавливаемый нестандартный UI")
        elif len(self.fingerprints) >= 3:
            self.hard_stop("три различных fingerprint нестандартного UI")

    def snapshot(self) -> dict:
        return {"incidents": self.incidents,
                "fingerprints": sorted(self.fingerprints),
                "distinct": len(self.fingerprints),
                "stopped": self.stopped,
                "reason": self.reason}


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


def _apply_source_selection(source: str) -> None:
    source_flags = {
        "hh": "HH_ENABLED",
        "superjob": "SUPERJOB_ENABLED",
        "habr": "HABR_ENABLED",
        "geekjob": "GEEKJOB_ENABLED",
    }
    if not source:
        return
    if source not in source_flags:
        raise ValueError(f"Unknown source: {source}")
    for source_name, config_name in source_flags.items():
        setattr(config, config_name, source_name == source)


def _evaluation_with_guard_flag(evaluation: dict, flag: str) -> dict:
    updated = dict(evaluation or {})
    flags = list(updated.get("guard_flags") or [])
    if flag not in flags:
        flags.append(flag)
    updated["guard_flags"] = flags
    return updated


def _build_logging_handlers() -> list[logging.Handler]:
    # The background launcher redirects stdout/stderr to LOG_FILE. In that
    # mode a StreamHandler would write every record a second time to the same
    # file; keep it only for an interactive terminal.
    handlers: list[logging.Handler] = []
    if not os.environ.get("JOB_HUNTER_BACKGROUND") and sys.stdout.isatty():
        handlers.append(logging.StreamHandler())

    if config.LOG_FILE:
        log_dir = os.path.dirname(config.LOG_FILE)
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
        handlers.append(PrivateFileHandler(config.LOG_FILE, channel="search"))
        handlers.append(PrivateFileHandler(chat_log_path(config.LOG_FILE), channel="chat"))

    if config.ERROR_LOG_FILE:
        error_dir = os.path.dirname(config.ERROR_LOG_FILE)
        if error_dir:
            os.makedirs(error_dir, exist_ok=True)
        error_handler = PrivateFileHandler(config.ERROR_LOG_FILE, channel="search")
        error_handler.setLevel(logging.WARNING)
        handlers.append(error_handler)

    return handlers


def _configure_logging(force: bool = False) -> None:
    if not force and logging.getLogger().handlers:
        return
    handlers = _build_logging_handlers()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=handlers,
        force=force,
    )
    for handler in handlers:
        handler.setFormatter(OperationalFormatter())
    logging.getLogger("chat_responder").propagate = True


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
        JsonStore(config.RUNTIME_STATUS_FILE).save(payload)
    except Exception as exc:
        log.warning("Failed to write runtime status: %s", exc)


def _append_run_history(entry: dict, *, path: str | None = None) -> None:
    try:
        append_json(path or config.RUN_HISTORY_FILE, entry)
    except Exception as exc:
        log.warning("Failed to append run history: %s", type(exc).__name__)


def _record_search_run(result: dict, dry_run: bool, ok: bool, error: str = "") -> None:
    observation = analytics.current_search()
    if observation is not None:
        observation.result = result
        observation.ok = ok
        observation.error_kind = "HHUnexpectedUI" if error == "hh_unexpected_ui" else error
        return
    # Compatibility for callers outside the observed search lifecycle.
    mode = "dry-run" if dry_run else "search"
    entry = {**result, "kind": "search", "run_id": result.get("_run_id", ""),
             "ok": ok, "mode": mode, "error_kind": error,
             "created_at": datetime.now().isoformat(timespec="seconds")}
    _append_run_history(entry)
    analytics.record_search_finished(run_id=entry["run_id"], mode=mode, result=entry)


def _observe_search(function):
    @wraps(function)
    async def wrapped(dry_run=False):
        mode = "dry-run" if dry_run else "search"
        run_id = analytics.new_run_id(mode)
        with analytics.observe_search(run_id, mode) as observation:
            _append_run_history(observation.entry(incomplete=True), path=observation.history_file)
            enabled = [label for enabled, label in ((config.HH_ENABLED, "hh.ru"),
                (config.SUPERJOB_ENABLED, "SuperJob"), (config.HABR_ENABLED, "Хабр Карьера"),
                (config.GEEKJOB_ENABLED, "GeekJob")) if enabled]
            analytics.record_search_started(run_id=run_id, mode=mode, enabled_sources=enabled)
            try:
                return await function(dry_run=dry_run)
            except BaseException as exc:
                observation.ok = False
                observation.error_kind = type(exc).__name__
                if not isinstance(exc, asyncio.CancelledError) or not observation.failure_stage:
                    analytics.record_failure(observation.stage, exc, continued=False)
                raise
            finally:
                # Instrumentation never replaces a result/exception from the business coroutine.
                try:
                    analytics.finish_unclassified(observation)
                    entry = observation.entry()
                    observation.result.update({field: entry[field] for field in
                        ("run_id", "started_at", "finished_at", "ok", "new", "manual", "funnel",
                         "reason_breakdown", "failure_stage", "error_kind", "stage_failures", "source_stats")})
                    _append_run_history(entry, path=observation.history_file)
                    analytics.record_search_finished(run_id=run_id, mode=mode, result=entry)
                except Exception as exc:
                    log.warning("Search diagnostics finalization failed: error_kind=%s", type(exc).__name__)
    return wrapped



def _format_no_new_vacancies_note(source_stats: dict | None) -> str:
    if not source_stats:
        return "Новых вакансий нет"

    new_count = sum(int((bucket or {}).get("new", 0) or 0) for bucket in source_stats.values())
    note = f"Все новые вакансии отсеяны keyword filter ({new_count})" if new_count else "Новых вакансий нет"
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
        return note
    return note + ". По источникам: " + "; ".join(lines)


def _snapshot_slug(value: object, max_length: int = 80) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "_", str(value or "").strip()).strip("._-")
    if not slug:
        return "unknown"
    return slug[:max_length]


async def _save_autoapply_failure_snapshot(
    source: str,
    vacancy_id: str,
    page,
    *, state_dir: str | None = None,
) -> dict[str, str]:
    if page is None:
        return {}

    from private_artifacts import capture_artifacts
    saved = await capture_artifacts(page, state_dir or config.HH_STATE_DIR,
                                   f"autoapply_failed_{source}_{vacancy_id}", full_page=True)

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
    import hh_resume_pipeline

    resume_path = config.RESUME_FILE
    pipeline_enabled = hh_resume_pipeline.enabled()
    resume_variants = hh_resume_pipeline.get_variants() if pipeline_enabled else []
    resumes = await client.get_resume_ids()
    if not resumes:
        print("❌ Резюме не найдены на hh.ru")
        return

    # Профильный фильтр: если включён HH-резюме-пайплайн с заданными тайтлами,
    # оставляем только резюме, попавшие в варианты профиля. Остальные
    # (например, резюме другого профиля в том же hh-аккаунте) скрываем.
    if pipeline_enabled:
        resolved = hh_resume_pipeline.resolve_variants(resumes, variants=resume_variants)
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

    atomic_write_text(resume_path, result["raw"])

    print(f"\n✅ Резюме сохранено: {resume_path}")
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




async def do_manual_apply_token(token: str) -> dict:
    """Отправить подтвержденный человеком yellow-zone отклик по token из очереди."""
    item = manual_apply_queue.get_candidate(token)
    if not item:
        print(f"❌ Заявка не найдена или устарела: {token}")
        return {"ok": False, "message": "manual apply token not found"}

    if getattr(config, "HH_APPLICATION_MODE", "auto") == "preview":
        return {"ok": False, "message": "Включён режим просмотра без отправки"}
    vacancy = dict(item.get("vacancy") or {})
    if company_blacklist.is_blocked(vacancy.get("company", "")):
        manual_apply_queue.mark_candidate(token, "company_blocked", "Компания в чёрном списке")
        return {"ok": False, "message": "Компания в чёрном списке"}
    if item.get("status") != "pending" or not item.get("allow_ai_apply", True):
        return {"ok": False, "message": "Отклик для этой карточки недоступен"}
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

    queue_store = manual_apply_queue._store()
    claimed = manual_apply_queue.claim_candidate(token, store=queue_store)
    if not claimed:
        return {"ok": False, "message": "Отклик уже занят, отозван или завершён"}
    owner = claimed["owner"]
    dispatch_started = False
    shutdown_started = False
    manual_result = None
    owned_attempt = None
    def finish(status, message=""):
        return manual_apply_queue.finish_candidate(token, owner, status, message, store=queue_store)
    def valid():
        return (manual_apply_queue.approval_valid(token, owner, store=queue_store)
                and not company_blacklist.is_blocked(vacancy.get("company", "")))
    def begin_external():
        return valid() and manual_apply_queue.begin_external(token, owner, store=queue_store)

    run_id = analytics.new_run_id("manual-ai-apply")
    vacancy["_analytics_run_id"] = run_id
    vacancy["_analytics_apply_mode"] = "manual"
    hh_client = HHClient()
    hh_client._manual_apply_guard = begin_external
    hh_client._manual_apply_no_action = lambda: manual_apply_queue.confirm_no_action(token, owner, store=queue_store)
    manual_uncertainty_note = None
    def record_manual_uncertain():
        nonlocal manual_uncertainty_note
        if manual_uncertainty_note is not None:
            return manual_uncertainty_note
        note = "Исход отклика не подтверждён. Отклик мог быть отправлен; проверьте историю вручную. Автоматического повтора нет."
        seen.mark_seen(vacancy.get("id", token), vacancy, "apply_uncertain")
        hh_pipeline.mark_terminal(vacancy.get("id", token), "apply_uncertain")
        finish("uncertain", note)
        analytics.record_decision(run_id=run_id, vacancy=vacancy, decision=DECISION_APPLY_UNCERTAIN,
                                  evaluation=evaluation, details=details, note="manual_ai:apply_uncertain")
        manual_uncertainty_note = note
        return note

    def reconcile_manual_result():
        nonlocal manual_result
        if manual_result is None:
            return
        manual_result = apply_orchestrator.apply_result_with_current_uncertainty(
            vacancy, hh_client, manual_result, owned_attempt=owned_attempt)
        if apply_result_is_uncertain(manual_result):
            manual_result["message"] = record_manual_uncertain()
    try:
        await hh_client.start()
        if not await hh_client.is_logged_in():
            raise RuntimeError("Не залогинен в hh.ru")

        can_apply, guard_note = hh_guard.can_auto_apply()
        if not can_apply:
            finish("deferred", guard_note)
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
            finish("archived", message)
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
        cover = await analytics.tracked_call("cover_letter", run_id, vacancy, generate_cover_letter, vacancy, details)
        cover = cover or ""
        if len(cover) > cover_limit:
            cover = cover[:cover_limit]
        cover_evaluation = _evaluation_with_cover_letter(evaluation, cover)
        if not (cover or "").strip():
            message = "ИИ-сопровод не сгенерировался; отклик без текста не отправляю."
            finish("failed_no_cover", message)
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

        if not valid():
            finish("dismissed", "Подтверждение отозвано до отправки")
            return {"ok": False, "message": "Подтверждение отозвано до отправки"}
        dispatch_started = True
        apply_result = await apply_orchestrator.dispatch_apply(vacancy, cover, hh_client=hh_client)
        owned_attempt = getattr(hh_client, "_external_attempt", None)
        if owned_attempt is None:
            monitor = getattr(getattr(hh_client, "_page", None), "_hh_action_monitor", None)
            owned_attempt = getattr(monitor, "last_attempt", None)
        manual_result = apply_result
        reconcile_manual_result()
        # This command has only one vacancy. Seal browser activity before its
        # final queue/seen classification or any success notification.
        shutdown_started = True
        try:
            await hh_client.stop()
        finally:
            reconcile_manual_result()
        apply_result = manual_result
        if apply_result_is_uncertain(apply_result):
            apply_result = {**apply_result, "ok": False, "uncertain": True}
            manual_note = record_manual_uncertain()
            await notify_needs_manual(vacancy, score, reason, note=manual_note)
            return {**apply_result, "ok": False, "uncertain": True, "message": manual_note}
        _record_hh_questionnaire_analytics(
            run_id=run_id,
            vacancy=vacancy,
            apply_result=apply_result,
            success=bool(apply_result.get("ok")),
        )
        cover_evaluation["cover_letter_status"] = apply_result.get("cover_letter_status", "unknown")
        apply_notes = apply_result.get("notes") or []
        apply_note_text = "; ".join(str(item) for item in apply_notes if item)
        question_answer_note = _format_hh_question_answers_for_note(apply_result)
        if question_answer_note:
            apply_note_text = (apply_note_text + "\n\n" + question_answer_note).strip()
        apply_message = str(apply_result.get("message", ""))

        if apply_result.get("closed_or_archived") or _looks_like_closed_or_archived(vacancy, apply_message):
            seen.mark_seen(vacancy.get("id", token), vacancy, "manual_ai_archived")
            hh_pipeline.mark_terminal(vacancy.get("id", token), "closed_or_archived")
            finish("archived", apply_message or "closed_or_archived")
            print("ℹ️ Вакансия уже закрыта или в архиве")
            return {"ok": False, "message": apply_message, "closed_or_archived": True}

        if apply_result.get("already_applied"):
            seen.mark_seen(vacancy.get("id", token), vacancy, "already_applied")
            hh_pipeline.mark_terminal(vacancy.get("id", token), "already_applied")
            finish("already_applied", apply_message or "already applied")
            print("ℹ️ Уже откликались ранее")
            if apply_result.get("resume_selection_status") == "unknown_existing_response":
                await notify_needs_manual(vacancy, score, reason, note=apply_message)
            return {"ok": True, "already_applied": True, "message": apply_message}

        if apply_result.get("ok"):
            seen.mark_seen(vacancy.get("id", token), vacancy, "manual_ai_applied")
            hh_guard.record_apply_success()
            hh_pipeline.record_successful_apply(vacancy, {"name": "manual_ai", "title": "", "id": ""})
            finish("applied", apply_message or "ok")
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
        finish("uncertain" if apply_result.get("uncertain") else "failed", message)
        await notify_needs_manual(
            vacancy,
            score,
            reason,
            note=f"ИИ-отклик не завершился: {message}. Открой вручную.",
        )
        print(f"❌ ИИ-отклик не завершился: {message}")
        return {"ok": False, "uncertain": False, "message": message}

    except HHUnexpectedUI as exc:
        uncertain = bool(getattr(exc, "hh_uncertain", False) or
                         (dispatch_started and getattr(exc, "hh_recovered", None) is not True))
        if uncertain:
            message = record_manual_uncertain()
            await notify_needs_manual(vacancy, score, reason, note=message)
        else:
            message = "Нестандартный UI: вакансия оставлена для ручной проверки."
            seen.mark_seen(vacancy.get("id", token), vacancy, "manual_hh_guard_stop")
            hh_pipeline.mark_terminal(vacancy.get("id", token), "manual_hh_guard_stop")
            finish("dismissed", message)
            analytics.record_decision(run_id=run_id, vacancy=vacancy, decision="guard_stop", note="manual_ai:hh_ui")
            await notify_needs_manual(vacancy, score, reason, note=message)
        return {"ok": False, "uncertain": uncertain, "reason": "hh_unexpected_ui", "message": message}
    except asyncio.CancelledError:
        if dispatch_started:
            record_manual_uncertain()
        else:
            finish("pending", "Попытка отменена")
        raise
    except Exception as exc:
        message = f"{type(exc).__name__}: {exc}"
        if dispatch_started:
            message = record_manual_uncertain()
        else:
            finish("failed", message)
        await notify_needs_manual(
            vacancy,
            score,
            reason,
            note=message if dispatch_started else f"ИИ-отклик упал: {message}. Открой вручную.",
        )
        log.warning("Manual AI apply failed: error_kind=%s", type(exc).__name__)
        print(f"❌ ИИ-отклик упал: {message}")
        return {"ok": False, "uncertain": bool(dispatch_started), "message": message}
    finally:
        if not shutdown_started:
            try:
                await hh_client.stop()
            except Exception:
                pass
            finally:
                reconcile_manual_result()


class _HHApplyUncertain(Exception):
    """Stop consuming a preliminary HH result after its receipt changes."""


class _HHApplyReceipt:
    """Pin one apply attempt and recheck it after consumer awaits."""

    def __init__(self, vacancy, client, promote):
        self.vacancy, self.client, self.promote = vacancy, client, promote
        self.result = None
        self.owned_attempt = None
        self.classification = None
        self.promoted = False

    def capture(self, result):
        self.result = result
        if self.vacancy.get("source", "hh") == "hh":
            self.owned_attempt = getattr(self.client, "_external_attempt", None)
            if self.owned_attempt is None:
                page = getattr(self.client, "_page", None)
                monitor = getattr(page, "_hh_action_monitor", None)
                self.owned_attempt = getattr(monitor, "last_attempt", None)
        self.reconcile()

    def account(self, classification):
        self.classification = classification

    def reconcile(self):
        if self.result is None or self.vacancy.get("source", "hh") != "hh":
            return False
        self.result = apply_orchestrator.apply_result_with_current_uncertainty(
            self.vacancy, self.client, self.result, owned_attempt=self.owned_attempt)
        if apply_result_is_uncertain(self.result):
            if not self.promoted:
                self.promote(self.classification)
                self.promoted = True
            return True
        return False

    def check(self):
        if self.reconcile():
            raise _HHApplyUncertain()

    async def wait(self, operation):
        if self.reconcile():
            # The caller constructed a coroutine but it has not started.
            # Do not execute its side effect after an already-sticky receipt.
            if hasattr(operation, "close"):
                operation.close()
            raise _HHApplyUncertain()
        try:
            value = await operation
        finally:
            # Persist the upgrade even when diagnostics or shutdown fails or
            # the caller is cancelled. It must precede exception propagation.
            self.reconcile()
        self.check()
        return value


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
    before_notify=None,
    receipt=None,
    receipt_classification="manual",
    **extra_analytics,
) -> None:
    """Общий хелпер для пометки вакансии как manual (ручной разбор)."""
    source = v.get("source", "unknown")
    source_label = v.get("source_label") or _source_label(source)
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
    if receipt is not None:
        receipt.account(receipt_classification)
    if before_notify is not None:
        if receipt is not None:
            await receipt.wait(before_notify())
        else:
            await before_notify()
    if receipt is not None:
        await receipt.wait(set_hunter_status("search_manual", status_msg, "busy"))
    else:
        await set_hunter_status("search_manual", status_msg, "busy")
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
    if receipt is not None:
        await receipt.wait(notify_needs_manual(v, score, reason, note=manual_note, reply_markup=reply_markup))
    else:
        await notify_needs_manual(v, score, reason, note=manual_note, reply_markup=reply_markup)


def _build_hh_retry_cover_letter(v: dict, resume_variant: dict | None = None) -> str:
    title = str(v.get("title") or "вакансию").strip()
    company = str(v.get("company") or "").strip()
    company_part = f" в {company}" if company else ""
    resume_title = str((resume_variant or {}).get("title") or "другой вариант резюме").strip()
    return (
        f"Здравствуйте! Направляю {resume_title} на вакансию «{title}»{company_part}. "
        "Подробности моего опыта и навыков указаны в резюме. "
        "Если профиль подходит, можно обсудить задачи и формат работы."
    )


def _evaluation_with_cover_letter(
    evaluation: dict,
    cover_letter: str,
    *,
    fallback: bool | None = None,
    overclaim_guard: bool | None = None,
    grounding_status: str | None = None,
) -> dict:
    cover_meta = analyze_cover_letter(
        cover_letter,
        cover_style=str(evaluation.get("cover_style") or ""),
        fallback=fallback,
        overclaim_guard=overclaim_guard,
        grounding_status=grounding_status,
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


@_observe_search
async def do_search(dry_run: bool = False) -> dict:
    """
    Один прогон поиска + откликов.
    Возвращает {"found": int, "applied": int, "skipped": int}
    """
    result: dict = {"found": 0, "applied": 0, "skipped": 0, "deferred": 0, "source_stats": {}, "note": "", "_run_id": "",
                    "hh_recovery": {"incidents": 0, "fingerprints": [], "distinct": 0, "stopped": False, "reason": ""}}
    analytics.current_search().result = result
    deferred_queue = MatcherDeferredQueue(config.JOB_HUNTER_HOME, cooldown_seconds=config.MATCHER_DEFER_COOLDOWN_SECONDS)
    deferred_seen_path = config.SEEN_VACANCIES_FILE
    deferred_candidates = []
    hh_breaker = HHRecoveryBreaker()

    def acknowledge_processed_deferred():
        # A successful score alone is not durable handling: downstream guards,
        # cancellation or errors may still leave the vacancy pending.
        if not deferred_candidates:
            return set()
        processed = ProtectedJsonStore(deferred_seen_path, default_factory=dict).load()
        acknowledged = set()
        for candidate in deferred_candidates:
            payload = processed.get(candidate["id"])
            if not candidate.get("_hh_retry") and isinstance(payload, dict) and payload.get("action"):
                deferred_queue.resolve(candidate)
                acknowledged.add(deferred_queue.key(candidate))
        return acknowledged

    await notifier.notify_stale_cookies()
    hh_client: HHClient | None = HHClient() if config.HH_ENABLED else None
    superjob_client: SuperJobClient | None = SuperJobClient() if config.SUPERJOB_ENABLED else None
    habr_client: HabrCareerClient | None = HabrCareerClient() if config.HABR_ENABLED else None
    geekjob_client: GeekJobClient | None = GeekJobClient() if config.GEEKJOB_ENABLED else None
    from private_artifacts import state_dir_for
    diagnostic_roots = {source: state_dir_for(client, config.HH_STATE_DIR)
                        for source, client in (("hh", hh_client), ("superjob", superjob_client),
                                               ("habr", habr_client), ("geekjob", geekjob_client))}
    last_office_status: tuple[str, str, str] | None = None
    runtime_mode = "dry-run" if dry_run else "search"
    run_id = analytics.current_context()["run_id"]
    result["_run_id"] = run_id
    last_apply_attempt_started_at_by_source = defaultdict(float)
    hh_retry_vacancies: list[dict] = []
    hh_auto_apply_guard_note = ""
    hh_logged_in = False
    last_hh_receipt = None
    hh_shutdown_started = False

    async def set_hunter_status(action: str, message: str, status: str) -> None:
        nonlocal last_office_status
        analytics.search_stage(action)
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

    async def halt_hh_browser():
        terminate = getattr(hh_client, "hard_stop_browser", None)
        if terminate is not None:
            if await terminate() is not True:
                raise RuntimeError("HH: завершение браузера не подтверждено; поиск остановлен")

    try:
        hh_startup_error = None
        if hh_client is not None:
            try:
                await hh_client.start()
                hh_logged_in = bool(await hh_client.is_logged_in())
                if not hh_logged_in:
                    hh_breaker.hard_stop("HH authentication was not proven")
                if hh_logged_in:
                    negotiation_statuses = await hh_client.get_negotiation_statuses()
                    analytics.record_negotiation_statuses(negotiation_statuses)
                    if hh_pipeline.enabled():
                        resumes = await hh_client.get_resume_ids()
                        hh_pipeline.remember_resolved_variants(
                            hh_pipeline.resolve_variants(resumes)
                        )
                        hh_pipeline.sync_negotiation_statuses(negotiation_statuses)
                        hh_retry_vacancies = hh_pipeline.get_retry_candidates()
                        blocked_retry_ids = {
                            str(key).split(":", 1)[-1]
                            for key, item in seen.all_entries().items()
                            if (item or {}).get("action") in {"apply_uncertain", "manual_hh_guard_stop"}
                        }
                        hh_retry_vacancies = [
                            vacancy for vacancy in hh_retry_vacancies
                            if str(vacancy.get("id", "")) not in blocked_retry_ids
                        ]
                        if hh_retry_vacancies:
                            log.info(
                                "Prepared %d hh retry candidates for staged resumes",
                                len(hh_retry_vacancies),
                            )
            except HHUnexpectedUI as exc:
                hh_startup_error = exc
                hh_breaker.hard_stop("HH UI/authentication state could not be proven")
                log.warning("HH startup blocked by unexpected UI: %s", getattr(exc, "fingerprint", ""))
            except Exception as e:
                hh_breaker.hard_stop("HH: состояние сессии не подтверждено")
                log.warning("Failed to prepare hh staged resume pipeline: %s", e)

        if hh_breaker.stopped:
            result["hh_recovery"] = hh_breaker.snapshot()
            await halt_hh_browser()

        if (hh_startup_error is not None and not any((config.SUPERJOB_ENABLED,
                config.HABR_ENABLED, config.GEEKJOB_ENABLED))):
            result["hh_recovery"] = hh_breaker.snapshot()
            result["note"] = str(hh_startup_error)
            result["source_stats"].setdefault("hh", {})["stop_reason"] = hh_breaker.reason
            await set_hunter_status("hh_ui_blocked", str(hh_startup_error), "busy")
            await notify_summary(0, 0, 0, result["source_stats"], dry_run=dry_run)
            _record_search_run(result, dry_run=dry_run, ok=False, error="hh_unexpected_ui")
            return result

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

        if not dry_run:
            await notify_search_started(enabled_sources)

        all_vacancies = await search_pipeline.collect_all(
            hh_client, superjob_client, habr_client, geekjob_client,
            hh_retry_vacancies=hh_retry_vacancies,
            source_stats=result["source_stats"],
            status_callback=set_hunter_status,
            **({"hh_stop_reason": hh_breaker.reason} if hh_breaker.stopped else {}),
        )
        hh_collection_stop = result["source_stats"].get("hh", {}).get("stop_reason")
        if hh_collection_stop:
            hh_breaker.hard_stop(hh_collection_stop)
            result["hh_recovery"] = hh_breaker.snapshot()
            await halt_hh_browser()
        result["hh_recovery"] = hh_breaker.snapshot()

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
        enabled_source_keys = {source for source, enabled in (
            ("hh", config.HH_ENABLED), ("superjob", config.SUPERJOB_ENABLED),
            ("habr", config.HABR_ENABLED), ("geekjob", config.GEEKJOB_ENABLED),
        ) if enabled}
        all_vacancies = deferred_queue.merge_ready(all_vacancies, enabled_source_keys)
        deferred_candidates = [v for v in all_vacancies if v.get("_matcher_deferred_revision")]
        already_processed = acknowledge_processed_deferred()
        all_vacancies = [v for v in all_vacancies if deferred_queue.key(v) not in already_processed]
        analytics.register_candidates(all_vacancies)
        safety_entries = seen.all_entries()
        unblocked_vacancies = []
        for vacancy in all_vacancies:
            if safety_entries.get(str(vacancy.get("id", "")), {}).get("action") in {
                    "apply_uncertain", "manual_hh_guard_stop"}:
                analytics.record_decision(run_id=run_id, vacancy=vacancy, decision="not_processed",
                                          note="manual_verification_required")
            else:
                unblocked_vacancies.append(vacancy)
        all_vacancies = unblocked_vacancies
        for vacancy in all_vacancies:
            if not vacancy.get("_hh_retry"):
                search_pipeline.get_source_bucket(result["source_stats"], vacancy)["new"] += 1

        log.info("Found %d unique vacancies before keyword filter", len(all_vacancies))
        await set_hunter_status("search_filter", f"Фильтр {len(all_vacancies)} вакансий", "thinking")

        # Keyword-фильтрация
        before_keyword_filter = all_vacancies
        all_vacancies = search_pipeline.keyword_filter(all_vacancies, result["source_stats"], run_id)
        retained_keys = {deferred_queue.key(v) for v in all_vacancies}
        for vacancy in before_keyword_filter:
            if deferred_queue.key(vacancy) not in retained_keys and vacancy.get("_matcher_deferred_revision"):
                deferred_queue.resolve(vacancy)
        result["found"] = len(all_vacancies)

        log.info("Keyword pass: %d vacancies", len(all_vacancies))
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
            if hh_breaker.stopped:
                await notify_summary(0, 0, 0, result["source_stats"], dry_run=dry_run)
            _record_search_run(result, dry_run=dry_run, ok=True)
            return result

        if config.HH_ENABLED:
            hh_can_auto_apply, hh_guard_note = hh_guard.can_auto_apply()
            if not hh_can_auto_apply:
                hh_auto_apply_guard_note = hh_guard_note

        applied_count = 0
        auto_applied_count_by_source = defaultdict(int)
        llm_issue_alert_sent = False
        shadow_verifier = ShadowVerifier()
        processed_by_source = defaultdict(int)
        habr_logged_in: bool | None = None
        superjob_ready: bool | None = None
        geekjob_ready: bool | None = None
        geekjob_ready_message = ""
        def consumer_receipt(v, vid, bucket, evaluation, details, resume_variant):
            def promote(classification):
                nonlocal applied_count
                # Withdraw this vacancy's prior classification exactly once.
                # Manual/skipped counters already written before a shutdown
                # await remain in place when guard_stop becomes uncertain.
                if classification == "applied":
                    result["applied"] -= 1
                    bucket["applied"] -= 1
                    applied_count -= 1
                    auto_applied_count_by_source["hh"] -= 1
                elif classification == "rejected":
                    bucket["rejected"] -= 1
                elif classification == "guard":
                    bucket["guard_stop"] -= 1
                if classification not in {"manual", "guard", "rejected"}:
                    result["skipped"] += 1
                if classification not in {"manual", "guard"}:
                    bucket["manual"] += 1
                bucket["uncertain"] = bucket.get("uncertain", 0) + 1
                seen.mark_seen(vid, v, "apply_uncertain")
                hh_pipeline.mark_terminal(vid, "apply_uncertain")
                hh_breaker.hard_stop("HH: исход отклика неизвестен; требуется ручная сверка")
                result["hh_recovery"] = hh_breaker.snapshot()
                bucket["stop_reason"] = hh_breaker.reason
                analytics.record_decision(
                    run_id=run_id, vacancy=v, decision=DECISION_APPLY_UNCERTAIN,
                    evaluation=evaluation, details=details, resume_variant=resume_variant,
                    note="hh:apply_uncertain")
            return _HHApplyReceipt(v, hh_client, promote)

        async def notify_hh_uncertain(receipt, v, score, reason):
            note = ("Исход отклика не подтверждён. Отклик мог быть отправлен; "
                    "проверьте историю откликов вручную. Автоматического повтора нет. HH остановлен.")
            try:
                await halt_hh_browser()
            finally:
                receipt.reconcile()
            try:
                await set_hunter_status("search_manual", "Ручная сверка: исход отклика неизвестен", "busy")
            finally:
                receipt.reconcile()
            create_task(f"Ручная сверка: {v['title']} @ {v['company']}",
                        f"URL: {v.get('url', '')}\n{note}", "medium")
            try:
                await notify_needs_manual(v, score, reason, note=note)
            finally:
                receipt.reconcile()

        async def record_hh_ui_incident(v, vid, bucket, score=0, reason="", evaluation=None,
                                        details="", fingerprint="", recovered=False, message="", receipt=None):
            if recovered:
                hh_breaker.recovered_incident(fingerprint)
            else:
                hh_breaker.hard_stop(message or "HH UI recovery was not proven")
            result["hh_recovery"] = hh_breaker.snapshot()
            if hh_breaker.stopped:
                bucket["stop_reason"] = hh_breaker.reason
            bucket["guard_stop"] = bucket.get("guard_stop", 0) + 1
            hh_pipeline.mark_terminal(vid, "manual_hh_guard_stop")
            analytics.record_event({"event": "hh_ui_isolation", "source": "hh", "vacancy_id": str(vid),
                                    "fingerprint": fingerprint, "recovered": recovered,
                                    **hh_breaker.snapshot()})
            message = message or "Вакансия оставлена без дальнейших действий; нужна ручная проверка."
            if hh_breaker.stopped:
                message += (f" HH остановлен: {hh_breaker.reason}. Восстановлений: {hh_breaker.incidents}; "
                            f"различных fingerprint: {len(hh_breaker.fingerprints)}.")
            await _mark_manual(
                "Ручная проверка HH: нестандартный UI",
                "manual_hh_guard_stop",
                "guard_stop",
                message,
                v, vid, score, reason, evaluation or {}, details,
                result, bucket, run_id, set_hunter_status,
                analytics_note=("hh:recovered_ui" if recovered else "hh:ui_hard_stop"),
                before_notify=halt_hh_browser if hh_breaker.stopped else None,
                receipt=receipt, receipt_classification="guard",
            )
            if hh_breaker.stopped:
                await set_hunter_status("hh_ui_blocked", hh_breaker.reason, "busy")

        async def handle_hh_unexpected(exc, v, vid, bucket, *, score=0, reason="", evaluation=None, details="", receipt=None):
            if v.get("source", "hh") != "hh":
                raise exc
            if getattr(exc, "hh_uncertain", False):
                if receipt is not None:
                    receipt.result = {**receipt.result, "ok": False, "uncertain": True}
                    receipt.check()
                hh_breaker.hard_stop("HH: возможный отклик требует ручной сверки")
                result["hh_recovery"] = hh_breaker.snapshot()
                bucket["stop_reason"] = hh_breaker.reason
                bucket["uncertain"] = bucket.get("uncertain", 0) + 1
                hh_pipeline.mark_terminal(vid, "apply_uncertain")
                await _mark_manual(
                    "Ручная сверка: исход отклика неизвестен", "apply_uncertain", DECISION_APPLY_UNCERTAIN,
                    "Отклик мог быть отправлен. Проверьте историю вручную; автоматического повтора нет. HH остановлен.",
                    v, vid, score, reason, evaluation or {}, details, result, bucket, run_id, set_hunter_status,
                    analytics_note="hh:apply_uncertain", before_notify=halt_hh_browser)
                return
            recovered = getattr(exc, "hh_recovered", None)
            if recovered is None:
                recover = getattr(hh_client, "recover_unexpected_ui", None)
                recovered = await recover(exc) if recover is not None else False
            if getattr(exc, "hh_uncertain", False):
                # Recovery may discover a request or ownership loss. Re-enter
                # only the uncertainty branch; never attempt another recovery.
                await handle_hh_unexpected(exc, v, vid, bucket, score=score, reason=reason,
                                           evaluation=evaluation, details=details, receipt=receipt)
                return
            await record_hh_ui_incident(
                v, vid, bucket, score=score, reason=reason, evaluation=evaluation, details=details,
                fingerprint=getattr(exc, "fingerprint", ""), recovered=recovered is True,
                message=getattr(exc, "hh_stop_reason", "") or "Нестандартный UI: вакансия оставлена для ручной проверки.",
                receipt=receipt)

        for v in all_vacancies:
            if (
                config.MAX_APPLICATIONS_PER_RUN > 0
                and applied_count >= config.MAX_APPLICATIONS_PER_RUN
            ):
                log.info("Reached max applications limit (%d)", config.MAX_APPLICATIONS_PER_RUN)
                analytics.current_search().stop_reason = "run_limit"
                break

            analytics.search_stage("pipeline", v)
            if company_blacklist.is_blocked(v.get("company", "")):
                log.info("Skipped vacancy=%s reason=company_blacklisted", v.get("id"))
                result["skipped"] += 1
                bucket = search_pipeline.get_source_bucket(result["source_stats"], v)
                bucket["blacklisted"] = bucket.get("blacklisted", 0) + 1
                analytics.record_decision(run_id=run_id, vacancy=v, decision="skipped_blacklisted")
                continue

            v["_analytics_run_id"] = run_id
            v["_analytics_apply_mode"] = "auto"
            vid = v["id"]
            source = v.get("source", "hh")
            bucket = search_pipeline.get_source_bucket(result["source_stats"], v)
            if source == "hh" and hh_breaker.stopped:
                result["hh_recovery"] = hh_breaker.snapshot()
                bucket["stop_reason"] = hh_breaker.reason
                result["skipped"] += 1
                bucket["guard_stop"] = bucket.get("guard_stop", 0) + 1
                analytics.record_decision(run_id=run_id, vacancy=v, decision="not_processed",
                                          note="hh_recovery_breaker")
                continue
            processed_by_source[source] += 1
            source_index = processed_by_source[source]
            source_total = relevant_counts.get(source, 0)

            log.info("Evaluating source=%s vacancy=%s", source, vid)
            if source_index == 1 or source_index == source_total or source_index % 5 == 0:
                await set_hunter_status(
                    "search_evaluate",
                    _format_source_progress("Проверяю", source, source_index, source_total),
                    "thinking",
                )

            # Получаем детали
            try:
                details = await analytics.tracked_call("details", run_id, v,
                    apply_orchestrator.fetch_vacancy_details,
                    v, hh_client, superjob_client, habr_client, geekjob_client,
                )
            except HHUnexpectedUI as exc:
                await handle_hh_unexpected(exc, v, vid, bucket)
                continue

            if _looks_like_closed_or_archived(v, details):
                log.info("  Skipped (closed/archived vacancy)")
                evaluation = {
                    "score": 0,
                    "reason": "Вакансия закрыта или находится в архиве",
                    "red_flags": ["closed_or_archived"],
                    "should_apply": False,
                }
                seen.mark_seen(vid, v, "skipped_archived")
                if v.get("_matcher_deferred_revision"):
                    deferred_queue.resolve(v)
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
                evaluation = await analytics.tracked_call("matcher", run_id, v, evaluate_vacancy, v, details)
            if is_deferred_evaluation(evaluation):
                evaluation = {**evaluation, "score": None, "should_apply": False,
                              "evaluation_status": "deferred_unscored"}
                deferred_queue.defer(v, details, evaluation.get("error_kind") or "llm_error")
                result["deferred"] += 1
                bucket["deferred_unscored"] = bucket.get("deferred_unscored", 0) + 1
                result["note"] = f"Оценка отложена: LLM quota/rate-limit или ошибка провайдера ({result['deferred']} вакансий)"
                analytics.record_decision(run_id=run_id, vacancy=v, decision=DECISION_DEFERRED_UNSCORED,
                                          evaluation=evaluation, details=details, note=evaluation.get("error_kind") or "llm_error")
                await set_hunter_status("search_deferred_unscored", result["note"], "thinking")
                if not llm_issue_alert_sent:
                    await notifier.notify_llm_issue(v, evaluation, source_index=source_index, source_total=source_total)
                    llm_issue_alert_sent = True
                continue
            await shadow_verifier.check(run_id, v, details, evaluation)
            score = evaluation.get("score", 0)
            reason = evaluation.get("reason", "")
            red_flags = evaluation.get("red_flags", [])

            log.info("  Score: %d | red_flag_count=%d", score, len(red_flags))

            if red_flags:
                log.warning("  Matcher rejected: reason=red_flags count=%d", len(red_flags))
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
                    allow_ai_apply = getattr(config, "HH_APPLICATION_MODE", "auto") != "preview"
                    profile_name = profile_mod.active().name
                    candidate = manual_apply_queue.create_candidate(
                        v,
                        evaluation,
                        details,
                        profile_name=profile_name,
                        allow_ai_apply=allow_ai_apply,
                    )
                    token = candidate.get("token", "")
                    reply_markup = manual_apply_queue.build_manual_apply_markup(v, profile_name, token, allow_ai_apply=allow_ai_apply)
                    if source == "hh":
                        note = (
                            "Желтая зона: вакансия не прошла автоотклик, но score достаточно высокий для ручного решения. "
                            + ("Можно открыть самому или нажать 'Откликнуться с ИИ'." if allow_ai_apply else "Включён режим просмотра: можно открыть самому.")
                        )
                    else:
                        note = (
                            "Желтая зона: вакансия не прошла автоотклик, но score достаточно высокий для ручного решения. "
                            "Для этого источника оставляю ссылку на ручную проверку."
                        )
                    log.info("  Manual review: reason=yellow_zone")
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

            analytics.count_stage("matcher_pass", v)
            hh_resume_variant = None
            if source == "hh" and hh_pipeline.enabled():
                if v.get("_hh_resume_variant"):
                    hh_resume_variant = hh_pipeline.get_variant_by_name(v["_hh_resume_variant"])
                if hh_resume_variant is None:
                    hh_resume_variant = hh_pipeline.get_next_variant(vid, evaluation.get("cluster"))

            if dry_run:
                log.info("  [DRY RUN] Matched vacancy=%s score=%d", vid, score)
                seen.mark_seen(vid, v, f"dry_run_{source}")
                result["applied"] += 1
                bucket["dry_run_match"] = bucket.get("dry_run_match", 0) + 1
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

            # HH can be configured per user to only show suitable jobs, or to
            # wait for an explicit Telegram confirmation before AI applies.
            hh_application_mode = getattr(config, "HH_APPLICATION_MODE", "auto")
            if source == "hh" and hh_application_mode in {"preview", "confirm"}:
                is_confirmation = hh_application_mode == "confirm"
                profile_name = profile_mod.active().name
                candidate = manual_apply_queue.create_candidate(
                    v,
                    evaluation,
                    details,
                    profile_name=profile_name,
                    allow_ai_apply=is_confirmation,
                )
                token = candidate.get("token", "")
                reply_markup = manual_apply_queue.build_manual_apply_markup(
                    v,
                    profile_name,
                    token,
                    allow_ai_apply=is_confirmation,
                )
                mode_label = "подтверждение" if is_confirmation else "просмотр"
                action_note = (
                    "Нажмите «Откликнуться с ИИ», чтобы отправить отклик."
                    if is_confirmation
                    else "Отклик не отправляется в режиме просмотра. Откройте вакансию сами."
                )
                await _mark_manual(
                    f"Ручной HH: {mode_label}",
                    f"manual_hh_{hh_application_mode}",
                    f"manual_hh_{hh_application_mode}",
                    f"Режим «{mode_label}». {action_note}",
                    v, vid, score, reason, evaluation, details,
                    result, bucket, run_id, set_hunter_status,
                    resume_variant=hh_resume_variant,
                    reply_markup=reply_markup,
                    analytics_note=f"hh_application_mode:{hh_application_mode}",
                )
                continue

            if source == "hh":
                if not hh_auto_apply_guard_note:
                    hh_can_auto_apply, hh_guard_note = hh_guard.can_auto_apply()
                    if not hh_can_auto_apply:
                        hh_auto_apply_guard_note = hh_guard_note
                if hh_auto_apply_guard_note:
                    # hh на anti-bot/rolling-limit guard: тихо откладываем без manual-задачи и notify.
                    # Не помечаем seen → вакансия подхватится на следующем прогоне после паузы.
                    log.info(
                        "  hh deferred: vacancy=%s reason=guard_stop", vid,
                    )
                    result["skipped"] += 1
                    bucket["deferred"] = bucket.get("deferred", 0) + 1
                    analytics.record_decision(run_id=run_id, vacancy=v, decision="guard_stop")
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
                        "  hh deferred (per-run limit): vacancy=%s", vid,
                    )
                    result["skipped"] += 1
                    bucket["deferred"] = bucket.get("deferred", 0) + 1
                    analytics.record_decision(run_id=run_id, vacancy=v, decision="guard_stop")
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
            apply_trace = None
            if source == "hh":
                apply_trace = apply_orchestrator.create_hh_apply_trace(
                    v,
                    mode=str(v.get("_analytics_apply_mode") or "auto"),
                )
                if apply_trace is not None:
                    apply_trace.event(
                        "HH_SESSION_CHECK",
                        ok=bool(hh_logged_in),
                        authenticated=bool(hh_logged_in),
                        url=getattr(getattr(hh_client, "_page", None), "url", ""),
                    )
            cover_limit = apply_orchestrator.get_cover_letter_limit(source)
            cover = await analytics.tracked_call("cover_letter", run_id, v, generate_cover_letter, v, details)
            cover = cover or ""
            cover_fallback_used = False
            if not (cover or "").strip() and v.get("_hh_retry"):
                cover = _build_hh_retry_cover_letter(v, hh_resume_variant)
                cover_fallback_used = True
                log.info("  hh retry fallback cover letter used for %s", vid)
            cover_meta = analyze_cover_letter(cover)
            if len(cover) > cover_limit:
                cover = cover[:cover_limit]
            if apply_trace is not None:
                apply_trace.event(
                    "COVER_LETTER_GENERATED",
                    ok=bool(cover.strip()),
                    expected=True,
                    generated=bool(cover.strip()),
                    chars=len(cover),
                    grounding_status=cover_meta["grounding_status"],
                    fallback=cover_meta["fallback_cover_letter"] or cover_fallback_used,
                )
            cover_evaluation = _evaluation_with_cover_letter(
                evaluation,
                cover,
                fallback=cover_fallback_used or None,
                grounding_status=cover_meta["grounding_status"],
            )
            if not (cover or "").strip():
                if apply_trace is not None:
                    apply_trace.finish(
                        ok=False,
                        message="LLM не сгенерировал сопроводительное письмо",
                    )
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
            log.info("  %s cover prepared: vacancy=%s chars=%d", source_label, vid, len(cover or ""))

            # 5. Пауза перед откликом
            if source == "hh":
                await wait_before_auto_apply(source, config.HH_MIN_SECONDS_BETWEEN_APPLICATIONS)
                last_apply_attempt_started_at_by_source[source] = asyncio.get_running_loop().time()
            elif source == "habr":
                await wait_before_auto_apply(source, config.HABR_MIN_SECONDS_BETWEEN_APPLICATIONS)
                last_apply_attempt_started_at_by_source[source] = asyncio.get_running_loop().time()

            receipt = consumer_receipt(v, vid, bucket, cover_evaluation, details, hh_resume_variant)
            if source == "hh":
                last_hh_receipt = receipt
            try:
                # 6. Отправляем отклик
                try:
                    analytics.count_stage("apply_attempt", v)
                    apply_result = await analytics.tracked_call("apply", run_id, v,
                        apply_orchestrator.dispatch_apply, v, cover,
                        hh_client, superjob_client, habr_client, geekjob_client,
                        preferred_resume_title=(hh_resume_variant or {}).get("title", ""),
                        preferred_resume_id=(hh_resume_variant or {}).get("id", ""),
                        trace=apply_trace,
                    )
                except asyncio.CancelledError:
                    receipt.capture({"ok": False})
                    raise
                except HHUnexpectedUI as exc:
                    receipt.capture({"ok": False, "uncertain": bool(getattr(exc, "hh_uncertain", False))})
                    receipt.check()
                    await receipt.wait(handle_hh_unexpected(exc, v, vid, bucket, score=score, reason=reason,
                                               evaluation=cover_evaluation, details=details, receipt=receipt))
                    continue
                except Exception as e:
                    receipt.capture({"ok": False, "uncertain": bool(getattr(e, "hh_uncertain", False))})
                    receipt.check()
                    if source == "hh" and hasattr(e, "hh_recovered"):
                        incident = HHUnexpectedUI("apply_exception", hashlib.sha256(type(e).__name__.encode()).hexdigest())
                        incident.hh_recovered = e.hh_recovered
                        incident.hh_uncertain = getattr(e, "hh_uncertain", False)
                        incident.hh_stop_reason = getattr(e, "hh_stop_reason", "")
                        await receipt.wait(handle_hh_unexpected(incident, v, vid, bucket, score=score, reason=reason,
                                                   evaluation=cover_evaluation, details=details, receipt=receipt))
                        continue
                    analytics.record_failure("apply", e, source=source, continued=True)
                    snapshot = await receipt.wait(_save_autoapply_failure_snapshot(
                        source,
                        vid,
                        _autoapply_page_for_source(
                            source,
                            hh_client,
                            superjob_client,
                            habr_client,
                            geekjob_client,
                        ),
                        state_dir=diagnostic_roots[source],
                    ))
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
                        receipt.account("rejected")
                        await receipt.wait(set_hunter_status(
                            "search_skip_existing",
                            "HH вакансия в архиве, ручную задачу не создаю",
                            "working",
                        ))
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
                        await receipt.wait(record_hh_ui_incident(
                            v, vid, bucket, score=score, reason=reason,
                            evaluation=cover_evaluation, details=details,
                            message=f"HH остановлен: {anti_bot_kind}; нужна ручная проверка.", receipt=receipt,
                        ))
                        continue
                    await receipt.wait(set_hunter_status("search_manual", f"Ручной {short_label}: ошибка", "busy"))
                    seen.mark_seen(vid, v, f"apply_failed_exception:{type(e).__name__}")
                    result["skipped"] += 1
                    bucket["manual"] += 1
                    receipt.account("manual")
                    analytics.record_decision(
                        run_id=run_id,
                        vacancy=v,
                        decision=DECISION_APPLY_FAILED_EXCEPTION,
                        evaluation=cover_evaluation,
                        details=details,
                        resume_variant=hh_resume_variant,
                        note=f"{source}:{type(e).__name__}" + (f"; {hh_auto_apply_guard_note}" if guard_suffix else ""),
                    )
                    log.warning("  %s apply crashed for %s: %s", source_label, vid, type(e).__name__)
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
                    await receipt.wait(notify_needs_manual(
                        v, score, reason,
                        note=(
                            f"Автоотклик {source_label} упал: {type(e).__name__}. Проверь вручную."
                            + (f" {hh_auto_apply_guard_note}" if guard_suffix else "")
                            + (f" Снимок: {snapshot['screenshot']}" if snapshot.get("screenshot") else "")
                        ),
                        screenshot_path=snapshot.get("screenshot"),
                    ))
                    continue

                receipt.capture(apply_result)
                receipt.check()

                log.info("  %s apply result: vacancy=%s ok=%s uncertain=%s questions=%s", source_label, vid,
                         bool(apply_result.get("ok")), bool(apply_result.get("uncertain")),
                         bool(apply_result.get("requires_questions")))
                if apply_result_is_uncertain(apply_result):
                    apply_result = {**apply_result, "ok": False, "uncertain": True}
                    bucket["uncertain"] = bucket.get("uncertain", 0) + 1
                    uncertainty_note = (
                        "Исход отклика не подтверждён. Отклик мог быть отправлен; проверьте историю откликов вручную. Автоматического повтора нет."
                    )
                    if source == "hh":
                        hh_pipeline.mark_terminal(vid, "apply_uncertain")
                        hh_breaker.hard_stop("HH: исход отклика неизвестен; требуется ручная сверка")
                        result["hh_recovery"] = hh_breaker.snapshot()
                        bucket["stop_reason"] = hh_breaker.reason
                        uncertainty_note += " HH остановлен."
                    await receipt.wait(_mark_manual(
                        "Ручная сверка: исход отклика неизвестен",
                        "apply_uncertain",
                        DECISION_APPLY_UNCERTAIN,
                        uncertainty_note,
                        v, vid, score, reason, cover_evaluation, details,
                        result, bucket, run_id, set_hunter_status,
                        resume_variant=hh_resume_variant,
                        analytics_note=f"{source}:apply_uncertain",
                        before_notify=halt_hh_browser if source == "hh" else None,
                    ))
                    continue
                if source == "hh" and apply_result.get("hh_hard_stop"):
                    await receipt.wait(record_hh_ui_incident(
                        v, vid, bucket, score=score, reason=reason, evaluation=cover_evaluation, details=details,
                        message=apply_result.get("hh_recovery_reason") or "HH: безопасное восстановление не доказано", receipt=receipt))
                    continue
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
                    receipt.account("rejected")
                    await receipt.wait(set_hunter_status(
                        "search_skip_existing",
                        "HH вакансия в архиве, ручную задачу не создаю",
                        "working",
                    ))
                    log.info("  hh skipped closed/archived vacancy after apply result for %s", vid)
                    continue

                # 7. hh-специфика: вопросы работодателя
                if source == "hh" and "пропускаем" in apply_result.get("message", "").lower():
                    snapshot = await receipt.wait(_save_autoapply_failure_snapshot(
                        source,
                        vid,
                        _autoapply_page_for_source(
                            source,
                            hh_client,
                            superjob_client,
                            habr_client,
                            geekjob_client,
                        ),
                        state_dir=diagnostic_roots[source],
                    ))
                    await receipt.wait(set_hunter_status("search_manual", "Ручной hh: вопросы", "busy"))
                    seen.mark_seen(vid, v, "skipped_questions")
                    result["skipped"] += 1
                    bucket["manual"] += 1
                    receipt.account("manual")
                    analytics.record_decision(
                        run_id=run_id,
                        vacancy=v,
                        decision=DECISION_QUESTIONS_REQUIRED,
                        evaluation=cover_evaluation,
                        details=details,
                        resume_variant=hh_resume_variant,
                    )
                    log.info("  Skipped: employer requires extra questions")
                    await receipt.wait(notify_needs_manual(
                        v,
                        score,
                        reason,
                        note=(
                            f"Нужен ручной отклик: работодатель добавил вопросы."
                            + (f" {apply_note_text}." if apply_note_text else "")
                            + (f" Снимок: {snapshot['screenshot']}" if snapshot.get("screenshot") else "")
                        ),
                        screenshot_path=snapshot.get("screenshot"),
                    ))
                    continue

                # 8. Обработка результата
                if apply_result.get("already_applied"):
                    if apply_result.get("resume_selection_status") == "unknown_existing_response":
                        await receipt.wait(notify_needs_manual(v, score, reason, note=str(apply_result.get("message") or "Исходное HH-резюме не подтверждено")))
                    seen.mark_seen(vid, v, "already_applied")
                    if source == "hh":
                        hh_pipeline.mark_terminal(vid, "already_applied")
                    result["skipped"] += 1
                    bucket["rejected"] += 1
                    receipt.account("rejected")
                    analytics.record_decision(
                        run_id=run_id,
                        vacancy=v,
                        decision=DECISION_ALREADY_APPLIED,
                        evaluation=cover_evaluation,
                        details=details,
                        resume_variant=hh_resume_variant,
                        note=f"{source}:{apply_result.get('message', 'already applied')}",
                    )
                    await receipt.wait(set_hunter_status(
                        "search_skip_existing",
                        f"Уже откликался {short_label}",
                        "working",
                    ))
                    log.info("  %s vacancy already has a response: %s", source_label, vid)
                    continue

                cover_evaluation["cover_letter_status"] = apply_result.get("cover_letter_status", "unknown")
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
                    receipt.account("applied")
                    analytics.record_decision(
                        run_id=run_id,
                        vacancy=v,
                        decision=DECISION_APPLIED_AUTO,
                        evaluation=cover_evaluation,
                        details=details,
                        resume_variant=hh_resume_variant,
                        note=f"{source}:{apply_note_text}" if apply_note_text else "",
                    )
                    await receipt.wait(set_hunter_status(
                        "search_apply_done",
                        f"Отправил {short_label} {auto_applied_count_by_source[source]}",
                        "working",
                    ))

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
                        await receipt.wait(task_complete(task_id, f"Отклик {source_label} отправлен (score {score})"))

                    await receipt.wait(notify_application(v, score, cover, note=apply_note_text or None))
                else:
                    apply_message = str(
                        apply_result.get("message")
                        or "HH не подтвердил отправку отклика после нажатия кнопки"
                    )
                    if source == "hh" and "не удалось подтвердить отклик" in str(apply_message).casefold():
                        log.warning(
                            "  hh apply unconfirmed; escalating to manual task for %s",
                            vid,
                        )
                    snapshot = await receipt.wait(_save_autoapply_failure_snapshot(
                        source,
                        vid,
                        _autoapply_page_for_source(
                            source,
                            hh_client,
                            superjob_client,
                            habr_client,
                            geekjob_client,
                        ),
                        state_dir=diagnostic_roots[source],
                    ))
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
                        receipt.account("rejected")
                        await receipt.wait(set_hunter_status(
                            "search_skip_existing",
                            "HH вакансия в архиве, ручную задачу не создаю",
                            "working",
                        ))
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
                        await receipt.wait(record_hh_ui_incident(
                            v, vid, bucket, score=score, reason=reason,
                            evaluation=cover_evaluation, details=details,
                            message=f"HH остановлен: {anti_bot_kind}; нужна ручная проверка.", receipt=receipt,
                        ))
                        continue

                    await receipt.wait(set_hunter_status("search_manual", f"Ручной {short_label}: не ушёл", "busy"))
                    seen.mark_seen(vid, v, f"apply_failed:{apply_message}")
                    result["skipped"] += 1
                    bucket["manual"] += 1
                    receipt.account("manual")
                    analytics.record_decision(
                        run_id=run_id,
                        vacancy=v,
                        decision=DECISION_APPLY_FAILED,
                        evaluation=cover_evaluation,
                        details=details,
                        resume_variant=hh_resume_variant,
                        note=f"{source}:{apply_message}" + (f"; {hh_auto_apply_guard_note}" if guard_suffix else ""),
                    )
                    log.warning("  %s apply failed: vacancy=%s", source_label, vid)
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
                    await receipt.wait(notify_needs_manual(
                        v, score, reason,
                        note=(
                            f"Автоотклик {source_label} не завершился: {apply_message}"
                            + (f" {hh_auto_apply_guard_note}" if guard_suffix else "")
                            + (" Ниже — снимок страницы после сбоя." if snapshot.get("screenshot") else "")
                        ),
                        screenshot_path=snapshot.get("screenshot"),
                    ))

                # Пауза между откликами
                await receipt.wait(asyncio.sleep(3))
            except _HHApplyUncertain:
                await notify_hh_uncertain(receipt, v, score, reason)
                continue

        # После поиска — заодно отвечаем в hh-чатах AI-помощникам,
        # пока браузер уже открыт. Включается флагом HH_CHAT_RESPONDER_ENABLED.
        if (not dry_run and config.HH_CHAT_RESPONDER_ENABLED and hh_client is not None
                and not hh_breaker.stopped):
            try:
                import hh_chat_responder as cr
                chat_summary = await cr.process_all(hh_client)
                logging.getLogger("chat_responder").info(
                    "chat-respond piggyback: scanned=%d with_ai=%d sent=%d skipped=%d read_failed=%d",
                    chat_summary.get("chats_scanned", 0),
                    chat_summary.get("with_ai", 0),
                    chat_summary.get("answers_sent", 0),
                    chat_summary.get("skipped", 0),
                    chat_summary.get("read_failures", 0),
                )
            except HHUnexpectedUI:
                raise
            except Exception as exc:
                logging.getLogger("chat_responder").warning("chat-responder failed: %s", type(exc).__name__)

        if hh_client:
            hh_shutdown_started = True
            try:
                await hh_client.stop()
            finally:
                if last_hh_receipt is not None:
                    last_hh_receipt.reconcile()

        status_msg = f"Поиск завершён: найдено {result['found']}, откликов {result['applied']}, пропущено {result['skipped']}"
        if result["deferred"]:
            status_msg += f"; оценка отложена (LLM quota/rate-limit/provider): {result['deferred']}"
        await set_hunter_status("search_done", status_msg, "idle")
        await notify_summary(
            result["found"],
            result["applied"],
            result["skipped"],
            result["source_stats"],
            dry_run=dry_run,
        )
        if not dry_run and result["applied"] > 0 and config.TELEGRAM_NOTIFY_AUTO_DIGEST:
            await notify_digest(analytics.summarize())
        _record_search_run(result, dry_run=dry_run, ok=True)

    except asyncio.CancelledError as exc:
        analytics.record_failure(analytics.current_search().stage, exc, continued=False)
        raise
    except HHUnexpectedUI as exc:
        analytics.record_failure(exc.stage, exc, source="hh", continued=False)
        result["note"] = str(exc)
        await set_hunter_status("hh_ui_blocked", str(exc), "busy")
        _record_search_run(result, dry_run=dry_run, ok=False, error="hh_unexpected_ui")
    except Exception as e:
        analytics.record_failure(analytics.current_search().stage, e, continued=False)
        log.error("Search failed: %s", type(e).__name__)
        await set_hunter_status("error", f"Ошибка поиска: {e}", "idle")
        _record_search_run(result, dry_run=dry_run, ok=False, error=type(e).__name__)
    finally:
        analytics.search_stage("cleanup")
        try:
            acknowledge_processed_deferred()
        except Exception as exc:
            log.warning("Deferred queue acknowledgement failed; records retained: %s", type(exc).__name__)
        if hh_client and not hh_shutdown_started:
            try:
                await hh_client.stop()
            finally:
                if last_hh_receipt is not None:
                    last_hh_receipt.reconcile()
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


async def do_trace_apply(vacancy_value: str, *, confirm_real: bool = False) -> dict:
    """Run one real HH application with an isolated structured trace."""
    if not confirm_real:
        result = {
            "ok": False,
            "message": "trace-apply отправляет реальный отклик; повтори команду с --confirm-real",
        }
        print(f"❌ {result['message']}")
        return result

    match = re.search(r"(?:vacancy/)?(\d+)", str(vacancy_value or "").strip())
    if not match:
        result = {"ok": False, "message": "Укажи числовой HH vacancy ID или URL вакансии"}
        print(f"❌ {result['message']}")
        return result

    vacancy_id = match.group(1)
    vacancy = {
        "id": vacancy_id,
        "source": "hh",
        "url": f"https://hh.ru/vacancy/{vacancy_id}",
        "_analytics_apply_mode": "trace-apply",
    }
    trace = apply_orchestrator.create_hh_apply_trace(vacancy, mode="trace-apply", force=True)
    if trace is None:
        result = {"ok": False, "message": "Не удалось создать каталог trace"}
        print(f"❌ {result['message']}")
        return result

    client = HHClient()
    result: dict = {"ok": False, "message": "Trace apply did not complete"}
    try:
        await client.start()
        authenticated = await client.is_logged_in()
        trace.event(
            "HH_SESSION_CHECK",
            ok=authenticated,
            authenticated=authenticated,
            url=getattr(client._page, "url", ""),
        )
        if not authenticated:
            failure_stage = trace.last_stage
            await trace.capture(client._page, "failure", screenshot=True, html=True)
            result = {"ok": False, "message": "HH-сессия не авторизована"}
            trace.finish(ok=False, message=result["message"], failure_stage=failure_stage)
            return result

        details = await client.get_vacancy_details(vacancy["url"])

        async def page_text(selector: str) -> str:
            try:
                element = await client._page.query_selector(selector)
                return (await element.inner_text()).strip() if element else ""
            except Exception:
                return ""

        vacancy["title"] = await page_text("[data-qa='vacancy-title'], h1")
        vacancy["company"] = await page_text(
            "[data-qa='vacancy-company-name'], [data-qa='vacancy-company-name-text']"
        )
        vacancy["details"] = details
        trace.event(
            "VACANCY_PREFLIGHT",
            ok=bool(details),
            vacancy_id=vacancy_id,
            url=vacancy["url"],
            title=vacancy.get("title", ""),
            company=vacancy.get("company", ""),
            details_chars=len(details or ""),
        )

        cover = await generate_cover_letter(vacancy, details)
        cover_meta = analyze_cover_letter(cover or "")
        cover = (cover or "")[: apply_orchestrator.get_cover_letter_limit("hh")]
        trace.event(
            "COVER_LETTER_GENERATED",
            ok=bool(cover.strip()),
            expected=True,
            generated=bool(cover.strip()),
            chars=len(cover),
            grounding_status=cover_meta["grounding_status"],
            fallback=cover_meta["fallback_cover_letter"],
        )
        if not cover.strip():
            failure_stage = trace.last_stage
            await trace.capture(client._page, "failure", screenshot=True, html=True)
            result = {"ok": False, "message": "LLM не сгенерировал сопроводительное письмо"}
            trace.finish(ok=False, message=result["message"], failure_stage=failure_stage)
            return result

        result = await apply_orchestrator.dispatch_apply(
            vacancy,
            cover,
            hh_client=client,
            preferred_resume_title=getattr(config, "HH_PRIMARY_RESUME_TITLE", ""),
            preferred_resume_id=getattr(config, "HH_PRIMARY_RESUME_ID", ""),
            trace=trace,
        )
        return result
    except Exception as exc:
        failure_stage = trace.last_stage
        if client._page is not None:
            await trace.capture(client._page, "failure", screenshot=True, html=True)
        result = {"ok": False, "message": f"{type(exc).__name__}: {exc}"}
        trace.finish(ok=False, message=result["message"], failure_stage=failure_stage)
        return result
    finally:
        try:
            await client.stop()
        except Exception as exc:
            log.warning("Trace apply HH client shutdown failed: %s", type(exc).__name__)
        print("\nTRACE RESULT")
        print(f"  Trace ID: {trace.trace_id}")
        print(f"  Result:   {'OK' if result.get('ok') else 'FAIL'}")
        if not result.get("ok"):
            print(f"  Failure:  {trace.failure_stage or trace.last_stage}")
        print(f"  Message:  {result.get('message', '')}")
        print(f"  Artifacts: {trace.trace_dir}")


async def main():
    parser = argparse.ArgumentParser(
        description="Job Hunter Agent — автопоиск работы на hh.ru, SuperJob, Хабр Карьере и GeekJob"
    )
    parser.add_argument(
        "--profile", default="default",
        help="Имя профиля (default = из env vars; иначе из ~/.job-hunter/profiles/<name>/profile.env)",
    )
    parser.add_argument(
        "--source",
        choices=("hh", "superjob", "habr", "geekjob"),
        default="",
        help=argparse.SUPPRESS,
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
    group.add_argument("--google-form-recheck", metavar="TOKEN", help="Проверить черновик Google Form после ручных правок")
    group.add_argument("--google-form-recheck-submit", metavar="TOKEN", help="Проверить и отправить подтверждённые ответы Google Form")
    parser.add_argument("--google-form-approval-revision", default=None, help="Версия ответов, подтверждённая Telegram кнопкой")
    group.add_argument("--manual-apply-token", metavar="TOKEN", help="Отправить yellow-zone отклик по Telegram token")
    group.add_argument("--trace-apply", metavar="VACANCY_ID", help="Один реальный HH-отклик с изолированным debug trace")
    parser.add_argument("--confirm-real", action="store_true", help="Подтвердить реальную отправку для --trace-apply")
    parser.add_argument("--chat-message-id", default="", help="ID сообщения в hh-чате для --chat-respond-one")
    parser.add_argument("--chat-allow-suspicious", action="store_true", help="Разрешить ответ на подозрительное HR-сообщение без явного AI-маркера")
    parser.add_argument("--chat-allow-any", action="store_true", help="Для ручного запуска разрешить AI-preview по любому последнему входящему сообщению")
    parser.add_argument("--chat-force-send", action="store_true", help="Для --chat-respond-one отправить ответ сразу, без dry-run preview")
    parser.add_argument("--chat-alternative", action="store_true", help="Для --chat-respond-one подготовить другую формулировку ответа")
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
    _apply_source_selection(args.source)
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
            await chat_commands.respond_all()
        elif args.chat_list_candidates:
            await chat_commands.list_candidates(
                limit=args.chat_list_limit,
                max_scan=args.chat_list_max_scan,
            )
        elif args.google_form_preview:
            await google_form_commands.preview(
                args.google_form_preview,
                message_id=args.chat_message_id,
                profile_name=args.profile,
            )
        elif args.google_form_submit:
            await google_form_commands.submit(args.google_form_submit)
        elif args.google_form_recheck:
            await google_form_commands.recheck(args.google_form_recheck, profile_name=args.profile)
        elif args.google_form_recheck_submit:
            await google_form_commands.recheck(args.google_form_recheck_submit, profile_name=args.profile, submit_after=True,
                                               approval_revision=args.google_form_approval_revision)
        elif args.manual_apply_token:
            result = await do_manual_apply_token(args.manual_apply_token)
            if not result.get("ok"):
                sys.exit(1)
        elif args.trace_apply:
            result = await do_trace_apply(args.trace_apply, confirm_real=args.confirm_real)
            if not result.get("ok"):
                sys.exit(1)
        elif args.chat_respond_one:
            await chat_commands.respond_one(
                args.chat_respond_one,
                message_id=args.chat_message_id,
                allow_suspicious=args.chat_allow_suspicious,
                allow_any=args.chat_allow_any,
                force_send=args.chat_force_send,
                alternative=args.chat_alternative,
            )
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
            from state_store.resume_analysis import AnalysisPublication
            source = Path(resume_path)
            analysis_path = str(source.with_name(source.stem + '_analysis.md')) if source.suffix == '.md' else resume_path + '.analysis.md'
            publication = AnalysisPublication(resume_path, analysis_path)
            result_text = await resume_analyzer.analyze_resume(publication.original_resume.decode('utf-8'))
            print(result_text)
            # Сохраняем анализ рядом с резюме
            if publication.publish(result_text):
                print(f"\n📄 Анализ сохранён: {analysis_path}")
            else:
                print('\n❌ Анализ не завершён; предыдущий результат сохранён.')
        elif args.dry_run:
            result = await do_search(dry_run=True)
            print(f"\n🔍 [DRY RUN] Найдено: {result['found']} | Подходящих: {result['applied']} | Отфильтровано: {result['skipped']}")
            if result.get("note"):
                print(f"ℹ️ {result['note']}")
    finally:
        await close_office_session()
        await close_notify_session()
        await close_llm_client()


if __name__ == "__main__":
    asyncio.run(main())
