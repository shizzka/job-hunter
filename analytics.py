"""Persistent analytics/event history for matcher tuning and funnel analysis."""
from __future__ import annotations

import json
import logging
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import config

log = logging.getLogger("analytics")

_state: dict | None = None


def _now() -> datetime:
    return datetime.now()


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _normalize(value: str) -> str:
    return " ".join((value or "").casefold().split())


def _vacancy_key(payload: dict) -> str:
    vacancy_id = str(payload.get("vacancy_id") or payload.get("id") or "").strip()
    source = str(payload.get("source") or "").strip()
    if vacancy_id:
        return f"{source}:{vacancy_id}"

    title = _normalize(payload.get("title", ""))
    company = _normalize(payload.get("company", ""))
    url = (payload.get("url") or "").split("?", 1)[0].strip().casefold()
    return f"{source}:{title}|{company}|{url}"


def _source_from_vacancy_id(vacancy_id: str) -> str:
    vacancy_id = str(vacancy_id or "").strip()
    if ":" in vacancy_id:
        return vacancy_id.split(":", 1)[0]
    if vacancy_id.isdigit():
        return "hh"
    return "unknown"


def _load_state() -> dict:
    global _state
    if _state is not None:
        return _state

    if os.path.exists(config.ANALYTICS_STATE_FILE):
        try:
            with open(config.ANALYTICS_STATE_FILE, encoding="utf-8") as f:
                _state = json.load(f)
        except Exception:
            _state = {}
    else:
        _state = {}

    _state.setdefault("negotiation_status_by_vacancy", {})
    _state.setdefault("invitation_keys", [])
    _state.setdefault("historical_decision_keys", [])
    return _state


def _save_state() -> None:
    if _state is None:
        return
    os.makedirs(os.path.dirname(config.ANALYTICS_STATE_FILE), exist_ok=True)
    with open(config.ANALYTICS_STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(_state, f, ensure_ascii=False, indent=2)


def _append_event(payload: dict) -> None:
    if not config.ANALYTICS_ENABLED:
        return

    try:
        os.makedirs(os.path.dirname(config.ANALYTICS_EVENTS_FILE), exist_ok=True)
        with open(config.ANALYTICS_EVENTS_FILE, "a", encoding="utf-8") as f:
            f.write(json.dumps(payload, ensure_ascii=False) + "\n")
    except Exception as exc:
        log.warning("Failed to append analytics event: %s", exc)


def new_run_id(mode: str) -> str:
    stamp = _now().strftime("%Y%m%dT%H%M%S")
    return f"{mode}-{stamp}-{os.getpid()}"


def _trim_details(details: str) -> str:
    text = (details or "").strip()
    if not text:
        return ""
    limit = max(0, config.ANALYTICS_MAX_DETAILS_CHARS)
    if limit and len(text) > limit:
        return text[:limit]
    return text


def _resume_variant_payload(resume_variant: dict | None) -> dict:
    if not resume_variant:
        return {
            "resume_variant": "",
            "resume_title": "",
            "resume_id": "",
        }
    return {
        "resume_variant": resume_variant.get("name", ""),
        "resume_title": resume_variant.get("title", ""),
        "resume_id": resume_variant.get("id", ""),
    }


def record_search_started(
    *,
    run_id: str,
    mode: str,
    enabled_sources: list[str],
) -> None:
    """EVT-001: начало поискового прогона."""
    if not config.ANALYTICS_ENABLED:
        return
    _append_event({
        "event": "search_started",
        "created_at": _now().isoformat(timespec="seconds"),
        "run_id": run_id,
        "mode": mode,
        "enabled_sources": enabled_sources,
    })


def record_search_finished(
    *,
    run_id: str,
    mode: str,
    result: dict,
) -> None:
    """EVT-002: завершение поискового прогона."""
    if not config.ANALYTICS_ENABLED:
        return
    _append_event({
        "event": "search_finished",
        "created_at": _now().isoformat(timespec="seconds"),
        "run_id": run_id,
        "mode": mode,
        "found": result.get("found", 0),
        "applied": result.get("applied", 0),
        "manual": result.get("manual", 0),
        "source_stats": result.get("source_stats", {}),
        "ok": result.get("ok", True),
    })


def record_decision(
    *,
    run_id: str,
    vacancy: dict,
    decision: str,
    evaluation: dict | None = None,
    details: str = "",
    dry_run: bool = False,
    resume_variant: dict | None = None,
    note: str = "",
) -> None:
    if not config.ANALYTICS_ENABLED:
        return

    evaluation = evaluation or {}
    payload = {
        "event": "decision",
        "created_at": _now().isoformat(timespec="seconds"),
        "run_id": run_id,
        "mode": "dry-run" if dry_run else "search",
        "dry_run": dry_run,
        "decision": decision,
        "vacancy_id": str(vacancy.get("id") or "").strip(),
        "source": vacancy.get("source", "") or "unknown",
        "source_label": vacancy.get("source_label", "") or vacancy.get("source", "") or "unknown",
        "title": vacancy.get("title", ""),
        "company": vacancy.get("company", ""),
        "url": vacancy.get("url", ""),
        "response_url": vacancy.get("response_url", ""),
        "location": vacancy.get("location", ""),
        "salary": vacancy.get("salary", ""),
        "snippet": vacancy.get("snippet", ""),
        "details": _trim_details(details),
        "search_query": vacancy.get("_search_query", ""),
        "search_profile": vacancy.get("_search_profile", ""),
        "search_path": vacancy.get("_search_path", ""),
        "is_retry": bool(vacancy.get("_hh_retry")),
        "last_known_status": vacancy.get("_hh_last_status", ""),
        "apply_mode": vacancy.get("apply_mode", ""),
        "score": evaluation.get("score"),
        "should_apply": bool(evaluation.get("should_apply", False)),
        "reason": evaluation.get("reason", ""),
        "red_flags": list(evaluation.get("red_flags", []) or []),
        "note": note,
    }
    payload.update(_resume_variant_payload(resume_variant))
    _append_event(payload)


def _status_bucket(status_text: str) -> str:
    text = (status_text or "").strip().casefold()
    if any(token in text for token in ("приглаш", "собесед", "оффер", "выход на работу")):
        return "positive"
    if "отказ" in text:
        return "rejected"
    if any(token in text for token in ("не просмотрен", "просмотрен", "ожидание")):
        return "pending"
    return "unknown"


def record_negotiation_statuses(items: list[dict]) -> None:
    if not config.ANALYTICS_ENABLED or not items:
        return

    state = _load_state()
    last_status_by_vacancy = state.setdefault("negotiation_status_by_vacancy", {})
    changed = False

    for item in items:
        vacancy_id = str(item.get("id") or "").strip()
        status_text = str(item.get("status") or "").strip()
        if not vacancy_id or not status_text:
            continue

        prev_status = last_status_by_vacancy.get(vacancy_id, "")
        if prev_status == status_text:
            continue

        payload = {
            "event": "negotiation_status",
            "created_at": _now().isoformat(timespec="seconds"),
            "vacancy_id": vacancy_id,
            "source": "hh",
            "source_label": "hh.ru",
            "title": item.get("title", ""),
            "company": item.get("company", ""),
            "url": item.get("url", ""),
            "status": status_text,
            "status_bucket": _status_bucket(status_text),
            "prev_status": prev_status,
        }
        _append_event(payload)
        last_status_by_vacancy[vacancy_id] = status_text
        changed = True

    if changed:
        _save_state()


def record_invitations(items: list[dict]) -> None:
    if not config.ANALYTICS_ENABLED or not items:
        return

    state = _load_state()
    invitation_keys = set(state.setdefault("invitation_keys", []))
    changed = False

    for item in items:
        payload = {
            "vacancy_id": str(item.get("id") or "").strip(),
            "title": item.get("title", ""),
            "company": item.get("company", ""),
            "url": item.get("url", ""),
            "source": "hh",
        }
        key = _vacancy_key(payload)
        if key in invitation_keys:
            continue

        _append_event(
            {
                "event": "invitation",
                "created_at": _now().isoformat(timespec="seconds"),
                **payload,
            }
        )
        invitation_keys.add(key)
        changed = True

    if changed:
        state["invitation_keys"] = sorted(invitation_keys)
        _save_state()


def _map_historical_action(action: str) -> tuple[str, str]:
    if action == "applied":
        return "applied_auto", "historical_seen"
    if action == "skipped_questions":
        return "questions_required", "historical_seen"
    if action.startswith("apply_failed_exception:"):
        return "apply_failed_exception", action.split(":", 1)[1].strip()
    if action.startswith("apply_failed:"):
        return "apply_failed", action.split(":", 1)[1].strip()
    if action.startswith("manual_"):
        return action, "historical_seen"
    if action.startswith("skipped_"):
        return action, "historical_seen"
    return f"historical_{action or 'unknown'}", "historical_seen"


def backfill_seen_decisions(entries: dict, run_id: str = "") -> dict:
    """Backfill older seen-state into analytics once, without duplicates."""
    if not config.ANALYTICS_ENABLED or not entries:
        return {"added": 0, "by_decision": {}}

    state = _load_state()
    historical_keys = set(state.setdefault("historical_decision_keys", []))
    decision_counter = Counter()
    added = 0
    changed = False
    backfill_run_id = run_id or new_run_id("analytics-backfill")

    for vacancy_id, payload in entries.items():
        if not isinstance(payload, dict):
            continue

        source = _source_from_vacancy_id(vacancy_id)
        decision, note = _map_historical_action(str(payload.get("action") or "").strip())
        historical_key = f"{source}:{vacancy_id}:{decision}"
        if historical_key in historical_keys:
            continue

        created_at = _parse_dt(payload.get("date")) or _now()
        _append_event(
            {
                "event": "decision",
                "created_at": created_at.isoformat(timespec="seconds"),
                "run_id": backfill_run_id,
                "mode": "historical",
                "dry_run": False,
                "historical": True,
                "decision": decision,
                "vacancy_id": str(vacancy_id or "").strip(),
                "source": source,
                "source_label": source,
                "title": payload.get("title", ""),
                "company": payload.get("company", ""),
                "url": payload.get("url", ""),
                "response_url": "",
                "location": "",
                "salary": "",
                "snippet": "",
                "details": "",
                "search_query": "",
                "search_profile": "historical_seen",
                "search_path": "",
                "is_retry": False,
                "last_known_status": "",
                "apply_mode": "",
                "score": None,
                "should_apply": decision == "applied_auto",
                "reason": "",
                "red_flags": [],
                "note": note,
                "resume_variant": "",
                "resume_title": "",
                "resume_id": "",
            }
        )
        historical_keys.add(historical_key)
        decision_counter[decision] += 1
        added += 1
        changed = True

    if changed:
        state["historical_decision_keys"] = sorted(historical_keys)
        _save_state()

    return {
        "added": added,
        "by_decision": dict(sorted(decision_counter.items(), key=lambda item: (-item[1], item[0]))),
    }


def _iter_events() -> list[dict]:
    if not os.path.exists(config.ANALYTICS_EVENTS_FILE):
        return []

    events = []
    try:
        with open(config.ANALYTICS_EVENTS_FILE, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception as exc:
        log.warning("Failed to read analytics events: %s", exc)
        return []
    return events


def summarize(days: int | None = None) -> dict:
    days = days if days is not None else config.ANALYTICS_RECENT_DAYS
    cutoff = _now() - timedelta(days=max(0, days))
    events = []
    for event in _iter_events():
        created_at = _parse_dt(event.get("created_at"))
        if created_at is None or created_at < cutoff:
            continue
        events.append(event)

    summary = {
        "days": days,
        "events": len(events),
        "search_runs": 0,
        "decisions": 0,
        "auto_applied": 0,
        "dry_run_matched": 0,
        "manual": 0,
        "keyword_filtered": 0,
        "red_flagged": 0,
        "low_score": 0,
        "invitations": 0,
        "positive_statuses": 0,
        "rejected_statuses": 0,
        "pending_statuses": 0,
        "by_source": {},
        "by_query": {},
        "by_resume_variant": {},
        "top_decisions": [],
    }

    decision_counter = Counter()
    latest_apply_by_vacancy = {}
    latest_status_by_vacancy = {}

    for event in events:
        event_type = event.get("event")
        if event_type == "search_finished":
            summary["search_runs"] += 1
        elif event_type == "decision":
            summary["decisions"] += 1
            decision = event.get("decision", "")
            decision_counter[decision or "unknown"] += 1
            source = event.get("source", "unknown")
            query = event.get("search_query", "")
            resume_variant = event.get("resume_variant", "")

            source_bucket = summary["by_source"].setdefault(
                source,
                {
                    "decisions": 0,
                    "auto_applied": 0,
                    "manual": 0,
                    "positive": 0,
                    "rejected": 0,
                },
            )
            source_bucket["decisions"] += 1

            if decision == "skipped_keyword_filter":
                summary["keyword_filtered"] += 1
            elif decision == "skipped_red_flags":
                summary["red_flagged"] += 1
            elif decision == "skipped_low_score":
                summary["low_score"] += 1
            elif decision == "dry_run_match":
                summary["dry_run_matched"] += 1

            if decision == "applied_auto":
                summary["auto_applied"] += 1
                source_bucket["auto_applied"] += 1
                latest_apply_by_vacancy[_vacancy_key(event)] = event
            elif decision.startswith("manual_") or decision in {
                "questions_required",
                "apply_failed",
                "apply_failed_exception",
            }:
                summary["manual"] += 1
                source_bucket["manual"] += 1

            if query:
                query_bucket = summary["by_query"].setdefault(
                    query,
                    {
                        "decisions": 0,
                        "auto_applied": 0,
                        "positive": 0,
                        "rejected": 0,
                    },
                )
                query_bucket["decisions"] += 1
                if decision == "applied_auto":
                    query_bucket["auto_applied"] += 1

            if resume_variant and decision == "applied_auto":
                variant_bucket = summary["by_resume_variant"].setdefault(
                    resume_variant,
                    {"applications": 0, "positive": 0, "rejected": 0},
                )
                variant_bucket["applications"] += 1

        elif event_type == "invitation":
            summary["invitations"] += 1

        elif event_type == "negotiation_status":
            latest_status_by_vacancy[_vacancy_key(event)] = event

    for vacancy_key, status_event in latest_status_by_vacancy.items():
        bucket = status_event.get("status_bucket", "unknown")
        apply_event = latest_apply_by_vacancy.get(vacancy_key)
        if bucket == "positive":
            summary["positive_statuses"] += 1
        elif bucket == "rejected":
            summary["rejected_statuses"] += 1
        elif bucket == "pending":
            summary["pending_statuses"] += 1

        if not apply_event:
            continue

        source = apply_event.get("source", "unknown")
        source_bucket = summary["by_source"].setdefault(
            source,
            {
                "decisions": 0,
                "auto_applied": 0,
                "manual": 0,
                "positive": 0,
                "rejected": 0,
            },
        )
        query = apply_event.get("search_query", "")
        resume_variant = apply_event.get("resume_variant", "")

        if bucket == "positive":
            source_bucket["positive"] += 1
        elif bucket == "rejected":
            source_bucket["rejected"] += 1

        if query:
            query_bucket = summary["by_query"].setdefault(
                query,
                {
                    "decisions": 0,
                    "auto_applied": 0,
                    "positive": 0,
                    "rejected": 0,
                },
            )
            if bucket == "positive":
                query_bucket["positive"] += 1
            elif bucket == "rejected":
                query_bucket["rejected"] += 1

        if resume_variant:
            variant_bucket = summary["by_resume_variant"].setdefault(
                resume_variant,
                {"applications": 0, "positive": 0, "rejected": 0},
            )
            if bucket == "positive":
                variant_bucket["positive"] += 1
            elif bucket == "rejected":
                variant_bucket["rejected"] += 1

    summary["top_decisions"] = decision_counter.most_common(8)

    # ── Воронка ──
    funnel_applied = summary["auto_applied"]
    funnel_viewed = 0
    funnel_rejected = summary["rejected_statuses"]
    funnel_positive = summary["positive_statuses"]
    funnel_pending = summary["pending_statuses"]
    for status_event in latest_status_by_vacancy.values():
        bucket = status_event.get("status_bucket", "unknown")
        if bucket in ("positive", "rejected", "pending"):
            funnel_viewed += 1
    summary["funnel"] = {
        "applied": funnel_applied,
        "viewed": funnel_viewed,
        "pending": funnel_pending,
        "rejected": funnel_rejected,
        "positive": funnel_positive,
        "response_rate": round(funnel_viewed / funnel_applied * 100, 1) if funnel_applied else 0,
        "positive_rate": round(funnel_positive / funnel_applied * 100, 1) if funnel_applied else 0,
    }

    # ── A/B resume: добавляем viewed/pending и conversion rates ──
    for vacancy_key, status_event in latest_status_by_vacancy.items():
        apply_event = latest_apply_by_vacancy.get(vacancy_key)
        if not apply_event:
            continue
        resume_variant = apply_event.get("resume_variant", "")
        if not resume_variant:
            continue
        variant_bucket = summary["by_resume_variant"].setdefault(
            resume_variant,
            {"applications": 0, "positive": 0, "rejected": 0},
        )
        bucket = status_event.get("status_bucket", "unknown")
        variant_bucket.setdefault("viewed", 0)
        variant_bucket.setdefault("pending", 0)
        if bucket in ("positive", "rejected", "pending"):
            variant_bucket["viewed"] += 1
        if bucket == "pending":
            variant_bucket["pending"] += 1

    for variant_bucket in summary["by_resume_variant"].values():
        apps = variant_bucket.get("applications", 0)
        viewed = variant_bucket.get("viewed", 0)
        positive = variant_bucket.get("positive", 0)
        variant_bucket["response_rate"] = round(viewed / apps * 100, 1) if apps else 0
        variant_bucket["positive_rate"] = round(positive / apps * 100, 1) if apps else 0

    return summary
