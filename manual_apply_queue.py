"""Persistent queue for Telegram-confirmed AI vacancy applications."""
from __future__ import annotations

import hashlib
import html
import json
import logging
import math
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

import config
import company_blacklist
import seen
from direction_history import runtime_metadata
from state_store.private_journal import iter_json_records
from state_store.protected import ProtectedJsonStore
from state_store.process_owner import current_process_owner, owner_alive as _owner_alive

log = logging.getLogger(__name__)

CALLBACK_MANUAL_APPLY = "manual_apply"
CALLBACK_MANUAL_FEEDBACK = "manual_fb"
CALLBACK_MANUAL_BLOCK_COMPANY = "manual_block_company"
CALLBACK_MANUAL_WHY = "manual_why"
CALLBACK_MANUAL_SNOOZE = "manual_snooze"
FEEDBACK_LABELS = {
    "good": "норм",
    "bad": "мимо",
}
MAX_ITEMS = 250
MAX_AGE_SECONDS = 7 * 24 * 60 * 60
ORPHAN_APPLY_GRACE_SECONDS = 5 * 60
ORPHAN_APPLY_MESSAGE = "Отклик мог уйти — проверьте на hh.ru"


def _recover_applying(data: dict, now: float, token: str | None = None) -> None:
    items = data["items"]
    candidates = ([items[token]] if token in items else []) if token is not None else items.values()
    for item in candidates:
        if item.get("status") != "applying" or _owner_alive(item):
            continue
        try:
            started = float(item.get("applying_ts") or 0)
            if not started and item.get("updated_at"):
                started = datetime.fromisoformat(item["updated_at"]).timestamp()
            started = started or float(item.get("created_ts") or 0)
        except (ValueError, TypeError, OverflowError):
            started = 0
        if not math.isfinite(started) or started <= 0:
            started = item.setdefault("recovery_observed_ts", now)
        if now - started < ORPHAN_APPLY_GRACE_SECONDS:
            continue
        item.update(status="uncertain", message=ORPHAN_APPLY_MESSAGE, orphan_recovered=True,
                    updated_at=datetime.fromtimestamp(now).isoformat(timespec="seconds"))


def recover_orphaned_applies(*, profile_name: str | None = None) -> dict:
    """Local recovery only; never infer non-delivery or schedule another apply."""
    return _store(profile_name).update(lambda data: _recover_applying(data, time.time()))


def _queue_path(profile_name: str | None = None) -> Path:
    if profile_name:
        import profile as profile_mod
        profile = profile_mod.load_profile(profile_name)
        return Path(profile.home_dir) / "manual_apply_queue.json"
    fallback = os.path.join(config.JOB_HUNTER_HOME, "manual_apply_queue.json")
    return Path(getattr(config, "MANUAL_APPLY_QUEUE_FILE", fallback) or fallback)



def _valid_queue(data: dict) -> bool:
    items = data.get("items")
    return isinstance(items, dict) and all(
        isinstance(item, dict)
        and all(name not in item or isinstance(item[name], dict) for name in ("vacancy", "evaluation"))
        for item in items.values()
    )


def _store(profile_name: str | None = None) -> ProtectedJsonStore:
    # Resolve once, before waiting: every operation uses the locked profile's
    # original path, even if process-wide active config changes in the meantime.
    path = _queue_path(profile_name)
    return ProtectedJsonStore(
        path, lock_path=path.with_suffix(".lock"),
        default_factory=lambda: {"items": {}}, validator=_valid_queue,
    )


def _read_queue(profile_name: str | None = None) -> dict:
    return _store(profile_name).load()


def _write_queue(data: dict, profile_name: str | None = None) -> None:
    """Explicit full replacement; queue mutations use a locked transaction."""
    _store(profile_name).save(data)


def _safe_profile(profile_name: str | None) -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]+", "_", (profile_name or "default").strip() or "default")


def _compact_vacancy(vacancy: dict) -> dict:
    keys = (
        "id", "source", "source_label", "title", "company", "salary", "url",
        "snippet", "response_url", "apply_mode", "_search_query", "_search_profile",
        "_resume_input_versions", "_direction_selection", "_cover_letter_selection", "_hh_salary_warning",
        "_hh_work_conditions", "_analytics_run_id",
    )
    compact = {key: vacancy.get(key) for key in keys if vacancy.get(key) not in (None, "")}
    if "source" not in compact:
        compact["source"] = "hh"
    if "source_label" not in compact:
        compact["source_label"] = compact.get("source", "hh")
    return compact


def _compact_evaluation(evaluation: dict) -> dict:
    keys = (
        "score", "response_probability_score", "reason", "should_apply",
        "cluster", "resume_variant", "cover_style", "red_flags", "guard_flags",
        "soft_flags", "hard_flags", "error_kind",
        "evaluation_status", "work_conditions_status",
        "direction", "direction_title", "hh_resume_hash", "hh_resume_numeric_id", "variant_text_sha256",
    )
    return {key: evaluation.get(key) for key in keys if evaluation.get(key) not in (None, "", [])}


def _prune(data: dict, now: float) -> None:
    items = data.setdefault("items", {})
    stale = []
    for token, item in items.items():
        try:
            created = float(item.get("created_ts") or 0)
        except (TypeError, ValueError):
            created = 0
        if item.get("status") not in {"applying", "uncertain"} and created and now - created > MAX_AGE_SECONDS:
            stale.append(token)
    for token in stale:
        items.pop(token, None)

    if len(items) <= MAX_ITEMS:
        return
    ordered = sorted(items.items(), key=lambda pair: float((pair[1] or {}).get("created_ts") or 0), reverse=True)
    data["items"] = {token: item for index, (token, item) in enumerate(ordered)
                     if index < MAX_ITEMS or item.get("status") in {"applying", "uncertain"}}


def create_candidate(
    vacancy: dict,
    evaluation: dict,
    details: str = "",
    *,
    profile_name: str | None = None,
    allow_ai_apply: bool = True,
) -> dict:
    now = time.time()
    seed = "|".join(
        str(part or "")
        for part in (
            now,
            vacancy.get("source"),
            vacancy.get("id"),
            vacancy.get("url"),
            vacancy.get("title"),
            vacancy.get("company"),
        )
    )
    token = hashlib.sha256(seed.encode("utf-8")).hexdigest()[:12]
    item = {
        "run_id": vacancy.get("_analytics_run_id") or "",
        **runtime_metadata(vacancy),
        "token": token,
        "profile_name": _safe_profile(profile_name or os.getenv("JOB_HUNTER_DEFAULT_PROFILE", "default")),
        "status": "pending",
        "allow_ai_apply": bool(allow_ai_apply),
        "created_ts": now,
        "created_at": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
        "vacancy": _compact_vacancy(vacancy),
        "evaluation": _compact_evaluation(evaluation),
        "details": (details or "")[:8000],
    }

    def mutate(data):
        _prune(data, now)
        data["items"][token] = item

    _store(profile_name).update(mutate)
    return item


def get_candidate(token: str, *, profile_name: str | None = None) -> dict | None:
    token = (token or "").strip()
    if not token:
        return None
    store = _store(profile_name)
    import profile_access
    bound_direction = profile_access.current_scope()[1]
    if bound_direction:
        from direction_history import HistoryScope
        saved = store.load().get("items", {}).get(token)
        if not isinstance(saved, dict) or HistoryScope(store.path.parent).owner(saved) != bound_direction:
            return None
    data = store.update(lambda data: _recover_applying(data, time.time(), token))
    item = data.get("items", {}).get(token)
    if not isinstance(item, dict):
        return None
    if profile_name and _safe_profile(item.get("profile_name")) != _safe_profile(profile_name):
        return None
    return item


def mark_candidate(token: str, status: str, message: str = "", *, profile_name: str | None = None) -> dict | None:
    item = None

    def mutate(data):
        nonlocal item
        item = data["items"].get((token or "").strip())
        if item is not None:
            if item.get("status") == "applying":
                item["revoked"] = True
                return
            if item.get("external_started") or item.get("status") == "uncertain" or item.get("orphan_recovered"):
                return  # Generic UI updates cannot reopen or erase an external attempt.
            item["status"] = status
            item["updated_at"] = datetime.now().isoformat(timespec="seconds")
            if message:
                item["message"] = str(message)[:1000]

    _store(profile_name).update(mutate)
    return item


def claim_candidate(token: str, *, store=None) -> dict | None:
    claimed = None
    def mutate(data):
        nonlocal claimed
        _recover_applying(data, time.time(), token)
        item = data["items"].get(token)
        if (not item or item.get("status") != "pending" or not item.get("allow_ai_apply", True)
                or item.get("feedback") == "bad"):
            return
        item.update(status="applying", owner=uuid.uuid4().hex, external_started=False,
                    **current_process_owner(), applying_ts=time.time())
        item.pop("native_attempt", None)
        claimed = dict(item)
    (store or _store()).update(mutate)
    return claimed


def approval_valid(token, owner, *, store=None) -> bool:
    item = (store or _store()).load()["items"].get(token, {})
    return (item.get("owner") == owner and item.get("status") == "applying"
            and item.get("allow_ai_apply", True) and item.get("feedback") != "bad"
            and not item.get("revoked"))


def begin_external(token, owner, *, store=None) -> bool:
    started = False
    def mutate(data):
        nonlocal started
        item = data["items"].get(token, {})
        if (item.get("owner") == owner and item.get("status") == "applying"
                and item.get("allow_ai_apply", True) and item.get("feedback") != "bad"
                and not item.get("revoked") and not item.get("external_started")):
            item["external_started"] = True
            started = True
    (store or _store()).update(mutate)
    return started


def confirm_no_action(token, owner, *, store=None) -> bool:
    """Consume only the current native attempt's proven zero-dispatch result."""
    changed = False
    def mutate(data):
        nonlocal changed
        item = data["items"].get(token, {})
        if item.get("owner") == owner and item.get("status") == "applying" and item.get("external_started"):
            item["external_started"] = False
            changed = True
    (store or _store()).update(mutate)
    return changed


def allow_stub_cover(token: str, *, profile_name: str | None = None) -> bool:
    """«📨 Отправить шаблон» (B17): the owner accepts the template letter for this pending card."""
    import profile_access
    if profile_access.current_scope()[1] and get_candidate(token, profile_name=profile_name) is None:
        return False
    changed = False

    def mutate(data):
        nonlocal changed
        item = data["items"].get((token or "").strip())
        if item and item.get("status") == "pending":
            item["allow_stub_cover"] = True
            changed = True

    _store(profile_name).update(mutate)
    return changed


def finish_candidate(token, owner, status, message="", *, store=None) -> bool:
    changed = False
    def mutate(data):
        nonlocal changed
        item = data["items"].get(token, {})
        if item.get("owner") != owner or item.get("status") != "applying":
            return
        if status == "uncertain":
            status_value = "uncertain"
        elif item.get("external_started"):
            status_value = status if status in {"applied", "already_applied"} else "uncertain"
        elif item.get("revoked") or item.get("feedback") == "bad":
            status_value = "dismissed"
        else:
            status_value = status
        item.update(status=status_value, message=str(message)[:1000],
                    updated_at=datetime.now().isoformat(timespec="seconds"))
        changed = True
    (store or _store()).update(mutate)
    return changed


def resolve_orphaned_apply(token: str, *, profile_name: str, user_id: int,
                           confirmed_not_sent: bool = False) -> dict | None:
    """Owner reconciliation; retry creates a fresh pending approval, never dispatches."""
    resolved = None
    store = _store(profile_name)

    def mutate(data):
        nonlocal resolved
        _recover_applying(data, time.time(), token)
        item = data["items"].get(token)
        if not item or item.get("status") != "uncertain" or not item.get("orphan_recovered"):
            return
        retry_token = uuid.uuid4().hex[:12]
        if confirmed_not_sent:
            if _owner_alive(item):
                return
            from state_store.native_apply import NativeApplyRepository
            binding = item.get("native_attempt") or {}
            path = Path(binding.get("path") or store.path.with_name("hh_apply_attempts.json"))
            if path.name != "hh_apply_attempts.json":
                raise ValueError("Invalid manual native attempt binding")
            repository = NativeApplyRepository(path.with_name("hh_cookies.json"), "hh")
            url = binding.get("url") or (item.get("vacancy") or {}).get("url")
            if not repository.authorize_manual_retry(
                    url, token, item.get("owner"), binding.get("owner"), retry_token, int(user_id),
                    grace_seconds=ORPHAN_APPLY_GRACE_SECONDS,
                    not_before=float(item.get("applying_ts") or item.get("created_ts") or 0),
                    retry_of=item.get("retry_of", "")):
                return
        item.update(status="handled", resolved_by=int(user_id),
                    resolved_at=datetime.now().isoformat(timespec="seconds"),
                    resolution="confirmed_not_sent" if confirmed_not_sent else "handled")
        resolved = dict(item)
        if confirmed_not_sent:
            retry = {key: value for key, value in item.items() if key in {
                "profile_name", "vacancy", "evaluation", "details", "allow_ai_apply",
                "direction", "direction_title", "run_id", "profile_id",
                "hh_resume_hash", "hh_resume_numeric_id", "variant_text_sha256",
            }}
            now = time.time()
            retry.update(token=retry_token, status="pending", retry_of=token,
                         created_ts=now, created_at=datetime.fromtimestamp(now).isoformat(timespec="seconds"))
            data["items"][retry["token"]] = retry
            item["retry_token"] = retry["token"]
            resolved = dict(retry)

    store.update(mutate)
    return resolved


def claim_native_attempt(token, owner, repository, url, *, store):
    """Lock order is always queue -> native; a failed queue write cannot dispatch."""
    claimed = None

    def mutate(data):
        nonlocal claimed
        item = data["items"].get(token, {})
        if (item.get("owner") != owner or item.get("status") != "applying"
                or item.get("revoked") or item.get("feedback") == "bad"
                or not item.get("allow_ai_apply", True) or item.get("native_attempt")):
            return
        claimed = repository.claim(url, "", "", manual_token=token, manual_owner=owner,
                                   retry_of=item.get("retry_of", ""))
        if claimed:
            item["native_attempt"] = {"path": str(repository.path), "url": url, "owner": claimed}

    store.update(mutate)
    return claimed


def build_orphaned_apply_markup(item: dict, profile_name: str) -> dict:
    token = item["token"]
    rows = []
    url = (item.get("vacancy") or {}).get("url")
    if url:
        rows.append([{"text": "🔗 Проверить на hh.ru", "url": url}])
    rows.extend([
        [{"text": "Проверил — разобрано", "callback_data": f"mar:{profile_name}:{token}:handled"}],
        [{"text": "Проверил — отклика нет", "callback_data": f"mar:{profile_name}:{token}:retry"}],
    ])
    return {"inline_keyboard": rows}


def feedback_label(value: str) -> str:
    return FEEDBACK_LABELS.get((value or "").strip(), "")


def record_feedback(token: str, value: str, *, user_id: int = 0, profile_name: str | None = None,
                    consume_decision: bool = False) -> dict | None:
    value = (value or "").strip()
    if value not in FEEDBACK_LABELS:
        return None
    item = None

    def mutate(data):
        nonlocal item
        current = data["items"].get((token or "").strip())
        if current is None or (consume_decision and (
                current.get("status") != "pending" or current.get("snoozed_until", 0) > time.time()
                or _safe_profile(current.get("profile_name")) != _safe_profile(profile_name))):
            return
        item = current
        item["feedback"] = value
        item["feedback_label"] = feedback_label(value)
        item["feedback_at"] = datetime.now().isoformat(timespec="seconds")
        if user_id:
            item["feedback_user_id"] = int(user_id)
        if value == "bad" and item.get("status") == "applying":
            item["revoked"] = True
        if (value == "bad" or consume_decision) and item.get("status") == "pending":
            item["status"] = "dismissed"
        item["updated_at"] = item["feedback_at"]

    _store(profile_name).update(mutate)
    if item is not None:
        _append_feedback_log(item, profile_name)
    return item


FEEDBACK_LOG = "feedback_log.jsonl"


def _append_feedback_log(item: dict, profile_name: str | None) -> None:
    """Durable 👍/👎 history for learning (F1): the queue itself keeps 7 days / 250 cards."""
    vacancy = item.get("vacancy") or {}
    evaluation = item.get("evaluation") or {}
    selection = vacancy.get("_direction_selection") or {}
    record = {
        "at": item.get("feedback_at"), "feedback": item.get("feedback"), "status": item.get("status"),
        "vacancy_id": str(vacancy.get("id") or ""), "source": vacancy.get("source") or "hh",
        "title": vacancy.get("title"), "company": vacancy.get("company"), "salary": vacancy.get("salary"),
        "url": vacancy.get("url"), "query": vacancy.get("_search_query"), "search_profile": vacancy.get("_search_profile"),
        "direction": selection.get("direction"), "direction_title": selection.get("direction_title"),
        "score": evaluation.get("score"), "reason": str(evaluation.get("reason") or "")[:500],
    }
    try:
        path = _queue_path(profile_name).with_name(FEEDBACK_LOG)
        from direction_history import HistoryScope
        scope = HistoryScope(path.parent)
        record.update(scope.metadata(item))
        record["direction"] = selection.get("direction") or record["direction"]
        record["direction_title"] = selection.get("direction_title") or item.get("direction_title") or record["direction_title"]
        record["variant_text_sha256"] = selection.get("text_sha256")
        with path.open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception as exc:  # the button must keep working even if the log is not writable
        log.warning("feedback log write failed: %s", type(exc).__name__)


def _local_application_outcomes(home: Path) -> dict:
    """Saved evidence only: rendering must never collect/refresh HH data."""
    outcomes = seen.application_outcomes_from_file(str(home / "seen_vacancies.json"))
    for filename in ("negotiation_snapshots.jsonl", "analytics_events.jsonl"):
        for row in iter_json_records(home / filename):
            if filename == "negotiation_snapshots.jsonl":
                if row.get("kind"):
                    continue  # summaries are not vacancy observations
                vacancy_id = row.get("vacancyId")
            else:
                if row.get("event") not in {"negotiation_observation", "negotiation_status"}:
                    continue
                vacancy_id = row.get("vacancyId") or row.get("vacancy_id")
            if (row.get("source") or "hh") != "hh" or not vacancy_id:
                continue
            identity = str(vacancy_id).removeprefix("hh:")
            if outcomes.get(identity) != "apply_uncertain":
                outcomes[identity] = "already_applied"
    return outcomes


def _reconcile_pending(store, data: dict, profile_name: str) -> dict:
    if not any(item.get("status") == "pending"
               and _safe_profile(item.get("profile_name")) == _safe_profile(profile_name)
               and (item.get("vacancy", {}).get("source") or "hh") == "hh"
               for item in data["items"].values()):
        return data
    outcomes = _local_application_outcomes(store.path.parent)
    if not outcomes:
        return data

    def mutate(current):
        for item in current["items"].values():
            # Recheck under the queue lock: never overwrite a concurrent claim
            # or a finished/revoked attempt with an older listing snapshot.
            vacancy = item.get("vacancy") or {}
            if (item.get("status") != "pending"
                    or _safe_profile(item.get("profile_name")) != _safe_profile(profile_name)
                    or (vacancy.get("source") or "hh") != "hh"):
                continue
            action = outcomes.get(str(vacancy.get("id") or "").removeprefix("hh:"))
            if not action:
                continue
            uncertain = action == "apply_uncertain" or item.get("external_started")
            item.update(status="uncertain" if uncertain else "already_applied",
                        message="Нужна сверка: результат отклика не подтверждён."
                                if uncertain else "Уже откликались ранее (локальная история HH).",
                        updated_at=datetime.now().isoformat(timespec="seconds"))

    return store.update(mutate)


def list_candidates(profile_name: str, *, limit: int = 8, include_snoozed: bool = False) -> list[dict]:
    """Newest first; limit pending decisions, always include unresolved attempts."""
    now = time.time()
    store = _store(profile_name)
    data = _reconcile_pending(store, recover_orphaned_applies(profile_name=profile_name), profile_name)
    _prune(data, now)
    items = []
    for item in data.get("items", {}).values():
        if not isinstance(item, dict) or item.get("status") not in {"pending", "uncertain"}:
            continue
        if _safe_profile(item.get("profile_name")) != _safe_profile(profile_name):
            continue
        snoozed_until = float(item.get("snoozed_until") or 0)
        if item.get("status") == "uncertain":
            items.append(item)
            continue
        if not include_snoozed and snoozed_until > now:
            continue
        if company_blacklist.is_blocked((item.get("vacancy") or {}).get("company", ""), profile_name):
            continue
        items.append(item)
    items.sort(key=lambda item: float(item.get("created_ts") or 0), reverse=True)
    remaining_pending = max(1, int(limit or 1))
    listed = []
    for item in items:
        if item.get("status") != "uncertain":
            if remaining_pending <= 0:
                continue
            remaining_pending -= 1
        listed.append(item)
    return listed


def snooze_candidate(token: str, *, profile_name: str, hours: int = 24) -> dict | None:
    item = None

    def mutate(data):
        nonlocal item
        current = data["items"].get((token or "").strip())
        if (current is None or current.get("status") != "pending"
                or float(current.get("snoozed_until") or 0) > time.time()
                or _safe_profile(current.get("profile_name")) != _safe_profile(profile_name)):
            return
        item = current
        until = time.time() + max(1, int(hours)) * 3600
        item["snoozed_until"] = until
        item["snoozed_until_at"] = datetime.fromtimestamp(until).isoformat(timespec="seconds")
        item["updated_at"] = datetime.now().isoformat(timespec="seconds")

    _store(profile_name).update(mutate)
    return item


def manual_apply_callback_data(profile_name: str, token: str) -> str:
    return f"{CALLBACK_MANUAL_APPLY}:{_safe_profile(profile_name)}:{(token or '').strip()}"


def manual_feedback_callback_data(profile_name: str, token: str, value: str) -> str:
    return f"{CALLBACK_MANUAL_FEEDBACK}:{_safe_profile(profile_name)}:{(token or '').strip()}:{(value or '').strip()}"


def manual_block_company_callback_data(profile_name: str, token: str) -> str:
    return f"{CALLBACK_MANUAL_BLOCK_COMPANY}:{_safe_profile(profile_name)}:{(token or '').strip()}"


def manual_why_callback_data(profile_name: str, token: str) -> str:
    return f"{CALLBACK_MANUAL_WHY}:{_safe_profile(profile_name)}:{(token or '').strip()}"


def manual_snooze_callback_data(profile_name: str, token: str) -> str:
    return f"{CALLBACK_MANUAL_SNOOZE}:{_safe_profile(profile_name)}:{(token or '').strip()}"


def parse_manual_apply_callback_data(data: str) -> tuple[str, str]:
    prefix = f"{CALLBACK_MANUAL_APPLY}:"
    if not (data or "").startswith(prefix):
        return "", ""
    rest = data[len(prefix):]
    profile_name, sep, token = rest.partition(":")
    if not sep:
        return "", ""
    return _safe_profile(profile_name), token.strip()


def parse_apply_launch_callback_data(data: str) -> tuple[str, str]:
    parts = str(data).split(":")
    if len(parts) != 3 or parts[0] not in {CALLBACK_MANUAL_APPLY, "stub", "mad"}:
        return "", ""
    return _safe_profile(parts[1]), parts[2].strip()


def parse_manual_feedback_callback_data(data: str) -> tuple[str, str, str]:
    prefix = f"{CALLBACK_MANUAL_FEEDBACK}:"
    if not (data or "").startswith(prefix):
        return "", "", ""
    parts = data[len(prefix):].split(":")
    if len(parts) != 3:
        return "", "", ""
    profile_name, token, value = (part.strip() for part in parts)
    if value not in FEEDBACK_LABELS:
        return "", "", ""
    return _safe_profile(profile_name), token, value


def parse_manual_block_company_callback_data(data: str) -> tuple[str, str]:
    prefix = f"{CALLBACK_MANUAL_BLOCK_COMPANY}:"
    if not (data or "").startswith(prefix):
        return "", ""
    rest = data[len(prefix):]
    profile_name, sep, token = rest.partition(":")
    if not sep:
        return "", ""
    return _safe_profile(profile_name), token.strip()


def parse_manual_why_callback_data(data: str) -> tuple[str, str]:
    prefix = f"{CALLBACK_MANUAL_WHY}:"
    if not (data or "").startswith(prefix):
        return "", ""
    rest = data[len(prefix):]
    profile_name, sep, token = rest.partition(":")
    if not sep:
        return "", ""
    return _safe_profile(profile_name), token.strip()


def parse_manual_snooze_callback_data(data: str) -> tuple[str, str]:
    prefix = f"{CALLBACK_MANUAL_SNOOZE}:"
    if not (data or "").startswith(prefix):
        return "", ""
    rest = data[len(prefix):]
    profile_name, sep, token = rest.partition(":")
    if not sep:
        return "", ""
    return _safe_profile(profile_name), token.strip()


def _format_list_value(value: object, *, limit: int = 5) -> str:
    if not value:
        return ""
    if isinstance(value, (list, tuple, set)):
        items = [str(item).strip() for item in value if str(item).strip()]
    else:
        items = [str(value).strip()]
    if not items:
        return ""
    suffix = ""
    if len(items) > limit:
        suffix = f" +{len(items) - limit}"
    return ", ".join(items[:limit]) + suffix


def _shorten(value: object, max_len: int) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= max_len:
        return text
    return text[: max_len - 3].rstrip() + "..."


def build_manual_why_text(item: dict | None) -> str:
    if not isinstance(item, dict):
        return "❌ Не нашёл сохранённое решение по этой вакансии."

    vacancy = item.get("vacancy") or {}
    evaluation = item.get("evaluation") or {}
    title = vacancy.get("title") or "вакансия"
    company = vacancy.get("company") or "компания не указана"
    lines = [
        "Почему ручное решение",
        f"{_shorten(title, 100)} @ {_shorten(company, 80)}",
    ]

    source = vacancy.get("source_label") or vacancy.get("source")
    if source:
        lines.append(f"Источник: {source}")

    score = evaluation.get("score")
    response_score = evaluation.get("response_probability_score")
    if score not in (None, ""):
        lines.append(f"Подходит на {score}/100 · {review_score_label(score)}")
    if response_score not in (None, ""):
        lines.append(f"Оценка шанса ответа: {response_score}/100")

    for label, key in (
        ("Обратите внимание", "red_flags"),
        ("Дополнительные замечания", "soft_flags"),
    ):
        value = _format_list_value(evaluation.get(key))
        if value:
            lines.append(f"{label}: {_shorten(value, 220)}")
    if evaluation.get("guard_flags"):
        lines.append("Перед откликом нужна ваша проверка.")

    reason = evaluation.get("reason") or item.get("details") or ""
    if reason:
        lines.extend(["", _shorten(reason, 1200)])

    url = vacancy.get("url")
    if url:
        lines.extend(["", str(url)])

    return "\n".join(lines)


def build_manual_apply_markup(
    vacancy: dict,
    profile_name: str,
    token: str,
    *,
    include_feedback: bool = True,
    allow_ai_apply: bool = True,
) -> dict | None:
    # Compact: three rows instead of six, so a long review list stays readable.
    rows = []
    first = []
    callback_data = manual_apply_callback_data(profile_name, token)
    is_hh = (vacancy.get("source") or "hh") == "hh"
    if allow_ai_apply and is_hh and len(callback_data.encode("utf-8")) <= 64:
        first.append({"text": "✅ Откликнуться с ИИ", "callback_data": callback_data})
    url = (vacancy.get("url") or "").strip()
    if url:
        first.append({"text": "🔗 Открыть вакансию", "url": url})
    if first:
        rows.append(first)
    second = []
    why_data = manual_why_callback_data(profile_name, token)
    if len(why_data.encode("utf-8")) <= 64:
        second.append({"text": "❓ Почему", "callback_data": why_data})
    if include_feedback:
        for text, value in (("👍 Норм", "good"), ("👎 Мимо", "bad")):
            feedback_data = manual_feedback_callback_data(profile_name, token, value)
            if len(feedback_data.encode("utf-8")) <= 64:
                second.append({"text": text, "callback_data": feedback_data})
    if second:
        rows.append(second)
    third = []
    if include_feedback:
        snooze_data = manual_snooze_callback_data(profile_name, token)
        if len(snooze_data.encode("utf-8")) <= 64:
            third.append({"text": "⏰ Через сутки", "callback_data": snooze_data})
    block_company_data = manual_block_company_callback_data(profile_name, token)
    if vacancy.get("company") and len(block_company_data.encode("utf-8")) <= 64:
        third.append({"text": "🚫 Заблокировать компанию", "callback_data": block_company_data})
    if third:
        rows.append(third)
    return {"inline_keyboard": rows} if rows else None


def save_sent_cover(token: str, owner: str, cover: str, *, store=None) -> bool:
    """Save the exact dispatched cover under the current apply claim only."""
    saved = False
    def mutate(data):
        nonlocal saved
        item = data["items"].get(token, {})
        if item.get("status") == "applying" and item.get("owner") == owner:
            item["sent_cover"] = str(cover)
            saved = True
    (store or _store()).update(mutate)
    return saved


def build_review_card_markup(item: dict, profile_name: str) -> dict:
    token = item["token"]
    vacancy = item.get("vacancy") or {}
    def button(text, action):
        return {"text": text, "callback_data": f"rq:{action}:{profile_name}:{token}"}
    rows = []
    if item.get("status") == "uncertain":
        # Recovery choices only attest a manual check; they never send an apply.
        if item.get("orphan_recovered"):
            rows.extend(build_orphaned_apply_markup(item, profile_name)["inline_keyboard"])
        elif vacancy.get("url"):
            rows.append([{"text": "🔗 Проверить на hh.ru", "url": vacancy["url"]}])
        rows.extend([[button("◀", "h"), button("▶", "v")],
                     [{"text": "☰ Вся очередь", "callback_data": f"rq:l:{profile_name}:0"}]])
        return {"inline_keyboard": rows}
    if item.get("allow_ai_apply", True) and (vacancy.get("source") or "hh") == "hh":
        rows.append([{"text": "✅ Откликнуться", "callback_data": manual_apply_callback_data(profile_name, token)}])
    rows.extend([[button("◀", "h"), button("👍", "g"), button("👎", "b"), button("▶", "v")],
                 [button("❓ Почему", "w"), button("⋯ Ещё", "m")]])
    return {"inline_keyboard": [[b for b in row if len(b["callback_data"].encode()) <= 64]
                                for row in rows if row]}


def review_score_label(score) -> str:
    try:
        score = int(score)
    except (ValueError, TypeError):
        return "Оценка не определена"
    return ("Очень хорошо подходит" if score >= 85 else "Скорее подходит" if score >= 70 else
            "Есть совпадения" if score >= 50 else "Подходит частично — решать вам")


def build_review_item_text(item: dict, index="•") -> str:
    vacancy = item.get("vacancy") or {}
    evaluation = item.get("evaluation") or {}
    title = str(vacancy.get("title") or "Вакансия")
    company = str(vacancy.get("company") or "Компания не указана")
    source = str(vacancy.get("source_label") or vacancy.get("source") or "hh.ru")
    salary = str(vacancy.get("salary") or "").strip()
    if salary.casefold() in {"", "не указана", "не указан", "не указано", "зарплата не указана"}:
        salary = "Зарплата не указана"
    score = evaluation.get("score")
    lines = [f"{index}. {title}" if index != "•" else f"📌 {title}",
             f"{company} · {source}", f"💰 {salary}"]
    if score not in (None, ""):
        lines.append(f"{score}/100 · {review_score_label(score)}")
    if item.get("status") == "uncertain":
        lines.append("❔ " + (ORPHAN_APPLY_MESSAGE if item.get("orphan_recovered") else
                              "Нужна сверка: результат отклика не подтверждён."))
    reason = _shorten(evaluation.get("reason"), 380)
    if reason:
        lines.append(reason)
    return "\n".join(lines)


def build_review_card_text(item: dict, *, position=None, total=None) -> str:
    heading = "📋 Ждут решения"
    if position is not None and total is not None:
        heading += f" · {position} из {total}"
    text = heading + "\n\n" + build_review_item_text(item)
    if item.get("status") != "uncertain":
        text += "\n\nБез вашего «Откликнуться» ничего не отправится."
    return text
