"""Persistent analytics/event history for matcher tuning and funnel analysis."""
from __future__ import annotations

import json
import logging
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta

import config
from outcome import status_bucket as _status_bucket, status_detail_bucket as _status_detail_bucket

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
        "area": vacancy.get("area", ""),
        "remote": vacancy.get("remote", ""),
        "schedule": vacancy.get("schedule", ""),
        "salary": vacancy.get("salary", ""),
        "required_experience": vacancy.get("experience", "") or vacancy.get("required_experience", ""),
        "number_of_applicants": vacancy.get("number_of_applicants", "") or vacancy.get("applicant_count", ""),
        "published_at": vacancy.get("published_at", "") or vacancy.get("publishedDate", "") or vacancy.get("date_published", ""),
        "snippet": vacancy.get("snippet", ""),
        "details": _trim_details(details),
        "search_query": vacancy.get("_search_query", ""),
        "search_profile": vacancy.get("_search_profile", ""),
        "search_path": vacancy.get("_search_path", ""),
        "is_retry": bool(vacancy.get("_hh_retry")),
        "last_known_status": vacancy.get("_hh_last_status", ""),
        "retry_reason": vacancy.get("_hh_retry_reason", ""),
        "retry_outcome": vacancy.get("_hh_retry_outcome", "") or vacancy.get("_hh_retry_reason", ""),
        "retry_after": vacancy.get("_hh_retry_after", ""),
        "apply_mode": vacancy.get("apply_mode", ""),
        "score": evaluation.get("score"),
        "match_score": evaluation.get("score"),
        "response_probability_score": evaluation.get("response_probability_score"),
        "cluster": evaluation.get("cluster", "") or vacancy.get("cluster", ""),
        "cover_style": evaluation.get("cover_style", ""),
        "cover_letter_hash": evaluation.get("cover_letter_hash", ""),
        "cover_letter_length": evaluation.get("cover_letter_length", 0),
        "cover_letter_features": dict(evaluation.get("cover_letter_features", {}) or {}),
        "fallback_cover_letter": bool(evaluation.get("fallback_cover_letter", False)),
        "overclaim_guard": bool(evaluation.get("overclaim_guard", False)),
        "preferred_resume_variant": evaluation.get("preferred_resume_variant", ""),
        "should_apply": bool(evaluation.get("should_apply", False)),
        "reason": evaluation.get("reason", ""),
        "red_flags": list(evaluation.get("red_flags", []) or []),
        "hard_flags": list(evaluation.get("hard_flags", []) or evaluation.get("red_flags", []) or []),
        "soft_flags": list(evaluation.get("soft_flags", []) or []),
        "guard_flags": list(evaluation.get("guard_flags", []) or []),
        "note": note,
    }
    payload.update(_resume_variant_payload(resume_variant))
    _append_event(payload)


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
            "status_detail_bucket": _status_detail_bucket(status_text),
            "prev_status": prev_status,
        }
        _append_event(payload)
        last_status_by_vacancy[vacancy_id] = status_text
        changed = True

    if changed:
        _save_state()


def _questionnaire_items_payload(items: list[dict]) -> list[dict]:
    payload_items = []
    for item in items or []:
        if not isinstance(item, dict):
            continue
        question = str(item.get("question") or item.get("question_text") or "").strip()
        answer = str(item.get("answer") or "").strip()
        if not question and not answer:
            continue
        payload_items.append(
            {
                "question_text": question[:500],
                "answer": answer[:1000],
                "control": str(item.get("control") or "").strip(),
                "required": bool(item.get("required", False)),
                "starred": bool(item.get("starred", False)),
                "best_guess": bool(item.get("best_guess", False)),
            }
        )
    return payload_items


def record_questionnaire(
    *,
    run_id: str,
    vacancy: dict,
    question_answers: list[dict],
    success: bool,
    reason: str = "",
) -> None:
    """Record structured HH employer questionnaire answers."""
    if not config.ANALYTICS_ENABLED:
        return

    items = _questionnaire_items_payload(question_answers)
    if not items and not reason:
        return

    _append_event(
        {
            "event": "questionnaire",
            "created_at": _now().isoformat(timespec="seconds"),
            "run_id": run_id,
            "source": vacancy.get("source", "") or "hh",
            "source_label": vacancy.get("source_label", "") or "hh.ru",
            "vacancy_id": str(vacancy.get("id") or "").strip(),
            "title": vacancy.get("title", ""),
            "company": vacancy.get("company", ""),
            "url": vacancy.get("url", ""),
            "success": bool(success),
            "reason": str(reason or "")[:500],
            "questions_count": len(items),
            "answered_count": sum(1 for item in items if item.get("answer")),
            "required_count": sum(1 for item in items if item.get("required")),
            "starred_count": sum(1 for item in items if item.get("starred")),
            "best_guess_count": sum(1 for item in items if item.get("best_guess")),
            "question_answers": items,
        }
    )


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


def _iter_events(events_file: str | None = None) -> list[dict]:
    path = events_file or config.ANALYTICS_EVENTS_FILE
    if not os.path.exists(path):
        return []

    events = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.replace("\x00", "").strip()
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


FILTER_AUDIT_ALLOWED_DECISIONS = {
    "applied_auto",
    "already_applied",
    "questions_required",
    "apply_failed",
    "apply_failed_exception",
    "manual_review",
    "manual_hh",
    "manual_hh_guard",
    "manual_hh_limit",
    "manual_geekjob_session",
    "manual_habr_session",
}

FILTER_AUDIT_VIABLE_CLUSTERS = {
    "manual_web_qa",
    "api_qa",
    "mobile_qa",
    "qa_support_adjacent",
    "enterprise_banking_qa",
    "junior_aqa_python",
}


def _filter_audit_sample(event: dict, cluster: str) -> dict:
    return {
        "created_at": event.get("created_at", ""),
        "decision": event.get("decision", ""),
        "score": event.get("score"),
        "cluster": cluster,
        "recorded_cluster": event.get("cluster", ""),
        "title": event.get("title", ""),
        "company": event.get("company", ""),
        "note": event.get("note", ""),
        "reason": str(event.get("reason", ""))[:220],
    }


def audit_filters(
    days: int | None = None,
    *,
    events_file: str | None = None,
    all_time: bool = False,
    limit: int = 20,
    min_viable_score: int = 50,
) -> dict:
    """Replay current deterministic vacancy classifier over analytics history."""
    from matcher import classify_vacancy_cluster

    days = days if days is not None else config.ANALYTICS_RECENT_DAYS
    cutoff = None if all_time else (_now() - timedelta(days=max(0, days)))
    cluster_counter = Counter()
    decision_cluster_counter = Counter()
    audited_decisions = 0
    would_block_allowed = []
    low_score_viable = []
    keyword_filtered_viable = []

    for event in _iter_events(events_file):
        if event.get("event") != "decision":
            continue
        created_at = _parse_dt(event.get("created_at"))
        if created_at is None:
            continue
        if cutoff is not None and created_at < cutoff:
            continue

        audited_decisions += 1
        decision = str(event.get("decision") or "")
        cluster = classify_vacancy_cluster(event, event.get("details") or "")
        cluster_counter[cluster] += 1
        decision_cluster_counter[(decision or "unknown", cluster)] += 1
        sample = _filter_audit_sample(event, cluster)

        if decision in FILTER_AUDIT_ALLOWED_DECISIONS and cluster == "reject_non_qa":
            would_block_allowed.append(sample)
        if (
            decision == "skipped_low_score"
            and cluster in FILTER_AUDIT_VIABLE_CLUSTERS
            and _coerce_int(event.get("score"), default=0) >= min_viable_score
        ):
            low_score_viable.append(sample)
        if (
            decision == "skipped_keyword_filter"
            and cluster in FILTER_AUDIT_VIABLE_CLUSTERS
            and str(event.get("note") or "") == "relevant_keywords"
        ):
            keyword_filtered_viable.append(sample)

    return {
        "days": days,
        "all_time": all_time,
        "decisions": audited_decisions,
        "by_cluster": cluster_counter.most_common(),
        "by_decision_cluster": [
            {"decision": decision, "cluster": cluster, "count": count}
            for (decision, cluster), count in decision_cluster_counter.most_common(20)
        ],
        "would_block_allowed_or_manual_count": len(would_block_allowed),
        "would_block_allowed_or_manual": would_block_allowed[:limit],
        "low_score_viable_count": len(low_score_viable),
        "low_score_viable": low_score_viable[:limit],
        "keyword_filtered_viable_count": len(keyword_filtered_viable),
        "keyword_filtered_viable": keyword_filtered_viable[:limit],
    }


def _coerce_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def summarize(days: int | None = None, *, events_file: str | None = None, all_time: bool = False) -> dict:
    days = days if days is not None else config.ANALYTICS_RECENT_DAYS
    cutoff = None if all_time else (_now() - timedelta(days=max(0, days)))
    events = []
    for event in _iter_events(events_file):
        created_at = _parse_dt(event.get("created_at"))
        if created_at is None:
            continue
        if cutoff is not None and created_at < cutoff:
            continue
        events.append(event)

    summary = {
        "days": days,
        "all_time": all_time,
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
        "questionnaires": 0,
        "questionnaire_successes": 0,
        "questionnaire_failures": 0,
        "questionnaire_questions": 0,
        "questionnaire_best_guess": 0,
        "questionnaire_required": 0,
        "positive_statuses": 0,
        "rejected_statuses": 0,
        "pending_statuses": 0,
        "interview_statuses": 0,
        "offer_statuses": 0,
        "test_task_statuses": 0,
        "positive_other_statuses": 0,
        "pending_new_statuses": 0,
        "pending_viewed_statuses": 0,
        "by_source": {},
        "by_query": {},
        "by_resume_variant": {},
        "by_cluster": {},
        "by_cover_style": {},
        "by_retry_reason": {},
        "top_decisions": [],
    }

    decision_counter = Counter()
    latest_apply_by_vacancy = {}
    latest_status_by_vacancy = {}

    def conversion_bucket(mapping: dict, key: str) -> dict:
        return mapping.setdefault(
            key or "unknown",
            {
                "decisions": 0,
                "auto_applied": 0,
                "manual": 0,
                "viewed": 0,
                "not_viewed": 0,
                "pending": 0,
                "positive": 0,
                "rejected": 0,
                "response_rate": 0,
                "positive_rate": 0,
            },
        )

    def status_is_viewed(bucket: str, detail_bucket: str) -> bool:
        return bucket in {"positive", "rejected"} or detail_bucket == "pending_viewed"

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
            cluster = event.get("cluster") or "unknown"
            cover_style = event.get("cover_style") or ""
            retry_reason = event.get("retry_reason") or event.get("retry_outcome") or ""

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
            cluster_bucket = conversion_bucket(summary["by_cluster"], cluster)
            cluster_bucket["decisions"] += 1
            retry_bucket = None
            if retry_reason:
                retry_bucket = conversion_bucket(summary["by_retry_reason"], retry_reason)
                retry_bucket["decisions"] += 1

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
                cluster_bucket["auto_applied"] += 1
                if retry_bucket is not None:
                    retry_bucket["auto_applied"] += 1
                if cover_style:
                    style_bucket = conversion_bucket(summary["by_cover_style"], cover_style)
                    style_bucket["auto_applied"] += 1
                latest_apply_by_vacancy[_vacancy_key(event)] = event
            elif decision == "already_applied":
                source_bucket["rejected"] += 1
                cluster_bucket["rejected"] += 1
            elif decision.startswith("manual_") or decision in {
                "questions_required",
                "apply_failed",
                "apply_failed_exception",
            }:
                summary["manual"] += 1
                source_bucket["manual"] += 1
                cluster_bucket["manual"] += 1

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

        elif event_type == "questionnaire":
            summary["questionnaires"] += 1
            if event.get("success"):
                summary["questionnaire_successes"] += 1
            else:
                summary["questionnaire_failures"] += 1
            summary["questionnaire_questions"] += int(event.get("questions_count") or 0)
            summary["questionnaire_best_guess"] += int(event.get("best_guess_count") or 0)
            summary["questionnaire_required"] += int(event.get("required_count") or 0)

        elif event_type == "negotiation_status":
            latest_status_by_vacancy[_vacancy_key(event)] = event

    for vacancy_key, status_event in latest_status_by_vacancy.items():
        bucket = status_event.get("status_bucket", "unknown")
        detail_bucket = status_event.get("status_detail_bucket") or _status_detail_bucket(status_event.get("status", ""))
        apply_event = latest_apply_by_vacancy.get(vacancy_key)
        if bucket == "positive":
            summary["positive_statuses"] += 1
        elif bucket == "rejected":
            summary["rejected_statuses"] += 1
        elif bucket == "pending":
            summary["pending_statuses"] += 1

        if detail_bucket == "interview":
            summary["interview_statuses"] += 1
        elif detail_bucket == "offer":
            summary["offer_statuses"] += 1
        elif detail_bucket == "test_task":
            summary["test_task_statuses"] += 1
        elif detail_bucket == "positive_other":
            summary["positive_other_statuses"] += 1
        elif detail_bucket == "pending_new":
            summary["pending_new_statuses"] += 1
        elif detail_bucket == "pending_viewed":
            summary["pending_viewed_statuses"] += 1

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
        cluster = apply_event.get("cluster") or "unknown"
        cover_style = apply_event.get("cover_style") or ""
        retry_reason = apply_event.get("retry_reason") or apply_event.get("retry_outcome") or ""
        viewed = status_is_viewed(bucket, detail_bucket)
        not_viewed = detail_bucket == "pending_new"
        cluster_bucket = conversion_bucket(summary["by_cluster"], cluster)
        style_bucket = conversion_bucket(summary["by_cover_style"], cover_style) if cover_style else None
        retry_bucket = conversion_bucket(summary["by_retry_reason"], retry_reason) if retry_reason else None

        if viewed:
            cluster_bucket["viewed"] += 1
            if style_bucket is not None:
                style_bucket["viewed"] += 1
            if retry_bucket is not None:
                retry_bucket["viewed"] += 1
        if not_viewed:
            cluster_bucket["not_viewed"] += 1
            if style_bucket is not None:
                style_bucket["not_viewed"] += 1
            if retry_bucket is not None:
                retry_bucket["not_viewed"] += 1
        if bucket == "pending":
            cluster_bucket["pending"] += 1
            if style_bucket is not None:
                style_bucket["pending"] += 1
            if retry_bucket is not None:
                retry_bucket["pending"] += 1

        if bucket == "positive":
            source_bucket["positive"] += 1
            cluster_bucket["positive"] += 1
            if style_bucket is not None:
                style_bucket["positive"] += 1
            if retry_bucket is not None:
                retry_bucket["positive"] += 1
        elif bucket == "rejected":
            source_bucket["rejected"] += 1
            cluster_bucket["rejected"] += 1
            if style_bucket is not None:
                style_bucket["rejected"] += 1
            if retry_bucket is not None:
                retry_bucket["rejected"] += 1

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
    funnel_rejected = 0
    funnel_positive = 0
    funnel_pending = 0
    funnel_not_viewed = 0
    for vacancy_key, status_event in latest_status_by_vacancy.items():
        if vacancy_key not in latest_apply_by_vacancy:
            continue
        bucket = status_event.get("status_bucket", "unknown")
        detail_bucket = status_event.get("status_detail_bucket") or _status_detail_bucket(status_event.get("status", ""))
        if status_is_viewed(bucket, detail_bucket):
            funnel_viewed += 1
        if detail_bucket == "pending_new":
            funnel_not_viewed += 1
        if bucket == "positive":
            funnel_positive += 1
        elif bucket == "rejected":
            funnel_rejected += 1
        elif bucket == "pending":
            funnel_pending += 1
    summary["funnel"] = {
        "applied": funnel_applied,
        "viewed": funnel_viewed,
        "pending": funnel_pending,
        "not_viewed": funnel_not_viewed,
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
        detail_bucket = status_event.get("status_detail_bucket") or _status_detail_bucket(status_event.get("status", ""))
        variant_bucket.setdefault("viewed", 0)
        variant_bucket.setdefault("not_viewed", 0)
        variant_bucket.setdefault("pending", 0)
        if status_is_viewed(bucket, detail_bucket):
            variant_bucket["viewed"] += 1
        if detail_bucket == "pending_new":
            variant_bucket["not_viewed"] += 1
        if bucket == "pending":
            variant_bucket["pending"] += 1

    for group_name in ("by_resume_variant", "by_cluster", "by_cover_style", "by_retry_reason"):
        for bucket_values in summary[group_name].values():
            apps = bucket_values.get("applications", bucket_values.get("auto_applied", 0))
            viewed = bucket_values.get("viewed", 0)
            positive = bucket_values.get("positive", 0)
            bucket_values["response_rate"] = round(viewed / apps * 100, 1) if apps else 0
            bucket_values["positive_rate"] = round(positive / apps * 100, 1) if apps else 0

    return summary
