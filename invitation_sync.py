"""
Проверка приглашений и синхронизация статусов переговоров (A-003).

Извлечено из agent.py.
"""
import logging
import re

import analytics
import hh_resume_pipeline as hh_pipeline
import profile_access
from hh_client import HHClient

log = logging.getLogger("agent")


def authorized_observations(items):
    """Bind account observations using resume/application evidence before use.

    A vacancy URL is an identity, not proof of a direction. Historical applied
    events may resolve it; ambiguous or missing evidence stays unowned.
    """
    if not items:
        return []
    records = []
    for item in items:
        record = dict(item)
        if not record.get("id"):
            match = re.search(r"/vacancy/(\d+)(?:[/?#]|$)", str(record.get("url") or ""))
            if match:
                record["id"] = match[1]
        records.append(record)
    scoped_direction = profile_access.current_scope()[1]
    return [{**record, **metadata}
            for record, metadata in zip(records, analytics.observation_metadata(records))
            if not scoped_direction or metadata.get("direction") == scoped_direction]


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
        negotiation_statuses = authorized_observations(await client.get_negotiation_statuses())
        analytics.record_negotiation_statuses(negotiation_statuses)
        result["negotiation_statuses_synced"] = len(negotiation_statuses)
        if hh_pipeline.enabled():
            hh_pipeline.sync_negotiation_statuses(negotiation_statuses)
    except Exception as e:
        log.warning("Failed to sync negotiation statuses: %s", e)

    # Проверка приглашений
    try:
        negotiations = await client.check_negotiations()
        invitations = authorized_observations(negotiations.get("invitations", []))
        analytics.record_invitations(invitations)
        result["invitations"] = invitations
    except Exception as e:
        log.warning("Failed to check negotiations: %s", e)
        result["error"] = str(e)

    return result
