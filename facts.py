"""Структурированные факты кандидата (facts.json) — для авто-ответов на анкеты.

LLM extraction из resume.md сохраняется как unconfirmed с provenance.
Только явно подтверждённые кандидатом сведения могут служить evidence;
extraction и legacy flat facts остаются подсказками для проверки.

Поля — гибкие, но есть рекомендованный набор (location, willing_remote,
willing_business_trips, english_level, tools_used, и т.п.). LLM сам решает структуру
при extract — главное чтобы итог парсился как JSON.
"""
from __future__ import annotations

import asyncio
import json
import hashlib
import logging
import os
import re
import time
from typing import Any

import config
from llm_client import get_llm_client
from state_store.json_store import JsonStore, atomic_write_json

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


def _is_empty_fact(value: Any) -> bool:
    return value is None or value == "" or value == [] or value == {}


def _format_fact_value(value: Any) -> str:
    if isinstance(value, bool):
        return "да" if value else "нет"
    if isinstance(value, list):
        return ", ".join(str(v) for v in value if not _is_empty_fact(v))
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def _append_fact_section(lines: list[str], title: str, value: Any) -> None:
    if _is_empty_fact(value):
        return
    lines.append(title)
    if isinstance(value, dict):
        for key, item in value.items():
            if _is_empty_fact(item):
                continue
            lines.append(f"- {key}: {_format_fact_value(item)}")
        return
    if isinstance(value, list):
        for item in value:
            if _is_empty_fact(item):
                continue
            lines.append(f"- {_format_fact_value(item)}")
        return
    lines.append(f"- {_format_fact_value(value)}")


def format_facts_for_prompt(facts: dict[str, Any], limit_chars: int = 3000) -> str:
    """Превратить facts.json в prompt-блок с явными claim guardrails.

    Поддерживает новую схему:
    - confirmed: подтвержденные факты, можно утверждать прямо;
    - inferred: аккуратные выводы из опыта;
    - weak: слабые/ограниченные факты, только мягкие формулировки;
    - do_not_claim / forbidden_claims: запреты, нельзя писать как факт;
    - allowed_wording: безопасные формулировки.

    Старые плоские facts.json остаются видимыми, но требуют подтверждения.
    """
    if not facts:
        return ""

    known_sections = {
        "confirmed",
        "inferred",
        "weak",
        "do_not_claim",
        "forbidden_claims",
        "allowed_wording",
        "unconfirmed",
        "_provenance",
    }
    has_structured_sections = any(key in facts for key in known_sections)
    lines = [
        "Структурированные сведения о кандидате:",
        "Правило: не расширяй эти факты и не превращай слабые факты в уверенные claims.",
    ]

    if has_structured_sections:
        _append_fact_section(lines, "CONFIRMED — можно утверждать прямо:", facts.get("confirmed"))
        _append_fact_section(lines, "UNCONFIRMED — нужна проверка кандидатом; не использовать как доказательство:", facts.get("unconfirmed"))
        _append_fact_section(lines, "INFERRED — можно использовать аккуратно, без усиления:", facts.get("inferred"))
        _append_fact_section(lines, "WEAK / LIMITED — только мягкие формулировки:", facts.get("weak"))
        _append_fact_section(lines, "ALLOWED WORDING — безопасные формулировки:", facts.get("allowed_wording"))
        forbidden = facts.get("do_not_claim") or facts.get("forbidden_claims")
        _append_fact_section(lines, "DO NOT CLAIM — запрещено писать или подразумевать:", forbidden)

        extra = {key: value for key, value in facts.items() if key not in known_sections and not _is_empty_fact(value)}
        _append_fact_section(lines, "OTHER FACTS:", extra)
    else:
        lines.append("LEGACY UNCONFIRMED — происхождение не подтверждено; не использовать как доказательство:")
        for key, value in facts.items():
            if _is_empty_fact(value):
                continue
            lines.append(f"- {key}: {_format_fact_value(value)}")

    block = "\n".join(lines) + "\n\n"
    if len(block) > limit_chars:
        block = block[:limit_chars - 1] + "…\n\n"
    return block


def confirmed_facts_for_prompt(value=None) -> str:
    """Only explicit confirmation can prove a personal claim; legacy is unknown."""
    value = load_facts() if value is None else value
    confirmed = value.get('confirmed') if isinstance(value, dict) else None
    if not isinstance(confirmed, dict) or not confirmed:
        return ''
    return format_facts_for_prompt({'confirmed': confirmed})


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


_client_singleton = None


def _get_llm_client():
    global _client_singleton
    if _client_singleton is None:
        _client_singleton = get_llm_client()
    return _client_singleton


from llm_utils import extract_first_json_object as _extract_first_json_object  # noqa: E402


async def extract_facts_from_resume(resume_text: str) -> dict[str, Any]:
    if not resume_text.strip():
        raise ValueError("empty resume_text")

    prompt = _EXTRACT_PROMPT_TEMPLATE.format(resume_text=resume_text[:8000])

    client = _get_llm_client()
    model = config.HH_FACTS_EXTRACT_MODEL or config.LLM_MODEL
    resp = await client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "Ты отвечаешь строго в формате JSON. Не пиши никакого текста до или после JSON-объекта."},
            {"role": "user", "content": prompt},
        ],
        temperature=0.1,
        max_tokens=1500,
    )
    if resp.choices[0].finish_reason != "stop":
        raise ValueError("Incomplete facts extraction")
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
    return {'unconfirmed': parsed, '_provenance': {
        'kind': 'model_extraction', 'scope': 'unconfirmed',
        'resume_sha256': hashlib.sha256(resume_text.encode()).hexdigest(),
        'source_chars': min(len(resume_text), 8000), 'model': model, 'extracted_at': int(time.time()),
    }}


def save_facts(value: dict[str, Any], *, path: str | None = None) -> str | None:
    """Replace candidate facts atomically, retaining the previous valid version."""
    if not isinstance(value, dict):
        raise TypeError("Candidate facts must be a JSON object")
    path = path or facts_file_path()
    backup = None

    def replace(previous):
        nonlocal backup
        if os.path.exists(path):
            backup = f"{path}.bak.{time.time_ns()}"
            atomic_write_json(backup, previous)
        if isinstance(value.get('_provenance'), dict) and value['_provenance'].get('kind') == 'model_extraction':
            # Extraction must not erase confirmed user input or existing bans.
            retained = {key: previous[key] for key in ('confirmed', 'do_not_claim', 'forbidden_claims')
                        if key in previous}
            return {**retained, **value}
        return value

    JsonStore(path, logger=log).update(replace)
    return backup


# ---------- CLI entry-point ----------

async def do_extract_facts() -> None:
    from hh_client import _load_resume_text  # late import to avoid circular dep
    resume_text = _load_resume_text()
    if not resume_text:
        print(f"❌ Резюме не найдено по пути {config.RESUME_FILE}")
        return
    print(f"📄 Резюме: {len(resume_text)} символов; модель: {config.LLM_MODEL}")
    print("🤖 Извлекаю факты через LLM…")
    path = facts_file_path()
    try:
        facts = await extract_facts_from_resume(resume_text)
    except Exception as exc:
        print(f"❌ Ошибка: {exc}")
        return

    backup = save_facts(facts, path=path)
    if backup:
        print(f"💾 Прежний facts.json → {os.path.basename(backup)}")
    print(f"✅ Сохранено в {path}; extraction требует подтверждения кандидатом")
    print(f"📋 Полей: {len(facts)}")
    for k, v in facts.items():
        sample = json.dumps(v, ensure_ascii=False)
        if len(sample) > 80:
            sample = sample[:77] + "…"
        print(f"   {k}: {sample}")


if __name__ == "__main__":
    asyncio.run(do_extract_facts())
