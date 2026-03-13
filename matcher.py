"""LLM-оценка релевантности вакансии + генерация сопроводительного письма."""
import json
import logging
import os
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
