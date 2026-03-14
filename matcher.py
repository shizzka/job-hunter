"""LLM-оценка релевантности вакансии + генерация сопроводительного письма."""
import json
import logging
import os
import re
from openai import AsyncOpenAI

import config

log = logging.getLogger("matcher")

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            base_url=config.LLM_BASE_URL,
            api_key=config.LLM_API_KEY or "no-key",
        )
    return _client


def _load_resume() -> str:
    """Загрузить резюме из файла."""
    if os.path.exists(config.RESUME_FILE):
        with open(config.RESUME_FILE) as f:
            return f.read().strip()
    return f"(Резюме не найдено — заполни {config.RESUME_FILE})"


ONE_YEAR_EXPERIENCE_PATTERNS = [
    r"\b1\s*\+?\s*year\b",
    r"\b1\s*\+?\s*years\b",
    r"\bone year\b",
    r"\bjunior\b",
    r"\bmiddle\b",
    r"\bmid-level\b",
    r"\bmid level\b",
    r"\bопыт\s+от\s+1\s+года\b",
    r"\bопыт\s+работы\s+от\s+1\s+года\b",
    r"\bот\s+1\s+года\b",
    r"\b1\s*[-–]\s*3\s*года\b",
    r"\b1\s*[-–]\s*3\s*years\b",
    r"\bдо\s+1\s+года\b",
    r"\bмидл\b",
    r"\bмиддл\b",
    r"\bmiddle\s+qa\b",
    r"\bqa\s+middle\b",
]

SENIOR_EXPERIENCE_PATTERNS = [
    r"\bsenior\b",
    r"\blead\b",
    r"\bprincipal\b",
    r"\bstaff\b",
    r"\bстарш(ий|ая)\b",
    r"\bведущ(ий|ая)\b",
    r"\bsenior\s+qa\b",
    r"\bqa\s+senior\b",
]


def _is_one_year_experience_vacancy(vacancy: dict, details: str = "") -> bool:
    haystack = " ".join(
        part
        for part in (
            vacancy.get("title", ""),
            vacancy.get("snippet", ""),
            details or "",
        )
        if part
    ).lower()
    return any(re.search(pattern, haystack) for pattern in ONE_YEAR_EXPERIENCE_PATTERNS)


def _is_senior_experience_vacancy(vacancy: dict, details: str = "") -> bool:
    haystack = " ".join(
        part
        for part in (
            vacancy.get("title", ""),
            vacancy.get("snippet", ""),
            details or "",
        )
        if part
    ).lower()
    return any(re.search(pattern, haystack) for pattern in SENIOR_EXPERIENCE_PATTERNS)


async def evaluate_vacancy(vacancy: dict, details: str = "") -> dict:
    """
    Оценить вакансию на релевантность.

    Возвращает:
    {
        "score": 0-100,       # оценка релевантности
        "reason": "...",      # почему подходит/не подходит
        "should_apply": bool, # рекомендация
        "red_flags": [...]    # красные флаги если есть
    }
    """
    resume = _load_resume()

    allow_one_year_override = _is_one_year_experience_vacancy(vacancy, details)
    force_senior_reject = _is_senior_experience_vacancy(vacancy, details)

    prompt = f"""Ты — ассистент по поиску работы. Оцени подходит ли вакансия для кандидата.

## Резюме кандидата:
{resume}

## Вакансия:
Название: {vacancy.get('title', '—')}
Компания: {vacancy.get('company', '—')}
Зарплата: {vacancy.get('salary', 'не указана')}
Краткое описание: {vacancy.get('snippet', '—')}

## Полное описание вакансии:
{details[:2000] if details else '(нет деталей)'}

## Задача:
Оцени вакансию по шкале 0-100, где:
- 80-100: отличное совпадение, точно откликаться
- 60-79: хорошее совпадение, стоит откликнуться
- 40-59: среднее совпадение, можно попробовать
- 0-39: не подходит

Верни ТОЛЬКО JSON (без markdown):
{{
    "score": <число 0-100>,
    "reason": "<1-2 предложения почему подходит/не подходит>",
    "should_apply": <true/false>,
    "red_flags": ["<красный флаг 1>", ...]
}}

Красные флаги: неадекватная зарплата, мошенники, MLM, требуют деньги от кандидата, вакансия-ловушка."""

    if allow_one_year_override:
        prompt += """

Дополнительное правило:
- Требование опыта до 1 года, junior/middle-уровень или диапазон 1-3 года НЕ считать причиной для отказа само по себе.
- Если вакансия в целом QA/тестовая и выглядит адекватной, всё равно рекомендуй отклик."""

    if force_senior_reject:
        prompt += """

Дополнительное правило:
- Senior / Lead / Principal-уровень считать несоответствием профилю кандидата.
- Такие вакансии не рекомендовать к отклику, если только текст явно не противоречит senior-метке."""

    try:
        client = _get_client()
        resp = await client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=500,
        )
        text = resp.choices[0].message.content.strip()

        # Пробуем распарсить JSON
        # Убираем markdown обёртку если есть
        if text.startswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0]

        result = json.loads(text)
        result.setdefault("score", 50)
        result.setdefault("reason", "")
        result["should_apply"] = bool(result.get("should_apply", result["score"] >= 50))
        if result["score"] < 50:
            result["should_apply"] = False
        result.setdefault("red_flags", [])
        if (
            allow_one_year_override
            and not result["red_flags"]
            and result["score"] >= 40
        ):
            result["score"] = max(result["score"], 50)
            result["should_apply"] = True
            reason = result.get("reason", "").strip()
            note = "Не режем вакансию только из-за требования опыта до 1 года."
            result["reason"] = f"{reason} {note}".strip()
        if force_senior_reject:
            result["score"] = min(result.get("score", 0), 39)
            result["should_apply"] = False
            reason = result.get("reason", "").strip()
            note = "Senior/Lead-уровень считаем слишком высоким для текущего профиля."
            result["reason"] = f"{reason} {note}".strip()
        return result

    except Exception as e:
        log.error("LLM evaluation failed: %s", e)
        return {
            "score": 0,
            "reason": f"LLM ошибка: {e}",
            "should_apply": False,  # при ошибке LLM — не откликаться вслепую
            "red_flags": [],
        }


async def generate_cover_letter(vacancy: dict, details: str = "") -> str:
    """Сгенерировать сопроводительное письмо для вакансии."""
    resume = _load_resume()

    prompt = f"""Ты — ассистент по поиску работы. Напиши короткое сопроводительное письмо.

## Резюме кандидата:
{resume}

## Вакансия:
Название: {vacancy.get('title', '—')}
Компания: {vacancy.get('company', '—')}

## Описание вакансии:
{details[:1500] if details else vacancy.get('snippet', '(нет описания)')}

## Правила:
- Максимум 3-4 предложения, уложись в 1500 символов
- Конкретно: что умею и как это совпадает с вакансией
- Без шаблонных фраз типа "с большим интересом ознакомился"
- Тон: профессиональный, но живой
- Не врать и не преувеличивать
- Писать от первого лица

Напиши ТОЛЬКО текст письма, без заголовков и пояснений."""

    try:
        client = _get_client()
        resp = await client.chat.completions.create(
            model=config.LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.7,
            max_tokens=300,
        )
        return resp.choices[0].message.content.strip()

    except Exception as e:
        log.error("Cover letter generation failed: %s", e)
        return ""
