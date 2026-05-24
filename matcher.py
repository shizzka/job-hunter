"""LLM-оценка релевантности вакансии + генерация сопроводительного письма."""
import json
import logging
import os
import re
from openai import AsyncOpenAI

import config
import proxy_utils

log = logging.getLogger("matcher")

_client: AsyncOpenAI | None = None


def _get_client() -> AsyncOpenAI:
    global _client
    if _client is None:
        _client = AsyncOpenAI(
            base_url=config.LLM_BASE_URL,
            api_key=config.LLM_API_KEY or "no-key",
            http_client=proxy_utils.llm_http_client(),
        )
    return _client


def _load_resume() -> str:
    """Загрузить резюме из файла."""
    if os.path.exists(config.RESUME_FILE):
        with open(config.RESUME_FILE) as f:
            return f.read().strip()
    return f"(Резюме не найдено — заполни {config.RESUME_FILE})"


from llm_utils import parse_llm_json as _parse_llm_json  # re-export для обратной совместимости


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

Верни ТОЛЬКО валидный JSON, без markdown-обёртки, без префикса, без рассуждений до или после. Первым символом ответа должен быть `{{`, последним `}}`. Формат:
{{
    "score": <число 0-100>,
    "reason": "<1-2 предложения почему подходит/не подходит>",
    "should_apply": <true/false>,
    "red_flags": ["<красный флаг 1>", ...]
}}

Красные флаги: неадекватная зарплата, мошенники, MLM, требуют деньги от кандидата, вакансия-ловушка.
ЖЁСТКИЙ красный флаг (score=0, should_apply=false): всё связанное с войной, СВО, боевыми действиями, ВПК, оборонкой, военной службой, военными организациями."""

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
            model=config.HH_MATCHER_MODEL or config.LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=800,
        )
        text = resp.choices[0].message.content.strip()
        result = _parse_llm_json(text)
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
    from prompt_blocks import (
        build_profile_note_block,
        build_knowledge_base_block,
    )
    profile_note = build_profile_note_block()
    knowledge = build_knowledge_base_block(limit_chars=8000)  # cover letter — поджимаем

    prompt = f"""Ты — ассистент по поиску работы. Напиши короткое сопроводительное письмо.

{profile_note}{knowledge}## Резюме кандидата:
{resume}

## Вакансия:
Название: {vacancy.get('title', '—')}
Компания: {vacancy.get('company', '—')}

## Описание вакансии:
{details[:1500] if details else vacancy.get('snippet', '(нет описания)')}

## Длина и форма:
- 3-4 предложения, до 1500 символов
- Писать от первого лица, в разговорном профессиональном тоне (как сообщение HR-у в мессенджере, не сочинение)
- Использовать обычное тире "-", НЕ em-dash "—" и НЕ дефис между словами как разделитель
- Пиши обычные слова: "REST API", "тест-кейсы", не "REST‑API", не "тест‑кейсы" со спец-символом

## ЖЁСТКИЕ ОГРАНИЧЕНИЯ ПРОТИВ ВЫДУМЫВАНИЯ:
- Используй ТОЛЬКО факты из раздела "Резюме кандидата" выше.
- ЗАПРЕЩЕНО упоминать технологии, языки, фреймворки, инструменты, которых нет в резюме, даже если они в вакансии. Если в вакансии Java/Selenium/Kotlin, а в резюме их нет - НЕ ПИСАТЬ про них.
- ЗАПРЕЩЕНО завышать стаж. Бери срок строго из резюме.
- ЗАПРЕЩЕНО приписывать достижения, цифры, проекты, которых нет в резюме.

## АНТИ-AI ПРАВИЛА (НЕ писать как LLM):
НЕЛЬЗЯ начинать письмо с самопредставления "Я - QA Engineer", "Я QA с опытом", "Меня зовут". Начни с дела: что зацепило в вакансии, или с конкретного факта про себя, релевантного позиции.
НЕЛЬЗЯ использовать слова-паразиты: "активно", "регулярно", "успешно", "эффективно", "оперативно", "профессионально", "качественно", "ежедневно", "глубокий опыт", "обширный опыт".
НЕЛЬЗЯ использовать шаблонные обороты: "в текущем проекте я", "в текущем проекте", "на текущем проекте", "благодаря этому", "это позволяет мне", "имею опыт", "обладаю навыками", "в моём арсенале", "владею инструментами", "готов применить навыки", "внести вклад в команду", "с большим интересом", "буду рад", "имею удовольствие". Если хочешь сослаться на текущую работу - пиши "сейчас работаю над...", "у меня сейчас...", "щас на проекте...".
НЕЛЬЗЯ строить письмо по тройной схеме "опыт → перечень технологий → готовность".
НЕЛЬЗЯ перечислять подряд через запятую больше 3 инструментов - живой человек так не пишет.
НЕЛЬЗЯ заканчивать на "готов изучить", "готов работать", "буду полезен".

## Как пишет живой кандидат:
- Короткие предложения. Иногда совсем короткие.
- Конкретика вместо обобщений: не "тестирую web-приложения", а "сейчас гоняю чаты и тарифную систему".
- Цифры и конкретные имена инструментов из резюме - но 1-2 за всё письмо, не списком.
- Допустимы лёгкие неформальные обороты: "поковырял", "сижу на", "проверяю руками", "пишу авто на".
- Можно начать с реакции на вакансию: "Увидел у вас X - у меня было похожее на Y."

Пример хорошего стиля (НЕ копировать дословно, это шаблон стиля):
"Заметил вакансию - у вас как раз веб с авторизацией и личным кабинетом. У меня сейчас примерно такой же стек: чаты, тарифы, REST. Тест-кейсы веду в TestIT, баги в YouTrack, ручное + смотрю запросы в DevTools и Чарльзе. До QA 13 лет занимался диагностикой техники, привычка докапываться до причины осталась. Если интересно - готов созвониться."

Пример плохого стиля (ЗАПРЕЩЕНО так писать):
"Я - QA Engineer с более чем 13-летним опытом в технической диагностике и QA. В текущем проекте я активно поддерживаю более 360 тест-кейсов в TestIT, ежедневно тестирую функциональность web-приложения и проверяю REST-API через Postman, а также анализирую сетевые запросы. Благодаря этому я готов применить свои навыки в вашей команде."

Напиши ТОЛЬКО текст письма, без заголовков и пояснений."""

    try:
        client = _get_client()
        resp = await client.chat.completions.create(
            model=config.HH_COVER_LETTER_MODEL or config.LLM_MODEL,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=2000,
        )
        return resp.choices[0].message.content.strip()

    except Exception as e:
        log.error("Cover letter generation failed: %s", e)
        return ""
