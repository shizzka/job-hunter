"""Общие утилиты для работы с LLM-ответами.

Сейчас здесь:
- parse_llm_json: робастный парсер JSON-ответа, который чинит markdown-fence
  и thinking-токены вокруг JSON-объекта.
- extract_first_json_object: вспомогательный сбалансированный поиск {…}.
"""
from __future__ import annotations

import json


def extract_first_json_object(text: str) -> str | None:
    """Найти первый сбалансированный JSON-объект {…} в строке (учитывая строки и эскейпы)."""
    if not text:
        return None
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_str = False
    esc = False
    for i in range(start, len(text)):
        ch = text[i]
        if esc:
            esc = False
            continue
        if in_str:
            if ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start:i + 1]
    return None


def strip_markdown_fence(text: str) -> str:
    """Убрать ```...``` markdown-обёртку вокруг JSON если есть."""
    s = (text or "").strip()
    if s.startswith("```"):
        s = s.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
    return s


def parse_llm_json(text: str) -> dict:
    """Робастный JSON-парсер: чинит markdown-fence, thinking-префикс, постфикс.

    Бросает json.JSONDecodeError если ни прямой parse, ни extract не сработали.
    """
    raw = strip_markdown_fence(text)
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        extracted = extract_first_json_object(raw) or extract_first_json_object(text or "")
        if not extracted:
            raise
        return json.loads(extracted)

async def repair_llm_json(
    client,
    *,
    model: str,
    raw_text: str,
    parse_error: str = "",
    schema: str = "",
    max_tokens: int = 600,
) -> dict:
    """Попросить LLM преобразовать почти-JSON в валидный JSON и распарсить результат."""
    prompt = (
        "Преобразуй ответ модели в валидный JSON. "
        "Не добавляй markdown, пояснения или текст вне JSON.\n"
    )
    if schema:
        prompt += f"\nОжидаемая схема:\n{schema.strip()}\n"
    if parse_error:
        prompt += f"\nОшибка парсинга:\n{parse_error.strip()}\n"
    prompt += f"\nИсходный ответ:\n{(raw_text or '')[:4000]}"

    response = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "Ты исправляешь JSON. Ответь только валидным JSON-объектом."},
            {"role": "user", "content": prompt},
        ],
        temperature=0,
        max_tokens=max_tokens,
    )
    return parse_llm_json(response.choices[0].message.content or "")
