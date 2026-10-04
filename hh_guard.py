"""Persistent HH anti-bot guard and rolling auto-apply limits."""
import json
import logging
import os
from datetime import datetime, timedelta

import config
from outcome import DECISION_APPLIED_AUTO
from state_store.protected import ProtectedJsonStore

log = logging.getLogger("hh_guard")


def _now() -> datetime:
    return datetime.now().astimezone()


def _parse_datetime(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=_now().tzinfo)
    return parsed


def _format_datetime(value: datetime) -> str:
    return value.astimezone().isoformat(timespec="seconds")


def _default_state() -> dict:
    return {
        "successful_apply_timestamps": [],
        "blocked_until": "",
        "last_kind": "",
        "last_reason": "",
        "last_stage": "",
        "last_detected_at": "",
        "seeded_from_analytics_at": "",
    }


def _corrupt_state(now: datetime | None = None) -> dict:
    now = now or _now()
    state = _default_state()
    state["blocked_until"] = _format_datetime(
        now + timedelta(hours=max(1, int(config.HH_ANTI_BOT_COOLDOWN_HOURS)))
    )
    state["last_kind"] = "state_corruption"
    state["last_reason"] = "HH guard state was corrupt; auto-apply paused as a precaution"
    state["last_stage"] = "state_load"
    state["last_detected_at"] = _format_datetime(now)
    return state


def _valid_state(state: dict) -> bool:
    timestamps = state.get("successful_apply_timestamps", [])
    if not isinstance(timestamps, list) or any(
        not isinstance(item, str) or not item or _parse_datetime(item) is None
        for item in timestamps
    ):
        return False
    for key in ("blocked_until", "last_detected_at", "seeded_from_analytics_at"):
        value = state.get(key, "")
        if not isinstance(value, str) or (value and _parse_datetime(value) is None):
            return False
    return all(isinstance(state.get(key, ""), str) for key in ("last_kind", "last_reason", "last_stage"))


def _store(path: str | None = None, *, now: datetime | None = None) -> ProtectedJsonStore:
    return ProtectedJsonStore(
        path or config.HH_GUARD_STATE_FILE,
        default_factory=_default_state,
        validator=_valid_state,
        logger=log,
        read_error_message="HH guard state read failed",
    )


def _normalize_state(state: dict, now: datetime | None = None) -> dict:
    now = now or _now()
    cutoff = now - timedelta(hours=24)
    normalized = _default_state()

    timestamps = []
    for item in list(state.get("successful_apply_timestamps", []) or []):
        parsed = _parse_datetime(str(item))
        if parsed and parsed >= cutoff:
            timestamps.append(_format_datetime(parsed))
    timestamps.sort()
    normalized["successful_apply_timestamps"] = timestamps

    blocked_until = _parse_datetime(str(state.get("blocked_until", "") or ""))
    if blocked_until and blocked_until > now:
        normalized["blocked_until"] = _format_datetime(blocked_until)

    for key in ("last_kind", "last_reason", "last_stage", "last_detected_at", "seeded_from_analytics_at"):
        value = str(state.get(key, "") or "").strip()
        if value:
            normalized[key] = value

    return normalized


def _seed_apply_timestamps_from_analytics(now: datetime | None = None) -> list[str]:
    now = now or _now()
    cutoff = now - timedelta(hours=24)
    path = config.ANALYTICS_EVENTS_FILE
    timestamps = []
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(payload, dict):
                    continue
                if payload.get("event") != "decision":
                    continue
                if payload.get("source") != "hh":
                    continue
                if payload.get("decision") != DECISION_APPLIED_AUTO:
                    continue
                created_at = _parse_datetime(str(payload.get("created_at", "") or ""))
                if created_at and created_at >= cutoff:
                    timestamps.append(_format_datetime(created_at))
    except FileNotFoundError:
        return []
    except Exception as exc:
        log.warning("Failed to seed HH guard from analytics %s: %s", path, exc)
        raise

    timestamps.sort()
    return timestamps


def _load_state(now: datetime | None = None) -> dict:
    now = now or _now()
    path = config.HH_GUARD_STATE_FILE
    try:
        state = _store(path, now=now).load()
    except Exception as exc:
        log.warning("HH guard state unavailable: %s", type(exc).__name__)
        # Keep damaged bytes: every future attempt remains blocked until repair.
        return _corrupt_state(now)

    normalized = _normalize_state(state, now=now)
    if not normalized["successful_apply_timestamps"] and not normalized["seeded_from_analytics_at"]:
        # Re-read and seed under one lock, preserving any concurrent cooldown/apply.
        return _update_state(lambda state: None, now=now)
    return normalized


def _update_state(mutator, *, now: datetime) -> dict:
    path = config.HH_GUARD_STATE_FILE

    def update(raw_state: dict) -> dict:
        state = _normalize_state(raw_state, now=now)
        if not state["successful_apply_timestamps"] and not state["seeded_from_analytics_at"]:
            seeded = _seed_apply_timestamps_from_analytics(now=now)
            state["successful_apply_timestamps"] = seeded
            # A successful empty scan (including a missing log) is still complete.
            # Failed reads raise instead: never cache an unverified zero count.
            state["seeded_from_analytics_at"] = _format_datetime(now)
        mutator(state)
        return _normalize_state(state, now=now)

    try:
        return _store(path, now=now).update(update)
    except Exception as exc:
        log.warning("Failed to update HH guard state %s: %s", path, type(exc).__name__)
        return _corrupt_state(now)


def detect_antibot_kind(value: str) -> str:
    text = " ".join((value or "").casefold().split())
    if not text:
        return ""
    if (
        "ddos-guard" in text
        or "проверка браузера перед переходом на hh.ru" in text
        or "не удалось проверить ваш браузер автоматически" in text
        or "checking your browser before accessing" in text
    ):
        return "ddos_guard"
    if (
        "rate limit" in text
        or "слишком много откликов" in text
        or "too many requests" in text
        or "too many applications" in text
    ):
        return "rate_limit"
    if (
        "captcha" in text
        or "капча" in text
        or "подтвердите, что вы не робот" in text
        or "текст с картинки" in text
        or "i'm not a robot" in text
        or "verify you are human" in text
    ):
        return "captcha"
    if (
        "проверка браузера" in text
        or "checking your browser" in text
        or "verify your browser" in text
    ):
        return "browser_check"
    return ""


def looks_like_antibot_text(value: str) -> bool:
    return bool(detect_antibot_kind(value))


def describe_antibot_kind(kind: str) -> str:
    mapping = {
        "captcha": "captcha",
        "ddos_guard": "DDOS-GUARD",
        "browser_check": "проверка браузера",
        "rate_limit": "rate limit",
        "state_corruption": "state corruption",
    }
    return mapping.get((kind or "").strip(), "anti-bot")


def _format_until(value: str) -> str:
    parsed = _parse_datetime(value)
    if not parsed:
        return ""
    return parsed.astimezone().strftime("%d.%m %H:%M")


def get_status(now: datetime | None = None) -> dict:
    now = now or _now()
    state = _load_state(now=now)
    cutoff = now - timedelta(hours=24)
    rolling_count = 0
    for item in state.get("successful_apply_timestamps", []):
        parsed = _parse_datetime(item)
        if parsed and parsed >= cutoff:
            rolling_count += 1

    blocked_until = _parse_datetime(state.get("blocked_until", ""))
    blocked = bool(blocked_until and blocked_until > now)
    limit = max(0, int(config.HH_AUTO_APPLY_MAX_PER_24H))
    status = dict(state)
    status.update(
        {
            "rolling_apply_count_24h": rolling_count,
            "auto_apply_limit_24h": limit,
            "limit_reached": bool(limit and rolling_count >= limit),
            "blocked": blocked,
            "blocked_until_label": _format_until(state.get("blocked_until", "")),
        }
    )
    return status


def format_block_note(status: dict) -> str:
    reason = describe_antibot_kind(status.get("last_kind", ""))
    until_label = status.get("blocked_until_label", "")
    if until_label:
        return f"HH автоотклики на паузе до {until_label} после антибота ({reason})."
    return f"HH автоотклики временно на паузе после антибота ({reason})."


def format_limit_note(status: dict) -> str:
    count = int(status.get("rolling_apply_count_24h", 0) or 0)
    limit = int(status.get("auto_apply_limit_24h", 0) or 0)
    return f"Достигнут HH-лимит автооткликов: {count}/{limit} за последние 24 часа."


def can_collect(now: datetime | None = None) -> tuple[bool, str]:
    status = get_status(now=now)
    if config.HH_SKIP_SEARCH_ON_ANTI_BOT and status.get("blocked"):
        return False, format_block_note(status)
    return True, ""


def can_auto_apply(now: datetime | None = None) -> tuple[bool, str]:
    status = get_status(now=now)
    if status.get("blocked"):
        return False, format_block_note(status)
    if status.get("limit_reached"):
        return False, format_limit_note(status)
    return True, ""


def record_apply_success(now: datetime | None = None) -> dict:
    now = now or _now()

    def remember(state: dict) -> None:
        timestamps = list(state.get("successful_apply_timestamps", []) or [])
        timestamps.append(_format_datetime(now))
        state["successful_apply_timestamps"] = timestamps

    _update_state(remember, now=now)
    return get_status(now=now)


def record_antibot(
    *,
    kind: str = "",
    raw_message: str = "",
    stage: str = "",
    now: datetime | None = None,
) -> dict:
    now = now or _now()
    detected_kind = kind or detect_antibot_kind(raw_message)
    cooldown_hours = max(1, int(config.HH_ANTI_BOT_COOLDOWN_HOURS))

    def remember(state: dict) -> None:
        state["blocked_until"] = _format_datetime(now + timedelta(hours=cooldown_hours))
        state["last_kind"] = detected_kind
        state["last_reason"] = (raw_message or describe_antibot_kind(detected_kind)).strip()
        state["last_stage"] = (stage or "").strip()
        state["last_detected_at"] = _format_datetime(now)

    _update_state(remember, now=now)
    return get_status(now=now)


def record_soft_cooldown(*, minutes: int = 15, reason: str = "captcha_human_timeout", now: datetime | None = None) -> dict:
    """Короткий cooldown (минуты) для случая 'captcha ждёт человека, но timeout вышел'.
    В отличие от record_antibot не использует HH_ANTI_BOT_COOLDOWN_HOURS (6h)."""
    now = now or _now()
    def remember(state: dict) -> None:
        state["blocked_until"] = _format_datetime(now + timedelta(minutes=max(1, minutes)))
        state["last_kind"] = "captcha"
        state["last_reason"] = reason
        state["last_stage"] = "human_timeout"
        state["last_detected_at"] = _format_datetime(now)

    _update_state(remember, now=now)
    return get_status(now=now)


def clear_cooldown(*, now: datetime | None = None) -> dict:
    """Снять blocked_until (для ручного перезапуска из TG-кнопки)."""
    now = now or _now()
    def clear(state: dict) -> None:
        state["blocked_until"] = ""
        state["last_stage"] = (state.get("last_stage") or "") + "+manual_clear"

    _update_state(clear, now=now)
    return get_status(now=now)


def clear_cooldown_for_profile(profile_name: str, *, now: datetime | None = None) -> dict:
    """Clear only the HH cooldown belonging to one named profile."""
    import profile as profile_mod

    now = now or _now()
    profile = profile_mod.load_profile(profile_name)
    path = os.path.join(profile.home_dir, "hh_guard_state.json")
    try:
        def clear(state: dict) -> dict:
            state["blocked_until"] = ""
            state["last_stage"] = (state.get("last_stage") or "") + "+manual_clear"
            return _normalize_state(state, now=now)

        return _store(path, now=now).update(clear)
    except Exception as exc:
        log.warning("Failed to save HH guard state %s: %s", path, exc)
        return _corrupt_state(now)
