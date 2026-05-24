"""Текстовые блоки, подкладываемые в LLM-промпты при автоответе на анкеты hh.ru.

Каждый блок включается опционально (если задан соответствующий env-knob /
доступен профильный файл фактов).
"""
from __future__ import annotations

import logging

import config

log = logging.getLogger("prompt_blocks")


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 1)].rstrip() + "…"


def build_salary_rule_block() -> str:
    """Блок-подсказка для LLM по расчёту зарплатных ожиданий под контекст вакансии."""
    baseline = (config.HH_AUTO_ANSWER_SALARY_BASELINE or "").strip()
    rule = (config.HH_AUTO_ANSWER_SALARY_RULE or "").strip()
    if not baseline and not rule:
        return ""
    lines = ["Зарплатные ожидания кандидата (учитывай при ответе на любой вопрос о ЗП):"]
    if baseline:
        lines.append(f"- Базовая планка: {baseline} ₽ на руки")
    if rule:
        lines.append(f"- Правило корректировки: {rule}")
    lines.append("- Для зарплатного вопроса посчитай число строго под ЭТУ вакансию, не бери цифры из резюме.")
    return "\n".join(lines) + "\n\n"


def build_facts_block() -> str:
    """Структурированные факты из profile/<name>/facts.json (если есть)."""
    try:
        import facts as facts_mod
        data = facts_mod.load_facts()
        return facts_mod.format_facts_for_prompt(data)
    except Exception as exc:
        log.debug("facts load failed: %s", exc)
        return ""


def build_profile_note_block() -> str:
    """Канонический профиль кандидата — приоритет над резюме (env HH_AUTO_ANSWER_PROFILE_NOTE)."""
    note = (config.HH_AUTO_ANSWER_PROFILE_NOTE or "").strip()
    if not note:
        return ""
    return f"⭐ КАНОНИЧЕСКИЙ ПРОФИЛЬ КАНДИДАТА (этот блок имеет приоритет над разделом «Резюме»):\n{note}\n\n"


def build_vacancy_context_block(vacancy_context: str, limit: int = 1500) -> str:
    """Контекст вакансии (title/company/description) для LLM."""
    if not vacancy_context:
        return ""
    return f"Контекст вакансии (на неё откликаемся):\n{_truncate(vacancy_context, limit)}\n\n"


def _knowledge_dir() -> str:
    """Папка knowledge/ рядом с resume.md (per-profile)."""
    import os
    import config
    home = os.path.dirname(config.RESUME_FILE) or os.path.expanduser("~/.job-hunter")
    return os.path.join(home, "knowledge")


def build_knowledge_base_block(limit_chars: int = 12000) -> str:
    """Подгрузить все .md/.txt из profile/<name>/knowledge/ и склеить как
    приоритетный блок «База знаний кандидата».

    Файлы сортируются по имени (alphabetically), склеиваются с заголовком
    «### <filename>». Общая длина обрезается до limit_chars (по умолчанию ~12 KB).
    """
    import os
    knowledge_dir = _knowledge_dir()
    if not os.path.isdir(knowledge_dir):
        return ""
    parts = []
    total = 0
    for fname in sorted(os.listdir(knowledge_dir)):
        if not (fname.endswith(".md") or fname.endswith(".txt")):
            continue
        path = os.path.join(knowledge_dir, fname)
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read().strip()
        except Exception as exc:
            log.debug("knowledge read %s failed: %s", fname, exc)
            continue
        if not content:
            continue
        chunk = f"### {fname}\n{content}\n"
        if total + len(chunk) > limit_chars:
            # обрезать chunk до оставшегося лимита
            remaining = limit_chars - total
            if remaining > 200:
                chunk = chunk[:remaining - 1] + "…\n"
                parts.append(chunk)
            break
        parts.append(chunk)
        total += len(chunk)
    if not parts:
        return ""
    block = (
        "📚 БАЗА ЗНАНИЙ КАНДИДАТА (приоритетный источник фактов, перекрывает резюме):\n\n"
        + "\n".join(parts)
        + "\n"
    )
    return block
