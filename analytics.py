"""Persistent analytics/event history for matcher tuning and funnel analysis."""
from __future__ import annotations

import json
import fcntl
import logging
import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from contextvars import ContextVar
from contextlib import contextmanager
import hashlib
import uuid
from functools import wraps

import config
import resume_versions
from state_store.json_store import JsonStore
from state_store.private_journal import append_json, read_json_records
from outcome import status_bucket as _status_bucket, status_detail_bucket as _status_detail_bucket

log = logging.getLogger("analytics")

_event_context = ContextVar("analytics_context", default={})
_event_destination = ContextVar("analytics_destination", default=None)


def _destination():
    return _event_destination.get() or (os.path.abspath(config.ANALYTICS_EVENTS_FILE),
                                      os.path.abspath(config.ANALYTICS_STATE_FILE),
                                      os.path.realpath(config.JOB_HUNTER_HOME))


@contextmanager
def event_context(**fields):
    destination = _event_destination.set(_destination())
    token = _event_context.set({**_event_context.get(), **fields})
    try:
        yield
    finally:
        _event_context.reset(token)
        _event_destination.reset(destination)


def current_context():
    return dict(_event_context.get())


_run_observation = ContextVar("search_observation", default=None)
FUNNEL_FIELDS = ("fetched", "already_seen", "new", "keyword_pass", "matcher_pass",
                 "apply_attempt", "applied", "manual", "uncertain", "skipped", "failed", "guard_stop",
                 "deferred_unscored", "dry_run_match", "not_processed", "retry_existing")


def best_effort(function):
    """Diagnostic writes cannot turn a completed action into another attempt."""
    @wraps(function)
    def wrapped(*args, **kwargs):
        try:
            return function(*args, **kwargs)
        except Exception as exc:
            log.warning("Analytics observation failed: error_kind=%s", type(exc).__name__)
    return wrapped


def decision_outcome(decision, evaluation=None, note=""):
    evaluation = evaluation or {}
    if decision == "applied_auto":
        return "applied", "applied"
    if decision == "dry_run_match":
        return "dry_run_match", "dry_run_match"
    if decision == "already_applied":
        return "skipped", "already_applied"
    if decision == "skipped_keyword_filter":
        return "skipped", "keyword_filter"
    if decision == "skipped_red_flags":
        return "skipped", "red_flags"
    if decision == "skipped_low_score":
        return "skipped", "closed_or_archived" if "closed_or_archived" in note else "low_score"
    if decision == "deferred_unscored":
        return "deferred_unscored", "deferred_unscored"
    if decision == "guard_stop":
        return "guard_stop", "guard_stop"
    if decision == "not_processed":
        return "not_processed", note if note in {"run_limit", "run_incomplete"} else "unclassified"
    if decision == "apply_uncertain":
        return "manual", "apply_uncertain"
    if decision.startswith("apply_failed"):
        return "failed", "guard_stop" if "no_cover_letter" in evaluation.get("guard_flags", []) else "apply_failed"
    if decision.startswith("manual_") or decision == "questions_required":
        return "manual", "manual_required"
    if decision == "skipped_blacklisted":
        return "skipped", "company_blacklisted"
    return "unclassified", "unclassified"


class SearchObservation:
    """One run's bounded-by-collected-vacancies diagnostics, never decision inputs."""
    def __init__(self, run_id, mode):
        self.run_id, self.mode = run_id, mode
        self.started_at = _now().isoformat(timespec="seconds")
        self.result = {"found": 0, "applied": 0, "source_stats": {}}
        self.counters = defaultdict(Counter)
        self.reasons = Counter()
        self.candidates, self.decided = {}, set()
        self.applied_keys = set()
        self.stage = "startup"
        self.ok = False
        self.error_kind = ""
        self.failure_stage = ""
        self.stop_reason = ""
        self.failures = []
        self.history_file = config.RUN_HISTORY_FILE

    def entry(self, *, incomplete=False):
        stats = self.result.get("source_stats", {})
        totals = Counter()
        sources = {}
        for source in sorted(set(stats) | set(self.counters)):
            bucket = dict(stats.get(source, {}))
            funnel = {field: int(self.counters[source].get(field, 0)) for field in FUNNEL_FIELDS}
            for field in ("fetched", "already_seen", "new"):
                funnel[field] = int(bucket.get(field, 0) or 0)
            funnel["manual"] = int(bucket.get("manual", funnel["manual"]) or 0)
            bucket["funnel"] = funnel
            bucket["keyword_pass"] = funnel["keyword_pass"]
            bucket["matcher_pass"] = funnel["matcher_pass"]
            bucket["apply_attempt"] = funnel["apply_attempt"]
            sources[source] = bucket
            totals.update(funnel)
        now = _now().isoformat(timespec="seconds")
        entry = {"kind": "search", "run_id": self.run_id, "mode": self.mode,
                "started_at": self.started_at, "finished_at": None if incomplete else now,
                "created_at": now, "status": "incomplete" if incomplete else "finished",
                "ok": False if incomplete else self.ok, "found": self.result.get("found", 0),
                "new": totals["new"], "applied": totals["applied"], "manual": totals["manual"],
                "skipped": self.result.get("skipped", 0), "deferred": self.result.get("deferred", 0),
                "source_stats": sources, "funnel": dict(totals), "reason_breakdown": dict(self.reasons),
                "failure_stage": (self.failure_stage or self.stage) if incomplete or not self.ok else "",
                "error_kind": self.error_kind, "error": "hh_unexpected_ui" if self.error_kind == "HHUnexpectedUI" else self.error_kind,
                "note": self.result.get("note", ""), "stage_failures": list(self.failures)}
        if self.result.get("hh_recovery"):
            entry["hh_recovery"] = dict(self.result["hh_recovery"])
        if incomplete:
            return incomplete_run_counts(entry, totals["applied"],
                {source: bucket["funnel"]["applied"] for source, bucket in sources.items()})
        return entry


@contextmanager
def observe_search(run_id, mode):
    observation = SearchObservation(run_id, mode)
    token = _run_observation.set(observation)
    with event_context(run_id=run_id, stage="startup", channel="search"):
        try:
            yield observation
        finally:
            _run_observation.reset(token)


def current_search():
    return _run_observation.get()


@best_effort
def search_stage(stage, vacancy=None):
    fields = {"stage": stage}
    if vacancy is not None:
        fields.update(vacancy_id=str(vacancy.get("id") or ""), source=vacancy.get("source", "unknown"))
    else:
        fields.update(vacancy_id="", source="")
    _event_context.set({**_event_context.get(), **fields})
    observation = current_search()
    if observation is not None:
        observation.stage = stage


@best_effort
def register_candidates(vacancies):
    observation = current_search()
    if observation is not None:
        for vacancy in vacancies:
            observation.candidates[(vacancy.get("source", "unknown"), str(vacancy.get("id")))] = vacancy
            if vacancy.get("_hh_retry"):
                observation.counters[vacancy.get("source", "unknown")]["retry_existing"] += 1


@best_effort
def count_stage(stage, vacancy):
    observation = current_search()
    if observation is not None:
        observation.counters[vacancy.get("source", "unknown")][stage] += 1


@best_effort
def count_application(vacancy):
    """Count a successful receipt once, independently of later state handling."""
    observation = current_search()
    key = (vacancy.get("source", "unknown"), str(vacancy.get("id")))
    if observation is not None and key not in observation.applied_keys:
        observation.applied_keys.add(key)
        observation.counters[key[0]]["applied"] += 1


@contextmanager
def _diagnostic_context(*, vacancy=True):
    allowed = ("run_id", "source", "vacancy_id") if vacancy else ("run_id", "source")
    token = _event_context.set({key: value for key, value in current_context().items() if key in allowed})
    try:
        yield
    finally:
        _event_context.reset(token)


@best_effort
def record_failure(stage, error, *, source=None, continued=False):
    context = current_context()
    source = source or context.get("source") or "unknown"
    fields = {"source": source, "stage": stage, "error_kind": type(error).__name__, "continued": continued}
    observation = current_search()
    if observation is not None:
        observation.failures.append(fields)
        if not continued:
            observation.failure_stage = stage
    if config.ANALYTICS_ENABLED:
        with _diagnostic_context():
            _append_event({"event": "stage_failed", **fields})


@best_effort
def record_event(payload):
    """Fail-soft entry to the existing journal for workflow metadata."""
    return _append_event(payload)


@best_effort
def record_unexpected_ui(fingerprint, stage):
    if config.ANALYTICS_ENABLED:
        with _diagnostic_context(vacancy=False):
            _append_event({"event": "unexpected_ui", "fingerprint": fingerprint, "stage": stage, "source": "hh"})


def zero_apply_diagnosis(run):
    if run.get("status") == "incomplete" and run.get("applied_count_status") != "exact":
        return ""
    funnel = run.get("funnel") or {}
    new = funnel.get("new", run.get("new"))
    found = run.get("found", 0)
    retries = funnel.get("retry_existing", 0)
    applied = funnel.get("applied", run.get("applied"))
    if applied is None or not (new or found or retries) or applied or run.get("mode") == "dry-run":
        return ""
    reasons = run.get("reason_breakdown") or {}
    uncertain = funnel.get("uncertain", run.get("uncertain", 0)) or reasons.get("apply_uncertain", 0)
    if uncertain:
        lines = [f"Подтверждено откликов: 0; требуется ручная сверка: {uncertain}"]
    else:
        lines = [f"0 откликов: новых {new}" if new is not None else f"0 откликов: найдено {found}"]
    if retries:
        lines.append(f"• повторных кандидатов: {retries}")
    labels = (("keyword_filter", "keyword filter"), ("low_score", "matcher: low score"),
              ("red_flags", "matcher: red flags"), ("manual_required", "manual"),
              ("guard_stop", "guard stop"), ("apply_failed", "apply failed"),
              ("apply_uncertain", "исход отклика не подтверждён"),
              ("already_applied", "already applied"), ("company_blacklisted", "blacklist"),
              ("closed_or_archived", "closed/archived"), ("deferred_unscored", "оценка отложена"),
              ("run_limit", "лимит прогона"), ("run_incomplete", "прогон прерван"))
    classified = 0
    for key, label in labels:
        count = reasons.get(key, 0)
        if isinstance(count, int) and count > 0:
            classified += count
            lines.append(f"• {label}: {count}")
    if not classified or reasons.get("unclassified") or classified < ((new if new is not None else found) + retries):
        lines.append("• причина не классифицирована")
    return "\n".join(lines)


async def tracked_call(stage, run_id, vacancy, function, *args, **kwargs):
    if current_search() is not None:
        search_stage(stage, vacancy)
    with event_context(stage=stage, run_id=run_id,
                       vacancy_id=str(vacancy.get("id") or ""),
                       source=vacancy.get("source", "unknown")):
        result = await function(*args, **kwargs)
        if stage == "apply" and isinstance(result, dict) and result.get("ok") and not result.get("already_applied"):
            count_application(vacancy)
        return result


def chat_context(function):
    @wraps(function)
    async def wrapped(*args, **kwargs):
        with event_context(channel="chat", stage="chat", source="hh", vacancy_id=""):
            return await function(*args, **kwargs)
    return wrapped


@best_effort
def finish_unclassified(observation):
    for key, vacancy in observation.candidates.items():
        if key not in observation.decided:
            record_decision(run_id=observation.run_id, vacancy=vacancy, decision="not_processed",
                            note=observation.stop_reason or ("run_incomplete" if not observation.ok else "unclassified"))


def latest_run_records(records):
    """Fold append-only start/finish checkpoints by ID; preserve legacy records."""
    seen, result = set(), []
    for record in reversed(records):
        if not isinstance(record, dict):
            continue
        key = record.get("run_id")
        if key and key in seen:
            continue
        if key:
            seen.add(key)
        result.append(record)
    return list(reversed(result))


def incomplete_run_counts(record, minimum=0, source_minimum=None, *, zero_proven=False):
    """Annotate a surviving checkpoint; never treat absence of receipts as zero."""
    result = dict(record)
    result["applied_count_status"] = "exact" if zero_proven else ("minimum" if minimum else "unknown")
    result["applied"] = minimum if minimum or zero_proven else None
    result["funnel"] = {**(record.get("funnel") or {}), "applied": result["applied"]}
    for field in ("apply_attempt", "manual"):
        result["funnel"][field] = result["funnel"].get(field) or None
        if field in result:
            result[field] = result[field] or None
    source_minimum = source_minimum or {}
    sources = {}
    for source in sorted(set(record.get("source_stats") or {}) | set(source_minimum)):
        bucket = dict((record.get("source_stats") or {}).get(source, {}))
        count = source_minimum.get(source, 0)
        bucket["applied_count_status"] = "exact" if zero_proven else ("minimum" if count else "unknown")
        bucket["applied"] = count if count or zero_proven else None
        bucket["funnel"] = {**(bucket.get("funnel") or {}), "applied": bucket["applied"]}
        for field in ("apply_attempt", "manual"):
            bucket["funnel"][field] = bucket["funnel"].get(field) or None
            if field in bucket:
                bucket[field] = bucket[field] or None
        sources[source] = bucket
    result["source_stats"] = sources
    return result


def format_run_count(run, field="applied", *, incomplete=None):
    """A display value, not a business counter: exact, lower bound, or unknown."""
    value = (run.get("funnel") or {}).get(field, run.get(field, None if field == "applied" else 0))
    incomplete = run.get("status") == "incomplete" if incomplete is None else incomplete
    uncertain = (run.get("funnel") or {}).get("uncertain", run.get("uncertain", 0))
    uncertain = uncertain or (run.get("reason_breakdown") or {}).get("apply_uncertain", 0)
    if field == "applied" and uncertain and not incomplete:
        confirmed = str(value) if value is not None else "неизвестно"
        return f"{confirmed} подтверждено; {uncertain} требуют сверки"
    if incomplete:
        if field == "applied" and run.get("applied_count_status") == "exact":
            return str(value) if value is not None else "неизвестно"
        return f">={value}" if isinstance(value, int) and value > 0 else "неизвестно"
    return str(value) if value is not None else "неизвестно"


def reconcile_run_records(records, *, events_file=None):
    """Read-only reconciliation of missing finals using the existing run journal."""
    records = latest_run_records(records)
    pending = {record.get("run_id") for record in records if record.get("status") == "incomplete" and record.get("run_id")}
    if not pending:
        return records
    events = [event for event in _iter_events(events_file) if event.get("run_id") in pending]
    receipts = [event for event in events if event.get("mode") != "dry-run" and not event.get("dry_run") and (
        (event.get("event") == "application_result" and event.get("outcome") == "sent") or
        (event.get("event") == "decision" and event.get("decision") == "applied_auto"))]
    # Both the workflow receipt and its terminal decision describe the same action.
    aliases = {(event["run_id"], event["application_id"]): (event.get("source", ""), str(event["vacancy_id"]))
        for event in receipts if event.get("application_id") and event.get("vacancy_id")}
    keys = defaultdict(set)
    unidentified = defaultdict(set)
    for event in receipts:
        run_id, source = event["run_id"], event.get("source", "")
        if event.get("vacancy_id"):
            key = (source, str(event["vacancy_id"]))
        elif event.get("application_id"):
            key = aliases.get((run_id, event["application_id"]), (source, "application:" + event["application_id"]))
        else:
            unidentified[run_id].add(source)
            continue
        keys[run_id].add(key)
    result = []
    for record in records:
        if record.get("status") != "incomplete":
            result.append(record)
            continue
        run_id = record.get("run_id")
        counts = Counter(source for source, key in keys[run_id] if source)
        for source in unidentified[run_id]:
            if source:
                counts[source] = max(counts[source], 1)
        minimum = max(sum(counts.values()), len(keys[run_id]), int(bool(unidentified[run_id])),
            record.get("applied") or 0)
        zero_proven = False
        for event in events:
            if event.get("run_id") != run_id or event.get("event") != "search_finished":
                continue
            funnel = event.get("funnel") or {}
            count = funnel.get("applied", event.get("applied"))
            if type(count) is int and event.get("mode") != "dry-run":
                minimum = max(minimum, count)
            if (event.get("status") == "finished" and type(count) is int and count == 0
                    and type(funnel.get("apply_attempt")) is int and funnel["apply_attempt"] == 0):
                zero_proven = True
        result.append(incomplete_run_counts(record, minimum, counts, zero_proven=zero_proven and minimum == 0))
    return result



@best_effort
def record_llm_call(provider, model, response=None, error_kind=None):
    """Record usage metadata only; never prompts, answers, credentials or URLs."""
    usage = getattr(response, "usage", None)
    def count(name):
        value = usage.get(name) if isinstance(usage, dict) else getattr(usage, name, None)
        return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None
    _append_event({
        "event": "llm_call", "call_id": uuid.uuid4().hex,
        "provider": provider, "model": model,
        "outcome": "error" if error_kind else "response",
        "error_kind": error_kind,
        "input_tokens": count("prompt_tokens"),
        "output_tokens": count("completion_tokens"),
        "total_tokens": count("total_tokens"),
        "cost_usd": None, "pricing_version": None,
        "cost_status": "unknown",
    })



def _now() -> datetime:
    return datetime.now()


def _parse_dt(value: object | None) -> datetime | None:
    if not isinstance(value, str) or not value:
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


def _default_state() -> dict:
    return {
        "negotiation_status_by_vacancy": {},
        "last_poll_by_vacancy": {},
        "invitation_keys": [],
        "historical_decision_keys": [],
    }


def _valid_state(state: dict) -> bool:
    for key in ("negotiation_status_by_vacancy", "last_poll_by_vacancy"):
        value = state.get(key, {})
        if not isinstance(value, dict) or any(not isinstance(item, str) for item in value.values()):
            return False
    for key in ("invitation_keys", "historical_decision_keys"):
        value = state.get(key, [])
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            return False
    return True


def _reconcile_event_tail(state: dict) -> dict:
    """Replay only events after the last durable state checkpoint."""
    invitation_keys = set(state.get("invitation_keys", []))
    historical_keys = set(state.get("historical_decision_keys", []))
    try:
        events_file = _destination()[0]
        with open(events_file, "rb") as stream:
            fcntl.flock(stream.fileno(), fcntl.LOCK_SH)
            stat = os.fstat(stream.fileno())
            identity = [os.path.realpath(events_file), stat.st_dev, stat.st_ino]
            checkpoint = state.get("_journal_checkpoint") or {}
            offset = checkpoint.get("offset", 0) if isinstance(checkpoint, dict) else 0
            if (
                not isinstance(checkpoint, dict) or checkpoint.get("identity") != identity
                or not isinstance(offset, int) or isinstance(offset, bool)
                or offset < 0 or offset > stat.st_size
            ):
                offset = 0
            stream.seek(offset)
            while True:
                start = stream.tell()
                line = stream.readline()
                if not line:
                    break
                try:
                    event = json.loads(line)
                except (ValueError, UnicodeError):
                    if not line.endswith(b"\n"):
                        # Retry an unfinished legacy record after more data arrives.
                        stream.seek(start)
                        break
                    continue
                if not isinstance(event, dict):
                    continue
                kind = event.get("event")
                vacancy_id = str(event.get("vacancy_id") or "").strip()
                if kind == "negotiation_status" and vacancy_id:
                    status = event.get("status")
                    if isinstance(status, str) and status.strip():
                        state["negotiation_status_by_vacancy"][vacancy_id] = status.strip()
                elif kind == "negotiation_observation" and vacancy_id:
                    observed_at = event.get("observed_at_utc")
                    if isinstance(observed_at, str) and observed_at:
                        state["last_poll_by_vacancy"][vacancy_id] = observed_at
                elif kind == "invitation":
                    try:
                        invitation_keys.add(_vacancy_key(event))
                    except (AttributeError, TypeError):
                        continue
                elif kind == "decision" and (event.get("historical") or event.get("mode") == "historical"):
                    decision = event.get("decision")
                    if vacancy_id and isinstance(decision, str) and decision:
                        source = event.get("source") or _source_from_vacancy_id(vacancy_id)
                        historical_keys.add(f"{source}:{vacancy_id}:{decision}")
            state["_journal_checkpoint"] = {"identity": identity, "offset": stream.tell()}
    except FileNotFoundError:
        pass
    # Other read errors deliberately propagate: an unreadable journal is not
    # evidence that no invitation/backfill has already been recorded.
    state["invitation_keys"] = sorted(invitation_keys)
    state["historical_decision_keys"] = sorted(historical_keys)
    return state


def _recover_state_from_events() -> dict:
    return _reconcile_event_tail(_default_state())


def _recover_corrupt_state() -> dict:
    state = _recover_state_from_events()
    if not state.get("_journal_checkpoint", {}).get("offset"):
        state["_recovery_required"] = "corrupt_state_without_event_history"
    return state


def _state_store() -> JsonStore:
    return JsonStore(
        _destination()[1],
        default_factory=_recover_state_from_events,
        corrupt_factory=_recover_corrupt_state,
        validator=_valid_state,
        logger=log,
        read_error_message="Analytics state read failed",
    )


def _load_state() -> dict:
    state = _state_store().load()
    for key, value in _default_state().items():
        state.setdefault(key, value)
    return _reconcile_event_tail(state)


def _update_state(mutator, default=None):
    result = default

    def update(state: dict) -> dict:
        nonlocal result
        for key, value in _default_state().items():
            state.setdefault(key, value)
        if state.get("_recovery_required"):
            raise RuntimeError("Corrupt analytics state needs manual event history recovery")
        _reconcile_event_tail(state)
        result = mutator(state)
        # Events are appended before the state checkpoint. If this save fails,
        # the next update replays that tail and does not emit duplicates.
        return _reconcile_event_tail(state)

    try:
        _state_store().update(update)
    except Exception as exc:
        log.warning("Analytics state update failed: %s", type(exc).__name__)
        return default
    return result


def _append_event(payload: dict, *, durable: bool = False) -> bool:
    if not config.ANALYTICS_ENABLED:
        return False

    payload = {**_event_context.get(), **payload}
    payload.setdefault("schema_version", 2)
    payload.setdefault("event_id", uuid.uuid4().hex)
    payload.setdefault("recorded_at_utc", datetime.now(timezone.utc).isoformat())
    payload.setdefault("created_at", _now().isoformat(timespec="seconds"))
    # Stable local identity; does not disclose the runtime filesystem path.
    payload.setdefault("profile_id", hashlib.sha256(_destination()[2].encode()).hexdigest()[:20])
    payload.setdefault("rules_version", "matcher-v1")
    try:
        append_json(_destination()[0], payload, durable=durable)
        return True
    except Exception as exc:
        log.warning("Failed to append analytics event: %s", type(exc).__name__)
        return False


def new_run_id(mode: str) -> str:
    stamp = _now().strftime("%Y%m%dT%H%M%S")
    return f"{mode}-{stamp}-{os.getpid()}-{uuid.uuid4().hex[:12]}"


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


@best_effort
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


@best_effort
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
        **{field: result.get(field) for field in ("started_at", "finished_at", "status", "new",
            "funnel", "reason_breakdown", "failure_stage", "error_kind", "stage_failures", "hh_recovery") if field in result},
    })


@best_effort
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
    outcome, reason_code = decision_outcome(decision, evaluation, note)
    observation = current_search()
    key = (vacancy.get("source", "unknown"), str(vacancy.get("id")))
    if observation is not None and run_id == observation.run_id and key not in observation.decided:
        observation.decided.add(key)
        if outcome == "applied":
            count_application(vacancy)
        else:
            observation.counters[key[0]][outcome] += 1
        observation.reasons[reason_code] += 1
        if decision == "apply_uncertain":
            observation.counters[key[0]]["uncertain"] += 1
        if reason_code == "guard_stop" and outcome != "guard_stop":
            observation.counters[key[0]]["guard_stop"] += 1
    log.info("decision=%s reason=%s", outcome, reason_code,
             extra={"observation_fields": {"run_id": run_id, "source": key[0],
                    "vacancy_id": key[1], "stage": "decision"}})
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
        "outcome": outcome,
        "reason_code": reason_code,
        "reason_group": "matcher_reject" if reason_code in {"low_score", "red_flags"} else reason_code,
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
        "submission_mode": vacancy.get("_analytics_apply_mode", "") or ("manual" if note.startswith("manual_ai") else "unknown"),
        "score": evaluation.get("score"),
        "match_score": evaluation.get("score"),
        "response_probability_score": evaluation.get("response_probability_score"),
        "cluster": evaluation.get("cluster", "") or vacancy.get("cluster", ""),
        "cover_style": evaluation.get("cover_style", ""),
        "cover_letter_hash": evaluation.get("cover_letter_hash", ""),
        "cover_letter_status": evaluation.get("cover_letter_status", "unknown"),
        "cover_letter_length": evaluation.get("cover_letter_length", 0),
        "has_cover_letter": (bool(evaluation.get("cover_letter_length")) if "cover_letter_length" in evaluation else None),
        "cover_letter_features": dict(evaluation.get("cover_letter_features", {}) or {}),
        "fallback_cover_letter": bool(evaluation.get("fallback_cover_letter", False)),
        "overclaim_guard": bool(evaluation.get("overclaim_guard", False)),
        "preferred_resume_variant": evaluation.get("preferred_resume_variant", ""),
        "should_apply": bool(evaluation.get("should_apply", False)),
        "evaluation_status": evaluation.get("evaluation_status", "scored"),
        "error_kind": evaluation.get("error_kind", ""),
        "reason": evaluation.get("reason", ""),
        "red_flags": list(evaluation.get("red_flags", []) or []),
        "hard_flags": list(evaluation.get("hard_flags", []) or evaluation.get("red_flags", []) or []),
        "soft_flags": list(evaluation.get("soft_flags", []) or []),
        "guard_flags": list(evaluation.get("guard_flags", []) or []),
        "note": note,
    }
    payload.update(_resume_variant_payload(resume_variant))
    payload.update(resume_versions.payload(vacancy))
    _append_event(payload)


def record_negotiation_statuses(items: list[dict]) -> None:
    if not config.ANALYTICS_ENABLED or not items:
        return

    _update_state(lambda state: _record_negotiation_statuses(state, items))


def _record_negotiation_statuses(state: dict, items: list[dict]) -> None:
    last_status_by_vacancy = state.setdefault("negotiation_status_by_vacancy", {})

    for item in items:
        vacancy_id = str(item.get("id") or "").strip()
        status_text = str(item.get("status") or "").strip()
        if not vacancy_id or not status_text:
            continue

        polls = state.setdefault("last_poll_by_vacancy", {})
        observed_at = datetime.now(timezone.utc).isoformat()
        if not _append_event({
            "event": "negotiation_observation", "vacancy_id": vacancy_id,
            "source": "hh", "observed_at_utc": observed_at,
            "previous_poll_at": polls.get(vacancy_id),
            "status_bucket": _status_bucket(status_text),
            "status_detail_bucket": _status_detail_bucket(status_text),
        }, durable=True):
            continue
        polls[vacancy_id] = observed_at
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
        if not _append_event(payload, durable=True):
            continue
        last_status_by_vacancy[vacancy_id] = status_text


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

    _update_state(lambda state: _record_invitations(state, items))


def _record_invitations(state: dict, items: list[dict]) -> None:
    invitation_keys = set(state.setdefault("invitation_keys", []))

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

        if not _append_event(
            {
                "event": "invitation",
                "created_at": _now().isoformat(timespec="seconds"),
                **payload,
            },
            durable=True,
        ):
            continue
        invitation_keys.add(key)
    state["invitation_keys"] = sorted(invitation_keys)


def _map_historical_action(action: str) -> tuple[str, str]:
    if action == "applied":
        return "applied_auto", "historical_seen"
    if action == "apply_uncertain":
        return "apply_uncertain", "historical_seen"
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

    return _update_state(
        lambda state: _backfill_seen_decisions(state, entries, run_id),
        default={"added": 0, "by_decision": {}},
    )


def _backfill_seen_decisions(state: dict, entries: dict, run_id: str) -> dict:
    historical_keys = set(state.setdefault("historical_decision_keys", []))
    decision_counter = Counter()
    added = 0
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
        if not _append_event(
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
            },
            durable=True,
        ):
            continue
        historical_keys.add(historical_key)
        decision_counter[decision] += 1
        added += 1
    state["historical_decision_keys"] = sorted(historical_keys)

    return {
        "added": added,
        "by_decision": dict(sorted(decision_counter.items(), key=lambda item: (-item[1], item[0]))),
    }


def _iter_events(events_file: str | None = None) -> list[dict]:
    path = events_file or _destination()[0]
    try:
        return read_json_records(path, strip_nuls=True)
    except Exception as exc:
        log.warning("Failed to read analytics events: %s", type(exc).__name__)
        return []


FILTER_AUDIT_ALLOWED_DECISIONS = {
    "applied_auto",
    "already_applied",
    "questions_required",
    "apply_uncertain",
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


def summarize(
    days: int | None = None,
    *,
    events_file: str | None = None,
    all_time: bool = False,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
) -> dict:
    days = days if days is not None else config.ANALYTICS_RECENT_DAYS
    cutoff = start_at if start_at is not None else (
        None if all_time else (_now() - timedelta(days=max(0, days)))
    )
    events = []
    for event in _iter_events(events_file):
        created_at = _parse_dt(event.get("created_at"))
        if created_at is None:
            continue
        if cutoff is not None and created_at < cutoff:
            continue
        if end_at is not None and created_at >= end_at:
            continue
        events.append(event)

    summary = {
        "days": days,
        "all_time": all_time,
        "events": len(events),
        "search_runs": 0,
        "decisions": 0,
        "auto_applied": 0,
        "uncertain": 0,
        "dry_run_matched": 0,
        "manual": 0,
        "keyword_filtered": 0,
        "red_flagged": 0,
        "low_score": 0,
        "deferred_unscored": 0,
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
        "reason_breakdown": {},
        "reason_groups": {},
        "search_funnel": {},
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
            for field, count in (event.get("funnel") or {}).items():
                summary["search_funnel"][field] = summary["search_funnel"].get(field, 0) + _coerce_int(count)
        elif event_type == "decision":
            summary["decisions"] += 1
            decision = event.get("decision", "")
            decision_counter[decision or "unknown"] += 1
            reason_code = event.get("reason_code") or decision_outcome(decision, event, event.get("note", ""))[1]
            summary["reason_breakdown"][reason_code] = summary["reason_breakdown"].get(reason_code, 0) + 1
            reason_group = event.get("reason_group") or ("matcher_reject" if reason_code in {"low_score", "red_flags"} else reason_code)
            summary["reason_groups"][reason_group] = summary["reason_groups"].get(reason_group, 0) + 1
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
            elif decision == "deferred_unscored":
                summary["deferred_unscored"] += 1
                source_bucket["deferred_unscored"] = source_bucket.get("deferred_unscored", 0) + 1

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
                "apply_uncertain",
                "apply_failed",
                "apply_failed_exception",
            }:
                summary["manual"] += 1
                source_bucket["manual"] += 1
                cluster_bucket["manual"] += 1
                if decision == "apply_uncertain":
                    summary["uncertain"] += 1
                    source_bucket["uncertain"] = source_bucket.get("uncertain", 0) + 1

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
