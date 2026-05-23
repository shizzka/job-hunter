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
