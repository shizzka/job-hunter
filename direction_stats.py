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


def resume_mismatches(home_dir, snapshots=None, events_path=None):
    """Applications whose hh topic carries another resume than the one requested.

    Requested = ``requested_resume_id`` (hh hash) of an applied decision event,
    mapped to a numeric id through saved direction variants. Actual = latest
    snapshot ``resumeId`` for the same vacancy. Unmapped hashes are skipped.
    """
    numeric = {}
    titles = {}
    for direction in list_directions(home_dir):
        for variant in direction["variants"]:
            if variant["hh_resume_numeric_id"] is not None:
                numeric[variant["hh_resume_hash"]] = variant["hh_resume_numeric_id"]
                titles[variant["hh_resume_numeric_id"]] = variant["title"]
    if not numeric:
        return []
    path = Path(events_path) if events_path else Path(home_dir) / "analytics_events.jsonl"
    requested = {}
    import analytics_read_cache
    events, _ = analytics_read_cache.journal(path).read(path)
    for event in events:
        if not isinstance(event, dict) or event.get("event") != "decision" or event.get("outcome") != "applied":
            continue
        resume_hash = str(event.get("requested_resume_id") or "")
        vacancy = str(event.get("vacancy_id") or "")
        if resume_hash in numeric and vacancy:
            requested[(vacancy, resume_hash)] = (numeric[resume_hash], str(event.get("title") or ""), str(event.get("company") or ""))
    latest_topics = {}
    for index, record in enumerate(_records(home_dir, snapshots)):
        if not isinstance(record, dict) or record.get("kind") is not None:
            continue
        vacancy, resume = str(record.get("vacancyId") or ""), _number(record.get("resumeId"))
        if vacancy and resume is not None:
            key = record.get("id", (vacancy, resume))
            previous = latest_topics.get(key)
            if previous is None or _time(record, index) >= previous[0]:
                latest_topics[key] = (_time(record, index), vacancy, resume)
    actual = {}
    for _, vacancy, resume in latest_topics.values():
        actual.setdefault(vacancy, set()).add(resume)
    result = []
    for (vacancy, resume_hash), (wanted, title, company) in sorted(requested.items()):
        resumes = actual.get(vacancy, set())
        observed = next(iter(resumes)) if len(resumes) == 1 else None
        if observed is not None and observed != wanted:
            result.append({"vacancy_id": vacancy, "title": title, "company": company,
                           "requested_resume_hash": resume_hash,
                           "requested_resume_numeric_id": wanted, "actual_resume_numeric_id": observed,
                           "requested_title": titles.get(wanted, ""), "actual_title": titles.get(observed, "")})
    return result


def export_csv(summary):
    """CSV text includes unmatched/ambiguous observations as dedicated rows."""
    stream = io.StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(summary["rows"])
    for name in ("unmatched", "ambiguous"):
        writer.writerow({"direction": name, **summary[name]})
    return stream.getvalue()


def scoped(summary, home, scope="active"):
    """Keep the numeric-resume report intact; apply a display scope afterwards."""
    from direction_history import ALL, UNSPECIFIED, HistoryScope
    view = HistoryScope(home, scope)
    if scope == ALL:
        return {**summary, "direction_title": view.title}
    rows = view.records(summary["rows"])
    unknown = scope == UNSPECIFIED or not view.directions
    unmatched = summary["unmatched"] if unknown else _empty()
    ambiguous = summary["ambiguous"] if unknown else _empty()
    return {**summary, "rows": rows, "unmatched": unmatched, "ambiguous": ambiguous,
            "total_topics": sum(row["topics"] for row in rows) + unmatched["topics"] + ambiguous["topics"],
            "direction_title": view.title}


# Cache only storage-backed calls; caller-supplied fixtures remain independent.
def _cached_report(fn):
    from functools import wraps
    @wraps(fn)
    def wrapped(home_dir, snapshots=None, **kwargs):
        if snapshots is not None or kwargs:
            return fn(home_dir, snapshots=snapshots, **kwargs)
        import analytics_read_cache as cache
        from direction_history import HistoryScope
        view = HistoryScope(home_dir)
        key = (fn.__name__, cache.ownership_key(view), cache.stamp(Path(home_dir) / "analytics_events.jsonl"))
        return cache.memo(key, lambda: (fn(home_dir), None))
    return wrapped


summarize = _cached_report(summarize)
resume_mismatches = _cached_report(resume_mismatches)
