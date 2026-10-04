"""Persistent queue for Telegram-confirmed AI vacancy applications."""
from __future__ import annotations

import hashlib
import html
import os
import re
import time
import uuid
from datetime import datetime
from pathlib import Path

import config
import company_blacklist
from state_store.protected import ProtectedJsonStore

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
        "_resume_input_versions",
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
    item = _read_queue(profile_name).get("items", {}).get(token)
    return item if isinstance(item, dict) else None


def mark_candidate(token: str, status: str, message: str = "", *, profile_name: str | None = None) -> dict | None:
    item = None

    def mutate(data):
        nonlocal item
        item = data["items"].get((token or "").strip())
        if item is not None:
            if item.get("status") == "applying":
                item["revoked"] = True
                return
            if item.get("external_started") or item.get("status") == "uncertain":
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
        item = data["items"].get(token)
        if (not item or item.get("status") != "pending" or not item.get("allow_ai_apply", True)
                or item.get("feedback") == "bad"):
            return
        item.update(status="applying", owner=uuid.uuid4().hex, external_started=False)
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


def finish_candidate(token, owner, status, message="", *, store=None) -> bool:
    changed = False
    def mutate(data):
        nonlocal changed
        item = data["items"].get(token, {})
        if item.get("owner") != owner or item.get("status") != "applying":
            return
        if item.get("external_started"):
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


def feedback_label(value: str) -> str:
    return FEEDBACK_LABELS.get((value or "").strip(), "")


def record_feedback(token: str, value: str, *, user_id: int = 0, profile_name: str | None = None) -> dict | None:
    value = (value or "").strip()
    if value not in FEEDBACK_LABELS:
        return None
    item = None

    def mutate(data):
        nonlocal item
        item = data["items"].get((token or "").strip())
        if item is None:
            return
        item["feedback"] = value
        item["feedback_label"] = feedback_label(value)
        item["feedback_at"] = datetime.now().isoformat(timespec="seconds")
        if user_id:
            item["feedback_user_id"] = int(user_id)
        if value == "bad" and item.get("status") == "applying":
            item["revoked"] = True
        if value == "bad" and item.get("status") == "pending":
            item["status"] = "dismissed"
        item["updated_at"] = item["feedback_at"]

    _store(profile_name).update(mutate)
    return item


def list_candidates(profile_name: str, *, limit: int = 8, include_snoozed: bool = False) -> list[dict]:
    """Pending decisions for one profile, newest first."""
    now = time.time()
    data = _read_queue(profile_name)
    _prune(data, now)
    items = []
    for item in data.get("items", {}).values():
        if not isinstance(item, dict) or item.get("status") != "pending":
            continue
        if _safe_profile(item.get("profile_name")) != _safe_profile(profile_name):
            continue
        snoozed_until = float(item.get("snoozed_until") or 0)
        if not include_snoozed and snoozed_until > now:
            continue
        if company_blacklist.is_blocked((item.get("vacancy") or {}).get("company", ""), profile_name):
            continue
        items.append(item)
    items.sort(key=lambda item: float(item.get("created_ts") or 0), reverse=True)
    return items[:max(1, int(limit or 1))]


def snooze_candidate(token: str, *, profile_name: str, hours: int = 24) -> dict | None:
    item = None

    def mutate(data):
        nonlocal item
        current = data["items"].get((token or "").strip())
        if current is None or current.get("status") != "pending":
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
        "<b>Почему ручное решение</b>",
        f"{html.escape(_shorten(title, 100))} @ {html.escape(_shorten(company, 80))}",
    ]

    source = vacancy.get("source_label") or vacancy.get("source")
    if source:
        lines.append(f"Источник: {html.escape(str(source))}")

    score = evaluation.get("score")
    response_score = evaluation.get("response_probability_score")
    score_parts = []
    if score not in (None, ""):
        score_parts.append(f"match {score}/100")
    if response_score not in (None, ""):
        score_parts.append(f"response {response_score}/100")
    if score_parts:
        lines.append("Score: " + html.escape(" | ".join(score_parts)))

    for label, key in (
        ("Кластер", "cluster"),
        ("Резюме", "resume_variant"),
        ("Сопровод", "cover_style"),
    ):
        value = evaluation.get(key)
        if value:
            lines.append(f"{label}: {html.escape(_shorten(value, 80))}")

    for label, key in (
        ("Красные флаги", "red_flags"),
        ("Guard flags", "guard_flags"),
        ("Мягкие флаги", "soft_flags"),
    ):
        value = _format_list_value(evaluation.get(key))
        if value:
            lines.append(f"{label}: {html.escape(_shorten(value, 220))}")

    reason = evaluation.get("reason") or item.get("details") or ""
    if reason:
        lines.extend(["", html.escape(_shorten(reason, 1200))])

    url = vacancy.get("url")
    if url:
        lines.extend(["", html.escape(str(url))])

    return "\n".join(lines)


def build_manual_apply_markup(
    vacancy: dict,
    profile_name: str,
    token: str,
    *,
    include_feedback: bool = True,
    allow_ai_apply: bool = True,
) -> dict | None:
    rows = []
    url = (vacancy.get("url") or "").strip()
    if url:
        rows.append([{"text": "Открою сам", "url": url}])
    callback_data = manual_apply_callback_data(profile_name, token)
    is_hh = (vacancy.get("source") or "hh") == "hh"
    if allow_ai_apply and is_hh and len(callback_data.encode("utf-8")) <= 64:
        rows.append([{"text": "Откликнуться с ИИ", "callback_data": callback_data}])
    why_data = manual_why_callback_data(profile_name, token)
    if len(why_data.encode("utf-8")) <= 64:
        rows.append([{"text": "Почему?", "callback_data": why_data}])
    if include_feedback:
        feedback_buttons = []
        for text, value in (("Норм", "good"), ("Мимо", "bad")):
            feedback_data = manual_feedback_callback_data(profile_name, token, value)
            if len(feedback_data.encode("utf-8")) <= 64:
                feedback_buttons.append({"text": text, "callback_data": feedback_data})
        if feedback_buttons:
            rows.append(feedback_buttons)
        snooze_data = manual_snooze_callback_data(profile_name, token)
        if len(snooze_data.encode("utf-8")) <= 64:
            rows.append([{"text": "⏰ Через сутки", "callback_data": snooze_data}])
    block_company_data = manual_block_company_callback_data(profile_name, token)
    if vacancy.get("company") and len(block_company_data.encode("utf-8")) <= 64:
        rows.append([{"text": "🚫 В чёрный список", "callback_data": block_company_data}])
    return {"inline_keyboard": rows} if rows else None
