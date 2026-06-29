"""Persistent queue for Telegram-confirmed AI vacancy applications."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
from datetime import datetime
from pathlib import Path

import config

CALLBACK_MANUAL_APPLY = "manual_apply"
CALLBACK_MANUAL_FEEDBACK = "manual_fb"
FEEDBACK_LABELS = {
    "good": "норм",
    "bad": "мимо",
}
MAX_ITEMS = 250
MAX_AGE_SECONDS = 7 * 24 * 60 * 60


def _queue_path() -> Path:
    fallback = os.path.join(config.JOB_HUNTER_HOME, "manual_apply_queue.json")
    return Path(getattr(config, "MANUAL_APPLY_QUEUE_FILE", fallback) or fallback)


def _read_queue() -> dict:
    path = _queue_path()
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


def _write_queue(data: dict) -> None:
    path = _queue_path()
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
    )
    compact = {key: vacancy.get(key) for key in keys if vacancy.get(key) not in (None, "")}
    if "source" not in compact:
        compact["source"] = "hh"
    if "source_label" not in compact:
        compact["source_label"] = compact.get("source", "hh")
    return compact


def _compact_evaluation(evaluation: dict) -> dict:
    keys = ("score", "reason", "should_apply", "red_flags", "guard_flags", "soft_flags", "error_kind")
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


def create_candidate(
    vacancy: dict,
    evaluation: dict,
    details: str = "",
    *,
    profile_name: str | None = None,
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
        "created_ts": now,
        "created_at": datetime.fromtimestamp(now).isoformat(timespec="seconds"),
        "vacancy": _compact_vacancy(vacancy),
        "evaluation": _compact_evaluation(evaluation),
        "details": (details or "")[:8000],
    }
    data = _read_queue()
    _prune(data, now)
    data.setdefault("items", {})[token] = item
    _write_queue(data)
    return item


def get_candidate(token: str) -> dict | None:
    token = (token or "").strip()
    if not token:
        return None
    item = _read_queue().get("items", {}).get(token)
    return item if isinstance(item, dict) else None


def mark_candidate(token: str, status: str, message: str = "") -> dict | None:
    data = _read_queue()
    item = data.get("items", {}).get((token or "").strip())
    if not isinstance(item, dict):
        return None
    item["status"] = status
    item["updated_at"] = datetime.now().isoformat(timespec="seconds")
    if message:
        item["message"] = str(message)[:1000]
    _write_queue(data)
    return item


def feedback_label(value: str) -> str:
    return FEEDBACK_LABELS.get((value or "").strip(), "")


def record_feedback(token: str, value: str, *, user_id: int = 0) -> dict | None:
    value = (value or "").strip()
    if value not in FEEDBACK_LABELS:
        return None
    data = _read_queue()
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
    _write_queue(data)
    return item


def manual_apply_callback_data(profile_name: str, token: str) -> str:
    return f"{CALLBACK_MANUAL_APPLY}:{_safe_profile(profile_name)}:{(token or '').strip()}"


def manual_feedback_callback_data(profile_name: str, token: str, value: str) -> str:
    return f"{CALLBACK_MANUAL_FEEDBACK}:{_safe_profile(profile_name)}:{(token or '').strip()}:{(value or '').strip()}"


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


def build_manual_apply_markup(
    vacancy: dict,
    profile_name: str,
    token: str,
    *,
    include_feedback: bool = True,
) -> dict | None:
    rows = []
    url = (vacancy.get("url") or "").strip()
    if url:
        rows.append([{"text": "Открою сам", "url": url}])
    callback_data = manual_apply_callback_data(profile_name, token)
    is_hh = (vacancy.get("source") or "hh") == "hh"
    if is_hh and len(callback_data.encode("utf-8")) <= 64:
        rows.append([{"text": "Откликнуться с ИИ", "callback_data": callback_data}])
    if include_feedback:
        feedback_buttons = []
        for text, value in (("Норм", "good"), ("Мимо", "bad")):
            feedback_data = manual_feedback_callback_data(profile_name, token, value)
            if len(feedback_data.encode("utf-8")) <= 64:
                feedback_buttons.append({"text": text, "callback_data": feedback_data})
        if feedback_buttons:
            rows.append(feedback_buttons)
    return {"inline_keyboard": rows} if rows else None
