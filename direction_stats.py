"""Observed direction/variant funnels from HH negotiation snapshots only.

Attempt logs are deliberately excluded: a local attempt does not establish an HH
application. Numeric resumeId mappings must be unique across all directions.
"""
from __future__ import annotations

import csv
import io
import json
from datetime import datetime
from pathlib import Path

from directions import list_directions

COUNTS = ("applied", "viewed", "rejected", "next_stage", "no_response")
CSV_FIELDS = ("direction", "direction_title", "hh_resume_hash", "hh_resume_numeric_id", "variant_title", "topics", *COUNTS)


def _number(value):
    if type(value) is int and value > 0:
        return value
    if isinstance(value, str) and value.isascii() and value.isdigit() and int(value) > 0:
        return int(value)
    return None


def _empty():
    return {"topics": 0, **dict.fromkeys(COUNTS, 0)}


def _codes(record):
    return {record[field].upper() for field in ("lastEmployerState", "lastState", "lastSubState", "employerSubState", "lastFunnelStage")
            if isinstance(record.get(field), str)}


def _counts(record):
    codes = _codes(record)
    origin = str(record.get("initialTopicType", "")).upper()
    applied = origin in {"RESPONSE", "RESPONSE_BY_APPLICANT"} or (
        not origin and str(record.get("lastState", "")).upper() == "RESPONSE")
    declined_by_applicant = record.get("declinedByApplicant") is True
    rejected = not declined_by_applicant and bool(codes & {
        "DISCARD", "REJECTED", "REJECTION", "REJECT", "DECLINED", "DENIED"})
    next_stage = bool(codes & {"INVITATION", "INVITATION_BY_EMPLOYER", "INTERVIEW", "PHONE_INTERVIEW", "TECHNICAL_INTERVIEW", "ASSESSMENT", "TEST", "OFFER", "HIRED", "ACCEPTED"})
    return {"topics": 1, "applied": int(applied),
            "viewed": int(record.get("viewedByOpponent") is True),
            "rejected": int(rejected), "next_stage": int(next_stage),
            "no_response": int(applied and not declined_by_applicant and not rejected and not next_stage)}


def _time(record, index):
    raw = record.get("fetched_at")
    try:
        date = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        timestamp = date.timestamp() if date.tzinfo is not None else float("-inf")
    except (TypeError, ValueError, AttributeError, OverflowError):
        timestamp = float("-inf")
    return timestamp, index


def _records(home_dir, snapshots):
    if snapshots is None:
        path = Path(home_dir) / "negotiation_snapshots.jsonl"
        if not path.exists():
            return []
        snapshots = path
    if isinstance(snapshots, (str, Path)):
        records = []
        with Path(snapshots).open(encoding="utf-8") as stream:
            for line in stream:
                if not line.strip():
                    continue
                try:
                    records.append(json.loads(line))
                except json.JSONDecodeError:
                    # A truncated journal tail cannot provide observation evidence.
                    continue
        return records
    return list(snapshots)


def summarize(home_dir, snapshots=None):
    """Return per-variant rows plus explicit unmatched and ambiguous buckets.

    Counts are overlapping observations: viewed applications may still have no
    response. ``next_stage`` requires an explicit invitation/interview/offer state;
    initial SCREENING alone is not treated as employer progression.
    """
    rows, mapping = [], {}
    for direction in list_directions(home_dir):
        for variant in direction["variants"]:
            row = {"direction": direction["name"], "direction_title": direction["title"],
                   "hh_resume_hash": variant["hh_resume_hash"],
                   "hh_resume_numeric_id": variant["hh_resume_numeric_id"],
                   "variant_title": variant["title"], **_empty()}
            rows.append(row)
            identity = variant["hh_resume_numeric_id"]
            if identity is not None:
                mapping.setdefault(identity, []).append(row)
    latest = {}
    invalid_topics = 0
    for index, record in enumerate(_records(home_dir, snapshots)):
        if not isinstance(record, dict) or record.get("kind") is not None:
            continue
        identity = _number(record.get("id"))
        if identity is None:
            invalid_topics += 1
            continue
        previous = latest.get(identity)
        if previous is None or _time(record, index) >= previous[0]:
            latest[identity] = _time(record, index), record
    unmatched, ambiguous = _empty(), _empty()
    unmatched_ids, ambiguous_ids = set(), set()
    for _, record in latest.values():
        identity = _number(record.get("resumeId"))
        destinations = mapping.get(identity, [])
        if len(destinations) == 1:
            bucket = destinations[0]
        elif len(destinations) > 1:
            bucket = ambiguous
            ambiguous_ids.add(identity)
        else:
            bucket = unmatched
            if identity is not None:
                unmatched_ids.add(identity)
        for key, value in _counts(record).items():
            bucket[key] += value
    return {"rows": rows, "unmatched": unmatched, "ambiguous": ambiguous,
            "unmatched_resume_ids": sorted(unmatched_ids), "ambiguous_resume_ids": sorted(ambiguous_ids),
            "total_topics": len(latest), "invalid_topics": invalid_topics,
            "source": "negotiation_snapshots.jsonl"}


def export_csv(summary):
    """CSV text includes unmatched/ambiguous observations as dedicated rows."""
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(summary["rows"])
    for name in ("unmatched", "ambiguous"):
        writer.writerow({"direction": name, **summary[name]})
    return stream.getvalue()
