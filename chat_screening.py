"""Heuristics for detecting HH AI recruiter and automated screening messages."""
from __future__ import annotations

import re
from typing import Any

# Известные аватарки системных ботов hh.ru (whitelist).
# Если найдётся новый бот — добавь сюда его URL.
AI_ASSISTANT_AVATAR_URLS = {
    "https://hhcdn.ru/file/18274603.png",  # «ИИ-помощник»
}
# Известные имена. Дополнительно детектятся AI-ish роли в alt/author
# и явная самопрезентация в тексте сообщения.
AI_NAMES = {
    "ИИ-помощник",
    "Робот-помощник",
    "Бот-помощник",
    "AI-рекрутер",
    "ИИ-рекрутер",
    "AI-ассистент",
    "ИИ-ассистент",
}

AI_RECRUITER_TEXT_PATTERNS = (
    re.compile(
        r"\bя\s+(?:ваш\s+)?(?:(?:виртуальн\w*|цифров\w*)\s+)?"
        r"(ассистент|помощник)\s+рекрутера\s+на\s+базе\s+(ai|ии)\b",
        re.I,
    ),
    re.compile(r"\bя\s+(?:ваш\s+)?(ai|ии)[-\s]*(ассистент|помощник|рекрутер)\b", re.I),
    re.compile(
        r"\bя\s+(?:ваш\s+)?(виртуальн\w*|цифров\w*)\s+"
        r"(ассистент|помощник)\s+рекрутера\b",
        re.I,
    ),
)

SUSPICIOUS_SCREENING_TEXT_PATTERNS = (
    re.compile(r"\bответьте\s+(?:пожалуйста,\s+)?на\s+(?:несколько\s+)?вопрос", re.I),
    re.compile(r"\b(?:пройти|заполнить)\s+(?:небольш\w+\s+)?(?:опрос|анкет)", re.I),
    re.compile(r"\b(?:для|чтобы)\s+(?:продолжить|мы\s+могли\s+продолжить|работодатель\s+узнал)", re.I),
    re.compile(r"\b(?:готов[ы]?|рассматриваете|сможе(?:те|шь))\s+ли\s+(?:вы|ты)\b", re.I),
    re.compile(r"\b(?:какие|какой)\s+у\s+(?:вас|тебя)\s+зарплатн\w+\s+ожидани", re.I),
    re.compile(r"\b(?:какой|какие)\s+у\s+(?:вас|тебя)\s+уровень\b", re.I),
    re.compile(r"\b(?:есть|имеется)\s+ли\s+у\s+(?:вас|тебя)\s+опыт\b", re.I),
    re.compile(r"\b(?:подскажите|подскажи|уточните|уточни|расскажите|расскажи|напишите|напиши),?\s+пожалуйста\b", re.I),
    re.compile(r"\b(?:почему|чем)\s+(?:вам\s+)?(?:интересн|заинтересовал)", re.I),
)

SUSPICIOUS_SCREENING_KEYWORDS = (
    "зарплат",
    "ожидан",
    "опыт",
    "тестирован",
    "api",
    "postman",
    "sql",
    "автотест",
    "образован",
    "обучени",
    "учебн",
    "учёб",
    "стажировк",
    "справк",
    "pet-проект",
    "пет-проект",
    "хакатон",
    "английск",
    "удален",
    "удалён",
    "офис",
    "график",
    "приступить",
    "релокац",
)

def normalize_ai_marker_text(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").casefold().replace("ё", "е")).strip()


def looks_like_ai_label(label: str) -> bool:
    label = (label or "").strip()
    if not label:
        return False
    if label in AI_NAMES:
        return True

    text = normalize_ai_marker_text(label)
    has_ai_marker = bool(
        re.search(r"(?<![a-zа-я0-9])(ии|ai|робот|бот)(?![a-zа-я0-9])", text, re.I)
    )
    has_ai_role = any(token in text for token in ("помощник", "ассистент", "рекрутер"))
    return has_ai_marker and has_ai_role


def looks_like_ai_recruiter_text(text: str) -> bool:
    normalized = normalize_ai_marker_text(text)
    if not normalized:
        return False
    return any(pattern.search(normalized) for pattern in AI_RECRUITER_TEXT_PATTERNS)


def looks_like_suspicious_screening_text(text: str) -> bool:
    """Human-labeled HR messages that look like automated screening.

    This intentionally does not mark the message as AI. It only enables a
    Telegram approval lane, because live HRs can ask the same questions.
    """
    normalized = normalize_ai_marker_text(text)
    if len(normalized) < 35:
        return False
    if looks_like_ai_recruiter_text(normalized):
        return False
    has_question_shape = (
        "?" in normalized
        or any(
            starter in normalized
            for starter in ("ответьте", "подскажите", "уточните", "расскажите", "напишите")
        )
    )
    if not has_question_shape:
        return False
    if any(pattern.search(normalized) for pattern in SUSPICIOUS_SCREENING_TEXT_PATTERNS):
        return True
    hits = sum(1 for token in SUSPICIOUS_SCREENING_KEYWORDS if token in normalized)
    return hits >= 2

def classify_message_author(message: dict[str, Any]) -> dict[str, Any]:
    """Mark AI/system messages while keeping own messages out of text heuristics."""
    avatar_src = (message.get("avatar_src") or "").split("?", 1)[0]
    message["avatar_src"] = avatar_src
    is_me = bool(message.get("is_me"))
    is_ai = (
        not is_me
        and (
            avatar_src in AI_ASSISTANT_AVATAR_URLS
            or looks_like_ai_label(message.get("avatar_alt") or "")
            or looks_like_ai_label(message.get("author") or "")
            or looks_like_ai_recruiter_text(message.get("text") or "")
        )
    )
    message["is_ai"] = is_ai
    message["is_ai_suspect"] = (
        not is_me
        and not is_ai
        and looks_like_suspicious_screening_text(message.get("text") or "")
    )
    message["is_other"] = not is_ai and not is_me
    return message

__all__ = [
    "AI_ASSISTANT_AVATAR_URLS",
    "AI_NAMES",
    "AI_RECRUITER_TEXT_PATTERNS",
    "SUSPICIOUS_SCREENING_TEXT_PATTERNS",
    "SUSPICIOUS_SCREENING_KEYWORDS",
    "normalize_ai_marker_text",
    "looks_like_ai_label",
    "looks_like_ai_recruiter_text",
    "looks_like_suspicious_screening_text",
    "classify_message_author",
]
