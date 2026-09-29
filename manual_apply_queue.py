"""Persistent queue for Telegram-confirmed AI vacancy applications."""
from __future__ import annotations

import hashlib
import fcntl
from functools import wraps
import html
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

import config
import company_blacklist

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



def _queue_transaction(function):
    @wraps(function)
    def locked(*args, **kwargs):
        path = _queue_path(kwargs.get("profile_name"))
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_suffix(".lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            return function(*args, **kwargs)
    return locked


def _read_queue(profile_name: str | None = None) -> dict:
    path = _queue_path(profile_name)
    if not path.exists():
        return {"items": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"items": {}}
    if not isinstance(data, dict):
        return {"items": {}}
    items = data.get("items")
    if not isinstance(items, dict):
        data["items"] = {}
    return data


def _write_queue(data: dict, profile_name: str | None = None) -> None:
    path = _queue_path(profile_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(path.suffix + ".tmp")
    tmp_path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(tmp_path, path)


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
        if created and now - created > MAX_AGE_SECONDS:
            stale.append(token)
    for token in stale:
        items.pop(token, None)

    if len(items) <= MAX_ITEMS:
        return
    ordered = sorted(items.items(), key=lambda pair: float((pair[1] or {}).get("created_ts") or 0), reverse=True)
    data["items"] = dict(ordered[:MAX_ITEMS])


@_queue_transaction
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
    data = _read_queue(profile_name)
    _prune(data, now)
    data.setdefault("items", {})[token] = item
    _write_queue(data, profile_name)
    return item


def get_candidate(token: str, *, profile_name: str | None = None) -> dict | None:
    token = (token or "").strip()
    if not token:
        return None
    item = _read_queue(profile_name).get("items", {}).get(token)
    return item if isinstance(item, dict) else None


@_queue_transaction
def mark_candidate(token: str, status: str, message: str = "", *, profile_name: str | None = None) -> dict | None:
    data = _read_queue(profile_name)
    item = data.get("items", {}).get((token or "").strip())
    if not isinstance(item, dict):
        return None
    item["status"] = status
    item["updated_at"] = datetime.now().isoformat(timespec="seconds")
    if message:
        item["message"] = str(message)[:1000]
    _write_queue(data, profile_name)
    return item


def feedback_label(value: str) -> str:
    return FEEDBACK_LABELS.get((value or "").strip(), "")


@_queue_transaction
def record_feedback(token: str, value: str, *, user_id: int = 0, profile_name: str | None = None) -> dict | None:
    value = (value or "").strip()
    if value not in FEEDBACK_LABELS:
        return None
    data = _read_queue(profile_name)
    item = data.get("items", {}).get((token or "").strip())
    if not isinstance(item, dict):
        return None
    item["feedback"] = value
    item["feedback_label"] = feedback_label(value)
    item["feedback_at"] = datetime.now().isoformat(timespec="seconds")
    if user_id:
        item["feedback_user_id"] = int(user_id)
    if value == "bad" and item.get("status") == "pending":
        item["status"] = "dismissed"
    item["updated_at"] = item["feedback_at"]
    _write_queue(data, profile_name)
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


@_queue_transaction
def snooze_candidate(token: str, *, profile_name: str, hours: int = 24) -> dict | None:
    data = _read_queue(profile_name)
    item = data.get("items", {}).get((token or "").strip())
    if not isinstance(item, dict) or item.get("status") != "pending":
        return None
    until = time.time() + max(1, int(hours)) * 3600
    item["snoozed_until"] = until
    item["snoozed_until_at"] = datetime.fromtimestamp(until).isoformat(timespec="seconds")
    item["updated_at"] = datetime.now().isoformat(timespec="seconds")
    _write_queue(data, profile_name)
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
