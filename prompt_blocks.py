"""Текстовые блоки, подкладываемые в LLM-промпты при автоответе на анкеты hh.ru.

Каждый блок включается опционально (если задан соответствующий env-knob /
доступен профильный файл фактов).
"""
from __future__ import annotations

import logging
import os

import config

log = logging.getLogger("prompt_blocks")


def _read_profile_env_values() -> dict[str, str]:
    path = os.path.join(str(getattr(config, "JOB_HUNTER_HOME", "") or ""), "profile.env")
    if not path or not os.path.isfile(path):
        return {}
    values: dict[str, str] = {}
    try:
        with open(path, encoding="utf-8") as f:
            for raw_line in f:
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip(chr(39) + chr(34))
    except Exception as exc:
        log.debug("profile env contacts read failed: %s", exc)
    return values


def _first_nonempty(*values: str) -> str:
    for value in values:
        value = str(value or "").strip()
        if value:
            return value
    return ""


def get_candidate_contacts() -> dict[str, str]:
    profile_env = _read_profile_env_values()
    telegram = _first_nonempty(
        os.getenv("CANDIDATE_TELEGRAM"),
        os.getenv("CONTACT_TELEGRAM"),
        profile_env.get("CANDIDATE_TELEGRAM"),
        profile_env.get("CONTACT_TELEGRAM"),
    )
    resume_url = _first_nonempty(
        os.getenv("CANDIDATE_RESUME_URL"),
        os.getenv("CONTACT_RESUME_URL"),
        profile_env.get("CANDIDATE_RESUME_URL"),
        profile_env.get("CONTACT_RESUME_URL"),
    )
    if not resume_url:
        resume_id = str(getattr(config, "HH_PRIMARY_RESUME_ID", "") or "").strip()
        if resume_id:
            resume_url = f"https://hh.ru/resume/{resume_id}"
    return {"telegram": telegram, "resume_url": resume_url}


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
    lines.append(
        "- В ответе назови ориентир и добавь, что конечные ожидания зависят "
        "от загрузки, зоны ответственности, задач и формата работы."
    )
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

def build_contact_block() -> str:
    """Контакты кандидата для форм, где HR явно просит Telegram или ссылку на резюме."""
    contacts = get_candidate_contacts()
    telegram = contacts.get("telegram", "")
    resume_url = contacts.get("resume_url", "")
    lines = []
    if telegram:
        lines.append(f"- Telegram для связи: {telegram}")
    if resume_url:
        lines.append(f"- Ссылка на резюме: {resume_url}")
    if not lines:
        return ""
    return (
        "Контактные данные кандидата "
        "(используй только когда форма прямо просит контакты/ссылку на резюме):\n"
        + "\n".join(lines)
        + "\n\n"
    )


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


_SECTION_HEADER_RE = None


def _parse_kb_sections(md_text: str) -> list[dict]:
    """Разбить markdown по '## NN. Title' → list of {num, title, content}.
    Игнорирует preface до первого '## NN.'.
    """
    import re
    global _SECTION_HEADER_RE
    if _SECTION_HEADER_RE is None:
        _SECTION_HEADER_RE = re.compile(r"^## (\d+)\.\s+(.+?)\s*$", re.MULTILINE)
    matches = list(_SECTION_HEADER_RE.finditer(md_text))
    if not matches:
        return []
    out = []
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md_text)
        content = md_text[start:end].strip()
        out.append({
            "num": int(m.group(1)),
            "title": m.group(2).strip(),
            "content": content,
        })
    return out


def _load_kb_filterable() -> tuple[str, list[dict]]:
    """Возвращает (about_me_text, qa_kb_sections).
    about_me.md грузим целиком как «общий блок».
    qa_kb.md парсим на секции и возвращаем — внешний код их фильтрует.
    Прочие .md/.txt из knowledge/ возвращаются в about_me_text как single block."""
    import os
    knowledge_dir = _knowledge_dir()
    about_parts = []
    qa_sections: list[dict] = []
    if not os.path.isdir(knowledge_dir):
        return "", []
    for fname in sorted(os.listdir(knowledge_dir)):
        path = os.path.join(knowledge_dir, fname)
        if not os.path.isfile(path):
            continue
        if not (fname.endswith(".md") or fname.endswith(".txt")):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                txt = f.read().strip()
        except Exception:
            continue
        if not txt:
            continue
        if fname.lower().startswith("qa_kb") or fname.lower().startswith("kb_"):
            qa_sections = _parse_kb_sections(txt)
        else:
            about_parts.append(f"### {fname}\n{txt}")
    return ("\n\n".join(about_parts), qa_sections)


def _format_kb_block(about_text: str, sections: list[dict], limit_chars: int = 10000) -> str:
    """Собрать финальный блок из about_text + перечня секций."""
    parts = ["📚 БАЗА ЗНАНИЙ КАНДИДАТА (приоритетный источник фактов, перекрывает резюме):"]
    if about_text:
        parts.append(about_text)
    for s in sections:
        parts.append(f"### {s['num']}. {s['title']}\n{s['content']}")
    block = "\n\n".join(parts) + "\n"
    if len(block) > limit_chars:
        block = block[:limit_chars - 1] + "…\n"
    return block


async def select_kb_sections(
    vacancy_context: str,
    sections: list[dict],
    llm_client,
    max_sections: int = 5,
    model: str | None = None,
) -> list[int]:
    """Первый из 2-pass LLM: спросить какие из секций релевантны вакансии.
    Возвращает список section_num (int).
    """
    if not sections or not vacancy_context:
        return []
    import config
    from llm_utils import parse_llm_json
    titles_block = "\n".join(f"{s['num']}. {s['title']}" for s in sections)
    selector_model = (model or "").strip() or config.LLM_MODEL
    prompt = f"""Из списка секций базы знаний QA-кандидата выбери {max_sections} наиболее релевантных для конкретной вакансии. Релевантные — те которые помогут написать качественный ответ работодателю/cover letter.

Контекст вакансии:
{vacancy_context[:1500]}

Секции базы знаний:
{titles_block}

Верни ТОЛЬКО валидный JSON. Первый символ `{{`, последний `}}`. Формат:
{{"selected": [<номер_секции>, <номер_секции>, ...]}}

Правила:
- Выбирай только из приведённых номеров.
- Максимум {max_sections} секций.
- Если в вакансии явно про API — обязательно секция «API-тестирование».
- Если про SQL — обязательно секция «SQL».
- Секции про инженерный/maker-бэкграунд (электрика, 3D-печать) — только если в вакансии есть hint на технический бэкграунд."""
    try:
        resp = await llm_client.chat.completions.create(
            model=selector_model,
            messages=[
                {"role": "system", "content": "Ты отвечаешь строго JSON. Никакого текста до или после."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.1,
            max_tokens=200,
        )
        raw = (resp.choices[0].message.content or "").strip()
        parsed = parse_llm_json(raw)
    except Exception as exc:
        log.warning("kb section selection failed: %s", exc)
        return []
    sel = parsed.get("selected") or []
    if not isinstance(sel, list):
        return []
    out = []
    for x in sel:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return out[:max_sections]


async def build_filtered_kb_block(
    vacancy_context: str,
    llm_client,
    max_sections: int = 5,
    limit_chars: int = 10000,
    selector_model: str | None = None,
) -> str:
    """2-pass: парсим KB → выбираем релевантные секции через LLM → формируем блок.
    Если LLM-фильтр не сработал или vacancy_context пуст — fallback на полный
    (обрезанный) блок через build_knowledge_base_block."""
    about_text, sections = _load_kb_filterable()
    if not about_text and not sections:
        return ""
    if not sections:
        return _format_kb_block(about_text, [], limit_chars=limit_chars)
    if not vacancy_context:
        # без контекста берём первые 5 секций (основное позиционирование)
        return _format_kb_block(about_text, sections[:5], limit_chars=limit_chars)
    selected_nums = await select_kb_sections(
        vacancy_context, sections, llm_client, max_sections=max_sections, model=selector_model
    )
    if not selected_nums:
        # fallback на старое поведение
        return build_knowledge_base_block(limit_chars=limit_chars)
    by_num = {s["num"]: s for s in sections}
    picked = [by_num[n] for n in selected_nums if n in by_num]
    if not picked:
        return build_knowledge_base_block(limit_chars=limit_chars)
    log.info("KB filter picked sections: %s", [f"{s['num']}.{s['title'][:30]}" for s in picked])
    return _format_kb_block(about_text, picked, limit_chars=limit_chars)


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
