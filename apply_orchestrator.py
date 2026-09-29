"""
Диспетчеризация откликов по источникам.

Извлечено из agent.py (A-002).
"""
import logging
import uuid
import analytics
import resume_versions

import config
import company_blacklist
from hh_client import HHClient
from superjob_client import SuperJobClient
from habr_career_client import HabrCareerClient
from geekjob_client import GeekJobClient

log = logging.getLogger("agent")


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
    except Exception as e:
        log.warning("Failed to get %s details for %s: %s", source, vacancy.get("id"), e)

    return details


# ── Диспетчеризация отклика ──

async def dispatch_apply(vacancy: dict, cover_letter: str, *args, **kwargs) -> dict:
    if vacancy.get("source", "hh") == "hh":
        if not kwargs.get("preferred_resume_id") and not kwargs.get("preferred_resume_title"):
            kwargs["preferred_resume_id"] = getattr(config, "HH_PRIMARY_RESUME_ID", "")
            kwargs["preferred_resume_title"] = getattr(config, "HH_PRIMARY_RESUME_TITLE", "")
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
        analytics._append_event({"event": "application_attempt"})
        try:
            result = await _dispatch_apply(vacancy, cover_letter, *args, **kwargs)
        except Exception as exc:
            analytics._append_event({"event": "application_result", "outcome": "error", "error_kind": type(exc).__name__})
            raise
        outcome = ("already_applied" if result.get("already_applied") else
                   "blocked" if result.get("reason") == "company_blacklisted" else
                   "sent" if result.get("ok") else "failed")
        analytics._append_event({"event": "application_result", "outcome": outcome,
                                 "cover_letter_status": result.get("cover_letter_status", "unknown"),
                                 "resume_selection_verified": result.get("resume_selection_verified", False),
                                 "selected_resume_id": result.get("selected_resume_id", "")})
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
            context_parts.append(f"Описание: {details[:1800]}")
        vacancy_context = "\n".join(context_parts)
        return await hh_client.apply_to_vacancy(
            vacancy["url"],
            cover_letter,
            response_url=vacancy.get("response_url", ""),
            preferred_resume_title=preferred_resume_title,
            preferred_resume_id=preferred_resume_id,
            vacancy_context=vacancy_context,
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
