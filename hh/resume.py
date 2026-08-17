"""Resume status and boost helpers for HH."""

from hh.text import normalize_text as _normalize_text


def _looks_like_resume_boost_action(value: str) -> bool:
    text = _normalize_text(value)
    if not text:
        return False
    if "поднять" in text:
        return "резюме" in text or len(text) <= 80
    return (
        "обновить дату" in text
        or "обновить резюме" in text
        or "обновить в поиске" in text
        or "поднять в поиске" in text
    )


def _looks_like_resume_boost_unavailable(value: str) -> bool:
    text = _normalize_text(value)
    return (
        "можно будет поднять" in text
        or "поднять можно" in text
        or "следующее поднятие" in text
        or "станет доступно" in text
        or "будет доступно" in text
        or "уже поднято" in text
    )


def _looks_like_resume_boost_success(value: str) -> bool:
    text = _normalize_text(value)
    return (
        "резюме поднято" in text
        or "резюме обновлено" in text
        or "поднято в поиске" in text
        or "обновлено в поиске" in text
    )


def _resume_matches_target(resume: dict, resume_id: str = "", resume_title: str = "") -> bool:
    target_id = str(resume_id or "").strip()
    target_title = _normalize_text(resume_title)
    current_id = str((resume or {}).get("id") or "").strip()
    current_title = _normalize_text(str((resume or {}).get("title") or ""))
    current_url = str((resume or {}).get("url") or "")
    return (
        bool(target_id and (target_id == current_id or target_id in current_url))
        or bool(target_title and (target_title in current_title or current_title in target_title))
    )
