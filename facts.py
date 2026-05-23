"""Структурированные факты кандидата (facts.json) — для авто-ответов на анкеты.

Один раз генерируется LLM из resume.md (`./run.sh extract-facts`), потом подкладывается
в LLM-промпт ПЕРЕД резюме. LLM сначала смотрит сюда (точный lookup), потом дотягивает
свободный контекст из резюме.

Поля — гибкие, но есть рекомендованный набор (location, willing_remote,
willing_business_trips, english_level, tools_used, и т.п.). LLM сам решает структуру
при extract — главное чтобы итог парсился как JSON.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from typing import Any

from openai import AsyncOpenAI

import config
import proxy_utils

log = logging.getLogger("facts")


# ---------- путь к файлу ----------

def facts_file_path() -> str:
    """Путь к facts.json в той же папке что и resume.md (т.е. учитывает активный профиль)."""
    home = os.path.dirname(config.RESUME_FILE) or os.path.expanduser("~/.job-hunter")
    return os.path.join(home, "facts.json")


# ---------- загрузка ----------

def load_facts() -> dict[str, Any]:
    path = facts_file_path()
    if not os.path.exists(path):
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return data
    except Exception as exc:
        log.warning("Failed to load facts %s: %s", path, exc)
    return {}


def format_facts_for_prompt(facts: dict[str, Any], limit_chars: int = 1500) -> str:
    """Превратить facts.json в bullet-список для вставки в LLM-промпт."""
    if not facts:
        return ""
    lines = ["Структурированные факты о кандидате (приоритет над свободным резюме):"]
    for key, value in facts.items():
        if value is None or value == "" or value == [] or value == {}:
            continue
        if isinstance(value, bool):
            value = "да" if value else "нет"
        elif isinstance(value, list):
            value = ", ".join(str(v) for v in value if v)
        elif isinstance(value, dict):
            value = json.dumps(value, ensure_ascii=False)
        lines.append(f"- {key}: {value}")
    block = "\n".join(lines) + "\n\n"
    if len(block) > limit_chars:
        block = block[:limit_chars - 1] + "…\n\n"
    return block


# ---------- извлечение через LLM ----------

_EXTRACT_PROMPT_TEMPLATE = """Извлеки из резюме структурированные факты о кандидате в JSON.

Используй ТОЛЬКО факты, которые явно есть в резюме. Ничего не выдумывай.
Если факт не упомянут или неоднозначен — НЕ включай это поле в JSON.

Рекомендованные поля (включай только те, что подтверждены резюме):
- "location": строка, город кандидата
- "willing_remote": true/false
- "willing_relocate": true/false
- "willing_business_trips": true/false
- "english_level": одно из "A1", "A2", "B1", "B2", "C1", "C2", или конкретная фраза из резюме
- "other_languages": список языков
- "experience_years": число (лет в основной профессии)
- "current_position": текущая должность
- "current_company": текущая компания
- "tools_used": список рабочих инструментов/технологий/фреймворков из резюме
- "tools_not_used": список инструментов, которые в резюме явно отсутствуют, но часто встречаются в вакансиях (если можно вывести)
- "programming_languages": список языков программирования из резюме
- "domains": список бизнес-доменов опыта (фин, EdTech, чаты, и т.п.)
- "education": уровень/специальность
- "ready_for_night_shifts": true/false (если упомянуто)
- "ready_for_office": true/false
- "summary": 1-2 предложения, как кандидат сам бы представился

Допустимы и любые другие поля, явно подтверждённые резюме.

Резюме:
---
{resume_text}
---

Верни ТОЛЬКО валидный JSON-объект без markdown и без пояснений. Первым символом ответа должен быть `{{`, последним `}}`."""


_client_singleton: AsyncOpenAI | None = None


def _get_llm_client() -> AsyncOpenAI:
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = AsyncOpenAI(
            base_url=config.LLM_BASE_URL,
            api_key=config.LLM_API_KEY or "no-key",
            http_client=proxy_utils.llm_http_client(),
        )
    return _client_singleton


from llm_utils import extract_first_json_object as _extract_first_json_object  # noqa: E402


async def extract_facts_from_resume(resume_text: str) -> dict[str, Any]:
    if not resume_text.strip():
        raise ValueError("empty resume_text")

    prompt = _EXTRACT_PROMPT_TEMPLATE.format(resume_text=resume_text[:8000])

    client = _get_llm_client()
    resp = await client.chat.completions.create(
        model=config.HH_FACTS_EXTRACT_MODEL or config.LLM_MODEL,
        messages=[
            {"role": "system", "content": "Ты отвечаешь строго в формате JSON. Не пиши никакого текста до или после JSON-объекта."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.1,
        max_tokens=1500,
    )
    raw = (resp.choices[0].message.content or "").strip()

    # очистка markdown fence
    if raw.startswith("```"):
        raw = raw.split("\n", 1)[-1].rsplit("```", 1)[0].strip()

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        extracted = _extract_first_json_object(raw)
        if not extracted:
            raise ValueError(f"LLM did not return valid JSON: {raw[:200]}")
        parsed = json.loads(extracted)

    if not isinstance(parsed, dict):
        raise ValueError(f"LLM returned non-object: {type(parsed).__name__}")
    return parsed


# ---------- CLI entry-point ----------

async def do_extract_facts() -> None:
    from hh_client import _load_resume_text  # late import to avoid circular dep
    resume_text = _load_resume_text()
    if not resume_text:
        print(f"❌ Резюме не найдено по пути {config.RESUME_FILE}")
        return
    print(f"📄 Резюме: {len(resume_text)} символов; модель: {config.LLM_MODEL}")
    print("🤖 Извлекаю факты через LLM…")
    try:
        facts = await extract_facts_from_resume(resume_text)
    except Exception as exc:
        print(f"❌ Ошибка: {exc}")
        return

    path = facts_file_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # бэкап существующего
    if os.path.exists(path):
        backup = f"{path}.bak.{int(os.path.getmtime(path))}"
        os.rename(path, backup)
        print(f"💾 Прежний facts.json → {os.path.basename(backup)}")

    with open(path, "w", encoding="utf-8") as f:
        json.dump(facts, f, ensure_ascii=False, indent=2)
    print(f"✅ Сохранено в {path}")
    print(f"📋 Полей: {len(facts)}")
    for k, v in facts.items():
        sample = json.dumps(v, ensure_ascii=False)
        if len(sample) > 80:
            sample = sample[:77] + "…"
        print(f"   {k}: {sample}")


if __name__ == "__main__":
    asyncio.run(do_extract_facts())
