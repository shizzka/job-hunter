"""Deterministic diagnostics over existing search checkpoints and journal events."""
from collections import Counter
from datetime import datetime
import uuid


def _time(value):
    try:
        return datetime.fromisoformat(value) if isinstance(value, str) else None
    except ValueError:
        return None


def _run_counters(data, events, incomplete):
    funnel = data.get("funnel") or {}
    counters = {key: funnel.get(key) for key in (
        "fetched", "already_seen", "new", "keyword_pass", "evaluated", "matcher_pass",
        "applied", "skipped", "deferred_unscored", "not_processed", "failed", "uncertain")}
    for key, legacy in (("fetched", "found"), ("applied", "applied")):
        if counters[key] is None:
            counters[key] = data.get(legacy)
    decisions = {}
    for event in events:
        if event.get("event") == "decision" and event.get("vacancy_id"):
            key = (event.get("source") or "unknown", str(event["vacancy_id"]))
            if decisions.get(key, {}).get("decision") != "apply_uncertain":
                decisions[key] = event
    observed = Counter(e.get("outcome") for e in decisions.values())
    uncertain_keys = {key for key, event in decisions.items() if event.get("decision") == "apply_uncertain"}
    recorded_uncertain = counters.get("uncertain") or 0
    uncertain = max(recorded_uncertain, data.get("uncertain") or 0,
                    (data.get("reason_breakdown") or {}).get("apply_uncertain", 0), len(uncertain_keys))
    if uncertain:
        counters["uncertain"] = uncertain
    superseded = {(e.get("source") or "unknown", str(e.get("vacancy_id"))) for e in events
                  if e.get("decision") == "applied_auto" or (
                      e.get("event") == "application_result" and e.get("outcome") == "sent")}
    superseded &= uncertain_keys
    if superseded and (incomplete or recorded_uncertain < len(uncertain_keys)):
        if counters.get("applied") is not None:
            remaining = max(0, counters["applied"] - len(superseded))
            counters["applied"] = remaining if remaining or not incomplete else None
    # Journal decisions are lower bounds for interrupted runs, never exact zeros.
    for key in ("applied", "skipped", "deferred_unscored", "not_processed", "failed"):
        if observed[key] and (incomplete or counters[key] is None):
            counters[key] = max(counters[key] or 0, observed[key])

    return counters, decisions, observed


def _reconstruct_legacy_failures(run_id, events, decisions, failures):
    # Older journals recorded the uncertainty decision but omitted its failure
    # event. Reconstruct a stable identity from that explicit evidence, without
    # rewriting history or guessing the missing execution stage.
    legacy_causes = {}
    positions = {id(event): index for index, event in enumerate(events)}
    for key, event in decisions.items():
        if key[0] != "hh" or event.get("decision") != "apply_uncertain":
            continue
        fid = event.get("failure_id") or uuid.uuid5(
            uuid.NAMESPACE_URL, f"job-hunter:{run_id}:{key[0]}:{key[1]}:apply_uncertain").hex
        legacy_causes[key] = fid
        if not any(f.get("failure_id") == fid for f in failures):
            failures.append({"failure_id": fid, "reason_code": "apply_uncertain",
                "source": key[0], "vacancy_id": key[1], "stage": "unknown",
                "retryable": False, "continued": False, "source_stopped": True,
                "created_at": event.get("recorded_at_utc") or event.get("created_at"),
                "correlation_origin": "journal_decision"})
    # Legacy navigation guards have explicit failed trace-stage metadata but
    # no failure ID. Join only the same vacancy's unrecovered isolation and
    # hard-stop decision; generic guards/text never prove a navigation cause.
    for key, event in decisions.items():
        if (key[0] != "hh" or event.get("decision") != "guard_stop"
                or event.get("note") != "hh:ui_hard_stop" or event.get("failure_id")):
            continue
        prior = events[:positions[id(event)]]
        matching = [e for e in prior if (e.get("source"), str(e.get("vacancy_id"))) == key]
        isolation = next((e for e in reversed(matching) if e.get("event") == "hh_ui_isolation"), None)
        artifact = next((e for e in reversed(matching)
                         if e.get("event") == "debug_artifact" and e.get("artifact_type") == "apply_trace"), None)
        if (isolation is None or isolation.get("recovered") is not False
                or artifact is None or artifact.get("stage") != "VACANCY_NAVIGATION"
                or artifact.get("outcome") != "error"
                or positions[id(artifact)] > positions[id(isolation)]
                or any(e.get("event") in {"decision", "hh_ui_isolation"}
                       and positions[id(artifact)] < positions[id(e)] < positions[id(isolation)]
                       for e in matching)):
            continue
        fid = uuid.uuid5(uuid.NAMESPACE_URL,
                        f"job-hunter:{run_id}:{key[0]}:{key[1]}:vacancy_navigation_failed").hex
        legacy_causes[key] = fid
        failures.append({"failure_id": fid, "reason_code": "vacancy_navigation_failed",
            "source": key[0], "vacancy_id": key[1], "stage": "VACANCY_NAVIGATION",
            "retryable": False, "continued": False, "source_stopped": True,
            "created_at": artifact.get("recorded_at_utc") or artifact.get("created_at"),
            "correlation_origin": "journal_trace_stage"})
    return legacy_causes, positions


def _run_failure_summary(run_id, data, events, decisions, observed, counters, incomplete):
    roots, affected, downstream = {}, {}, {}
    journal_failures = [e for e in events if e.get("event") in {"stage_failed", "failure_observed"}]
    # Checkpoints preserve observations even if an individual journal write failed.
    first_events = {e.get("failure_id"): e for e in reversed(journal_failures) if e.get("event") == "stage_failed"}
    failures = [{**f, **first_events.get(f.get("failure_id"), {})} for f in data.get("stage_failures") or []]
    saved_ids = {f.get("failure_id") for f in failures if f.get("failure_id")}
    failures += [e for e in journal_failures if e.get("event") == "failure_observed" or e.get("failure_id") not in saved_ids]
    legacy_causes, positions = _reconstruct_legacy_failures(run_id, events, decisions, failures)
    aliases = {f['failure_id']: f['parent_failure_id'] for f in failures
               if f.get('failure_id') and f.get('parent_failure_id')}
    definitions = {f['failure_id']: f for f in reversed(failures)
                   if f.get('failure_id') and not f.get('parent_failure_id')}
    def origin(fid):
        seen = set()
        while fid in aliases and fid not in seen:
            seen.add(fid)
            fid = aliases[fid]
        return fid
    for index, failure in enumerate(failures):
        fid = origin(failure.get("parent_failure_id") or failure.get("failure_id")) or f"legacy-{index}"
        if fid not in roots:
            root = definitions.get(fid, {} if failure.get('parent_failure_id') else failure)
            roots[fid] = {"failure_id": fid if failure.get('failure_id') or failure.get('parent_failure_id') else None,
                "reason_code": root.get("reason_code") or "UNKNOWN_ROOT_CAUSE",
                "stage": root.get("stage") or data.get("failure_stage") or "unknown",
                "source": root.get("source") or "unknown", "provider": root.get("provider"),
                "retryable": root.get("retryable"), "fallback_status": root.get("fallback_status"),
                "first_failure_at": root.get("recorded_at_utc") or root.get("created_at"),
                "continued": root.get("continued", False)}
            if root.get("correlation_origin"):
                roots[fid]["correlation_origin"] = root["correlation_origin"]
            if root.get("collection"):
                roots[fid]["collection"] = dict(root["collection"])
        affected.setdefault(fid, set())
        downstream.setdefault(fid, Counter())
        if failure.get("vacancy_id"):
            affected[fid].add((failure.get("source") or "unknown", str(failure["vacancy_id"])))
    for key, event in decisions.items():
        inferred = legacy_causes.get(key)
        if (not inferred and key[0] == "hh" and event.get("decision") == "not_processed"
                and event.get("note") == "hh_recovery_breaker"):
            prior = [(positions[id(decisions[cause])], fid) for cause, fid in legacy_causes.items()
                     if cause[0] == key[0] and positions[id(decisions[cause])] < positions[id(event)]]
            inferred = max(prior)[1] if prior else None
        fid = origin(event.get("failure_id") or inferred)
        if fid:
            if fid not in roots:
                roots[fid] = {"failure_id": fid, "reason_code": "UNKNOWN_ROOT_CAUSE",
                    "stage": "unknown", "source": event.get("source"), "provider": None,
                    "retryable": None, "fallback_status": None,
                    "first_failure_at": event.get("recorded_at_utc") or event.get("created_at")}
            affected.setdefault(fid, set()).add(key)
            downstream.setdefault(fid, Counter())[event.get("decision", "decision")] += 1
    problems = sum(counters.get(key) or 0 for key in ("deferred_unscored", "failed", "uncertain"))
    limited = (data.get("reason_breakdown") or {}).get("run_limit", 0)
    unprocessed = counters.get("not_processed") or 0
    problems += max(0, unprocessed - limited)
    if not roots and (data.get("ok") is False and not incomplete or problems):
        roots["unknown"] = {"failure_id": None, "reason_code": "UNKNOWN_ROOT_CAUSE",
            "stage": data.get("failure_stage") or "unknown", "source": "unknown",
            "provider": None, "retryable": None, "fallback_status": None,
            "first_failure_at": None}
        # Unlinked legacy consequences have a count, but no asserted correlation.
        affected["unknown"] = set()
        downstream["unknown"] = Counter({key: value for key, value in observed.items()
            if key in {"not_processed", "deferred_unscored", "failed"}})
    for fid, root in roots.items():
        root["affected_count"] = len(affected.get(fid, ())) or None
        root["downstream_events"] = dict(sorted(downstream.get(fid, {}).items()))
    # A source-stopping failure takes priority over a recovered failure. Within
    # each class use the first durable timestamp, then stable insertion order.
    def failure_order(item):
        timestamp = _time(item[1].get("first_failure_at"))
        return (bool(item[1].get("continued")), timestamp is None,
                timestamp.timestamp() if timestamp else 0)
    roots = dict(sorted(roots.items(), key=failure_order))
    return roots, affected, failures, problems, limited


def _run_status(data, counters, roots, incomplete, problems, limited):
    progress = sum(counters.get(key) or 0 for key in ("evaluated", "applied", "matcher_pass"))
    if any(root['stage'] == 'collection' for root in roots.values()):
        stats = data.get("source_stats") or {}
        if any(bucket.get("collection") for bucket in stats.values()):
            progress += sum((bucket.get("collection") or {}).get("vacancies_retained", bucket.get("fetched", 0)) or 0
                            for bucket in stats.values())
        else:
            progress += counters.get("fetched") or 0
    if incomplete:
        status = "incomplete"
    elif roots or problems or data.get("ok") is False:
        status = "partial" if progress else "failed"
    elif limited:
        status = "partial"
    elif data.get("ok") is True:
        status = "success"
    else:
        status = "unknown"
    return status


def build_run_summary(run, events=(), *, profile=""):
    """No text-log inference. Missing legacy data remains explicitly unknown."""
    run_id = run.get("run_id", "")
    events = [e for e in events if run_id and e.get("run_id") == run_id]
    started = next((e for e in events if e.get("event") == "search_started"), {})
    finished = next((e for e in reversed(events) if e.get("event") == "search_finished"), {})
    data = {**run, **finished}
    if finished:
        data["status"] = finished.get("status") or "finished"
        data["finished_at"] = finished.get("finished_at") or finished.get("created_at")
    incomplete = data.get("status") == "incomplete" or not finished and not data.get("finished_at") and data.get("ok") is None
    funnel = data.get("funnel") or {}
    counters, decisions, observed = _run_counters(data, events, incomplete)

    roots, affected, failures, problems, limited = _run_failure_summary(
        run_id, data, events, decisions, observed, counters, incomplete)
    status = _run_status(data, counters, roots, incomplete, problems, limited)
    start_at = data.get("started_at") or started.get("created_at")
    finish_at = data.get("finished_at")
    start_time, finish_time = _time(start_at), _time(finish_at)
    duration = None
    if start_time and finish_time and (start_time.tzinfo is None) == (finish_time.tzinfo is None):
        duration = max(0, (finish_time - start_time).total_seconds())
    sources = sorted(set((data.get("source_stats") or {})) | {
        e["source"] for e in events if e.get("source") and e["source"] != "unknown"})
    if not sources:
        sources = started.get("enabled_sources", [])
    artifacts = []
    for event in events:
        if event.get("event") == "debug_artifact" and event.get("artifact_ref"):
            ref = {key: event.get(key) for key in ("artifact_type", "artifact_ref", "trace_id", "source", "vacancy_id")}
            key = (event.get("source") or "unknown", str(event.get("vacancy_id", "")))
            ref["failure_ids"] = [roots[fid]["failure_id"] for fid in affected
                                  if key in affected[fid] and roots[fid].get("failure_id")]
            if ref not in artifacts:
                artifacts.append(ref)
    from analytics import run_observation_fields
    observation = run_observation_fields({**data, "status": "incomplete" if incomplete else data.get("status"),
                                         "funnel": {**funnel, **counters}, "stage_failures": failures})
    return {"run_id": run_id, "profile": profile, "profile_id": data.get("profile_id") or started.get("profile_id"),
        "direction": data.get("direction"), "direction_title": data.get("direction_title", ""),
        "started_at": start_at, "finished_at": finish_at, "duration_seconds": duration,
        "sources": sources, "source_stats": data.get("source_stats") or {},
        "stage_failures": failures, "status": status, "counters": counters,
        "counter_status": "minimum_or_unknown" if incomplete else "recorded",
        "primary_failure": next(iter(roots.values()), None), "failure_count": len(roots),
        "stop_reason": "RUN_LIMIT" if limited else None,
        "artifacts": artifacts, **observation}
