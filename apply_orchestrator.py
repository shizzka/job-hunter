"""
Диспетчеризация откликов по источникам.

Извлечено из agent.py (A-002).
"""
import logging

import config
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

async def dispatch_apply(
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

    if source == "hh" and hh_client is not None:
        return await hh_client.apply_to_vacancy(
            vacancy["url"],
            cover_letter,
            response_url=vacancy.get("response_url", ""),
            preferred_resume_title=preferred_resume_title,
            preferred_resume_id=preferred_resume_id,
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
        "hh": True,
        "superjob": config.SUPERJOB_AUTO_APPLY,
        "habr": config.HABR_AUTO_APPLY,
        "geekjob": config.GEEKJOB_AUTO_APPLY,
    }.get(source, False)


def get_cover_letter_limit(source: str) -> int:
    """Лимит символов cover letter по источнику."""
    return 1500 if source == "habr" else 1900
