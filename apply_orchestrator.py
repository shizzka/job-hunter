"""
Диспетчеризация откликов по источникам.

Извлечено из agent.py (A-002).
"""
import logging
import os
import uuid
import analytics
import resume_versions

import config
import company_blacklist
import profile as profile_mod
from debug_trace import ApplyTrace
from hh_client import HHClient
from hh.resume_target import exact_title_resume_id
from hh.ui import HHUnexpectedUI
from outcome import apply_result_is_uncertain
from superjob_client import SuperJobClient
from habr_career_client import HabrCareerClient
from geekjob_client import GeekJobClient

log = logging.getLogger("agent")


def _knowledge_file_names(home_dir: str) -> list[str]:
    knowledge_dir = os.path.join(home_dir, "knowledge")
    try:
        return sorted(
            name
            for name in os.listdir(knowledge_dir)
            if name.endswith((".md", ".txt")) and os.path.isfile(os.path.join(knowledge_dir, name))
        )
    except OSError:
        return []


def create_hh_apply_trace(vacancy: dict, *, mode: str = "auto", force: bool = False) -> ApplyTrace | None:
    if not force and not getattr(config, "HH_APPLY_TRACE_ENABLED", True):
        return None
    try:
        active_profile = profile_mod.active()
        profile_name = str(active_profile.name or "default")
        home_dir = str(config.JOB_HUNTER_HOME or active_profile.home_dir)
        trace = ApplyTrace.create(
            home_dir=home_dir,
            source="hh",
            vacancy_id=str(vacancy.get("id") or "unknown"),
            profile=profile_name,
            mode=mode,
            retention_days=getattr(config, "HH_APPLY_TRACE_RETENTION_DAYS", 14),
            max_runs=getattr(config, "HH_APPLY_TRACE_MAX_RUNS", 100),
        )
        trace.event(
            "PROFILE_CONTEXT",
            ok=True,
            profile=profile_name,
            home_dir=home_dir,
            hh_state_dir=config.HH_STATE_DIR,
            cookies_file=config.HH_COOKIES_FILE,
            resume_file=config.RESUME_FILE,
            resume_id=getattr(config, "HH_PRIMARY_RESUME_ID", ""),
            resume_title=getattr(config, "HH_PRIMARY_RESUME_TITLE", ""),
            kb_files=_knowledge_file_names(home_dir),
        )
        return trace
    except Exception as exc:
        log.warning("Could not initialize HH apply trace: %s", type(exc).__name__)
        return None


# ── Получение деталей вакансии ──

async def fetch_vacancy_details(
    vacancy: dict,
    hh_client: HHClient | None = None,
    superjob_client: SuperJobClient | None = None,
    habr_client: HabrCareerClient | None = None,
    geekjob_client: GeekJobClient | None = None,
) -> str:
    """Получить детали вакансии из соответствующего источника."""
    source = vacancy.get("source", "hh")
    details = vacancy.get("details", "")
    url = vacancy.get("url", "")

    if not url:
        return details

    try:
        if source == "hh" and hh_client is not None:
            details = await hh_client.get_vacancy_details(url)
        elif source == "habr" and habr_client is not None:
            details = await habr_client.get_vacancy_details(url)
        elif source == "geekjob" and geekjob_client is not None:
            details = await geekjob_client.get_vacancy_details(url)
    except HHUnexpectedUI:
        raise
    except Exception as e:
        analytics.record_failure("details", e, source=source, continued=True)
        log.warning("Failed to get %s details for %s: %s", source, vacancy.get("id"), type(e).__name__)

    return details


# ── Диспетчеризация отклика ──

async def dispatch_apply(vacancy: dict, cover_letter: str, *args, **kwargs) -> dict:
    # Do not navigate to the resume catalog for a blocked company; still recheck
    # in _dispatch_apply after any preflight awaits.
    if company_blacklist.is_blocked(vacancy.get("company", "")):
        return {"ok": False, "message": "Компания в чёрном списке", "reason": "company_blacklisted"}
    if vacancy.get("source", "hh") == "hh":
        if not kwargs.get("preferred_resume_id") and not kwargs.get("preferred_resume_title"):
            kwargs["preferred_resume_id"] = getattr(config, "HH_PRIMARY_RESUME_ID", "")
            kwargs["preferred_resume_title"] = getattr(config, "HH_PRIMARY_RESUME_TITLE", "")
        hh_client = kwargs.get("hh_client") or (args[0] if args else None)
        if hh_client is not None and not str(kwargs.get("preferred_resume_id") or "").strip():
            target_title = str(kwargs.get("preferred_resume_title") or "").strip()
            target_id = ""
            if target_title:
                try:
                    target_id = exact_title_resume_id(await hh_client.get_resume_ids(), target_title)
                except HHUnexpectedUI:
                    raise
                except Exception as exc:
                    log.warning("HH resume target resolution failed: %s", type(exc).__name__)
            if not target_id:
                result = {
                    "ok": False, "reason": "hh_resume_target_unresolved",
                    "resume_selection_verified": False,
                    "message": "Целевое HH-резюме не определено однозначно — отклик не отправлен. Нужна ручная проверка resume ID.",
                }
                trace = kwargs.get("trace")
                if trace is not None:
                    trace.event("RESUME_TARGET", ok=False, reason=result["reason"])
                    trace.finish(ok=False, message=result["message"], failure_stage="RESUME_TARGET")
                return result
            kwargs["preferred_resume_id"] = target_id
        trace = kwargs.get("trace")
        if trace is None:
            trace = create_hh_apply_trace(
                vacancy,
                mode=str(vacancy.get("_analytics_apply_mode") or "auto"),
            )
            kwargs["trace"] = trace
        if trace is not None:
            trace.event(
                "DISPATCH_APPLY",
                ok=True,
                has_cover_letter=bool((cover_letter or "").strip()),
                cover_letter_chars=len(cover_letter or ""),
                requested_resume_id=kwargs.get("preferred_resume_id", ""),
                requested_resume_title=kwargs.get("preferred_resume_title", ""),
            )
    application_id = uuid.uuid4().hex
    vacancy["_requested_resume"] = {"id": kwargs.get("preferred_resume_id", ""),
                                    "title": kwargs.get("preferred_resume_title", "")}
    with analytics.event_context(
        application_id=application_id,
        run_id=vacancy.get("_analytics_run_id", ""),
        vacancy_id=str(vacancy.get("id") or ""),
        source=vacancy.get("source", "hh"),
        stage="apply",
        apply_mode=vacancy.get("_analytics_apply_mode", "unknown"),
        has_cover_letter=bool(cover_letter.strip()),
        resume_id=kwargs.get("preferred_resume_id", ""),
        **resume_versions.payload(vacancy),
    ):
        analytics.record_event({"event": "application_attempt"})
        try:
            result = await _dispatch_apply(vacancy, cover_letter, *args, **kwargs)
        except Exception as exc:
            analytics.record_event({"event": "application_result", "outcome": "error", "error_kind": type(exc).__name__})
            trace = kwargs.get("trace")
            if trace is not None:
                try:
                    failure_stage = trace.last_stage
                    hh_client = kwargs.get("hh_client") or (args[0] if args else None)
                    if not isinstance(exc, HHUnexpectedUI) and getattr(hh_client, "_page", None) is not None:
                        await trace.capture(hh_client._page, "failure", screenshot=True, html=True)
                    trace.finish(
                        ok=False,
                        message=f"{type(exc).__name__}: {exc}",
                        failure_stage=failure_stage,
                    )
                except Exception as trace_error:
                    log.warning("HH apply trace unavailable: error_kind=%s", type(trace_error).__name__)
            raise
        if apply_result_is_uncertain(result):
            result = {**result, "ok": False, "uncertain": True}
        outcome = ("uncertain" if result.get("uncertain") else
                   "already_applied" if result.get("already_applied") else
                   "blocked" if result.get("reason") == "company_blacklisted" else
                   "sent" if result.get("ok") else "failed")
        analytics.record_event({"event": "application_result", "outcome": outcome,
                                 "cover_letter_status": result.get("cover_letter_status", "unknown"),
                                 "resume_selection_verified": result.get("resume_selection_verified", False),
                                 "selected_resume_id": result.get("selected_resume_id", "")})
        trace = kwargs.get("trace")
        if trace is not None:
            try:
                result_ok = bool(result.get("ok"))
                failure_stage = trace.last_stage
                if not result_ok:
                    hh_client = kwargs.get("hh_client") or (args[0] if args else None)
                    if getattr(hh_client, "_page", None) is not None:
                        await trace.capture(hh_client._page, "failure", screenshot=True, html=True)
                trace.finish(
                    ok=result_ok,
                    message=str(result.get("message") or outcome),
                    failure_stage=failure_stage,
                )
                result = {**result, "trace_id": trace.trace_id, "trace_dir": os.fspath(trace.trace_dir)}
            except Exception as trace_error:
                # Diagnostics cannot erase a receipt or turn uncertainty into a retry.
                log.warning("HH apply trace completion unavailable: error_kind=%s", type(trace_error).__name__)
        return result


async def _dispatch_apply(
    vacancy: dict,
    cover_letter: str,
    hh_client: HHClient | None = None,
    superjob_client: SuperJobClient | None = None,
    habr_client: HabrCareerClient | None = None,
    geekjob_client: GeekJobClient | None = None,
    preferred_resume_title: str = "",
    preferred_resume_id: str = "",
    trace: ApplyTrace | None = None,
) -> dict:
    """Отправить отклик через соответствующий клиент источника."""
    source = vacancy.get("source", "hh")
    if company_blacklist.is_blocked(vacancy.get("company", "")):
        return {"ok": False, "message": "Компания в чёрном списке", "reason": "company_blacklisted"}

    if source == "hh" and hh_client is not None:
        title = (vacancy.get("title") or "").strip()
        company = (vacancy.get("company") or "").strip()
        details = (vacancy.get("details") or "").strip()
        context_parts = []
        if title:
            context_parts.append(f"Должность: {title}")
        if company:
            context_parts.append(f"Компания: {company}")
        if details:
            context_parts.append(f"Описание: {details}")
        vacancy_context = "\n".join(context_parts)
        return await hh_client.apply_to_vacancy(
            vacancy["url"],
            cover_letter,
            response_url=vacancy.get("response_url", ""),
            preferred_resume_title=preferred_resume_title,
            preferred_resume_id=preferred_resume_id,
            vacancy_context=vacancy_context,
            trace=trace,
        )
    elif source == "superjob" and superjob_client is not None:
        return await superjob_client.apply_to_vacancy(vacancy, cover_letter)
    elif source == "habr" and habr_client is not None:
        return await habr_client.apply_to_vacancy(vacancy["url"], cover_letter)
    elif source == "geekjob" and geekjob_client is not None:
        return await geekjob_client.apply_to_vacancy(vacancy, cover_letter)
    else:
        return {"ok": False, "message": f"Unknown source: {source}"}


# ── Проверка готовности источника ──

def is_auto_apply_enabled(source: str) -> bool:
    """Проверить, включён ли автоотклик для данного источника."""
    return {
        "hh": getattr(config, "HH_APPLICATION_MODE", "auto") == "auto",
        "superjob": config.SUPERJOB_AUTO_APPLY,
        "habr": config.HABR_AUTO_APPLY,
        "geekjob": config.GEEKJOB_AUTO_APPLY,
    }.get(source, False)


def get_cover_letter_limit(source: str) -> int:
    """Лимит символов cover letter по источнику."""
    return 1500 if source == "habr" else 1900
