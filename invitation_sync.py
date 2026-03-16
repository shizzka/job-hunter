"""
Проверка приглашений и синхронизация статусов переговоров (A-003).

Извлечено из agent.py.
"""
import logging

import analytics
import hh_resume_pipeline as hh_pipeline
from hh_client import HHClient

log = logging.getLogger("agent")


async def check_invitations(client: HHClient) -> dict:
    """
    Проверить приглашения и синхронизировать статусы.

    Возвращает:
    {
        "invitations": [...],
        "negotiation_statuses_synced": int,
        "error": str | None,
    }
    """
    result = {
        "invitations": [],
        "negotiation_statuses_synced": 0,
        "error": None,
    }

    # Синхронизация статусов переговоров
    try:
        negotiation_statuses = await client.get_negotiation_statuses()
        analytics.record_negotiation_statuses(negotiation_statuses)
        result["negotiation_statuses_synced"] = len(negotiation_statuses)
        if hh_pipeline.enabled():
            hh_pipeline.sync_negotiation_statuses(negotiation_statuses)
    except Exception as e:
        log.warning("Failed to sync negotiation statuses: %s", e)

    # Проверка приглашений
    try:
        negotiations = await client.check_negotiations()
        invitations = negotiations.get("invitations", [])
        analytics.record_invitations(invitations)
        result["invitations"] = invitations
    except Exception as e:
        log.warning("Failed to check negotiations: %s", e)
        result["error"] = str(e)

    return result
