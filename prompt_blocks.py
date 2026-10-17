"""Текстовые блоки, подкладываемые в LLM-промпты при автоответе на анкеты hh.ru.

Каждый блок включается опционально (если задан соответствующий env-knob /
доступен профильный файл фактов).
"""
from __future__ import annotations

import logging
import os

import config
import filters
from direction_runtime import candidate_home

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

    def inherited(key: str) -> str:
        if getattr(config, "CANDIDATE_PROFILE_ISOLATED", False):
            return ""
        return os.getenv(key, "")

    email = _first_nonempty(
        profile_env.get("CANDIDATE_EMAIL"),
        profile_env.get("CONTACT_EMAIL"),
        profile_env.get("HH_AUTH_EMAIL"),
        inherited("CANDIDATE_EMAIL"),
        inherited("CONTACT_EMAIL"),
        inherited("HH_AUTH_EMAIL"),
    )
    phone = _first_nonempty(
        profile_env.get("CANDIDATE_PHONE"),
        profile_env.get("CONTACT_PHONE"),
        profile_env.get("HH_AUTH_PHONE"),
        inherited("CANDIDATE_PHONE"),
        inherited("CONTACT_PHONE"),
        inherited("HH_AUTH_PHONE"),
    )
    telegram = _first_nonempty(
        profile_env.get("CANDIDATE_TELEGRAM"),
        profile_env.get("CONTACT_TELEGRAM"),
        inherited("CANDIDATE_TELEGRAM"),
        inherited("CONTACT_TELEGRAM"),
    )
    resume_url = _first_nonempty(
        profile_env.get("CANDIDATE_RESUME_URL"),
        profile_env.get("CONTACT_RESUME_URL"),
        inherited("CANDIDATE_RESUME_URL"),
        inherited("CONTACT_RESUME_URL"),
    )
    if not resume_url:
        resume_id = str(getattr(config, "HH_PRIMARY_RESUME_ID", "") or "").strip()
        if resume_id:
            resume_url = f"https://hh.ru/resume/{resume_id}"
    return {"email": email, "phone": phone, "telegram": telegram, "resume_url": resume_url}


def _truncate(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
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
    """Структурированные и подтверждённые пользователем факты профиля."""
    blocks = []
    try:
        import facts as facts_mod
        block = facts_mod.format_facts_for_prompt(facts_mod.load_facts())
        if block:
            blocks.append(block)
    except Exception as exc:
        log.debug("facts load failed: %s", exc)
    try:
        import candidate_interview
        block = candidate_interview.prompt_block()
        if block:
            blocks.append(block)
    except Exception as exc:
        log.debug("candidate interview facts load failed: %s", exc)
    return "".join(blocks)


def build_cover_facts_blocks() -> tuple[str, str]:
    """One confirmed snapshot for author/checker, plus unabridged restrictions.

    Covers use confirmed data only. Structured facts and explicit interview
    boundaries stay whole; other interview entries share a bounded budget.
    """
    import candidate_interview
    import facts
    personal = facts.load_facts()
    evidence = facts.confirmed_facts_for_prompt(personal, limit_chars=None)
    interview, limits = candidate_interview.cover_prompt_blocks()
    return evidence + interview, facts.format_fact_constraints(personal) + limits


def build_profile_note_block() -> str:
    """Read candidate positioning from the current profile's knowledge directory."""
    path = os.path.join(_knowledge_dir(), "profile_note.md")
    try:
        with open(path, encoding="utf-8") as stream:
            note = stream.read().strip()
    except FileNotFoundError:
        note = ""
    except OSError as exc:
        log.warning("profile note read failed: %s", type(exc).__name__)
        return ""
    if note:
        return f"⭐ КАНОНИЧЕСКИЙ ПРОФИЛЬ КАНДИДАТА (данные текущего профиля):\n{note}\n\n"
    note = (config.HH_AUTO_ANSWER_PROFILE_NOTE or "").strip()
    if not note:
        return ""
    return f"⭐ КАНОНИЧЕСКИЙ ПРОФИЛЬ КАНДИДАТА (этот блок имеет приоритет над разделом «Резюме»):\n{note}\n\n"


def build_contact_block() -> str:
    """Контакты кандидата для форм, где HR явно просит контакты или ссылку на резюме."""
    contacts = get_candidate_contacts()
    email = contacts.get("email", "")
    phone = contacts.get("phone", "")
    telegram = contacts.get("telegram", "")
    resume_url = contacts.get("resume_url", "")
    lines = []
    if email:
        lines.append(f"- Email для связи: {email}")
    if phone:
        lines.append(f"- Телефон для связи: {phone}")
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


def _knowledge_dir(profile_dir: str | None = None) -> str:
    """Папка knowledge/ профиля, общая для его направлений."""
    import os
    import config
    home = profile_dir or candidate_home()
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
    reserved = {int(match.group(1)) for match in matches}
    used: set[int] = set()
    next_num = max(reserved) + 1
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(md_text)
        content = md_text[start:end].strip()
        num = int(m.group(1))
        if num in used:
            log.warning("KB duplicate section number: original=%d assigned=%d", num, next_num)
            num = next_num
            next_num += 1
        used.add(num)
        out.append({
            "num": num,
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
    preferred = [
        "answering_rules.md", "constraints.md", "candidate_facts.md",
        "projects.md", "about_me.md", "qa_kb.md",
    ]
    names = [name for name in preferred if os.path.isfile(os.path.join(knowledge_dir, name))]
    names.extend(name for name in sorted(os.listdir(knowledge_dir)) if name not in names)
    has_canonical_facts = "candidate_facts.md" in names
    for fname in names:
        if fname in {"kb_eval_questions.txt", "synonyms.txt"}:
            continue
        if fname == "profile_note.md":
            continue  # included separately by build_profile_note_block()
        if has_canonical_facts and fname == "about_me.md":
            continue
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


def _format_kb_block(
    about_text: str, sections: list[dict], limit_chars: int | None = 10000,
    *, preserve_sections: bool = False,
) -> str:
    """Reserve selected sections first; common files fill the remaining budget.

    Explicit selections stay whole even when they alone exceed the budget.
    Unfiltered fallbacks remain bounded and report any partial/dropped sections.
    """
    parts = ["📚 БАЗА ЗНАНИЙ КАНДИДАТА (приоритетный источник фактов, перекрывает резюме):"]
    if about_text and not preserve_sections:
        parts.append(about_text)
    spans = []
    offset = len("\n\n".join(parts))
    for s in sections:
        part = f"### {s['num']}. {s['title']}\n{s['content']}"
        start = offset + 2
        offset = start + len(part)
        spans.append((s["num"], start, offset))
        parts.append(part)
    selected_text = "\n\n".join(parts)
    original = selected_text + ("\n\n" + about_text if about_text and preserve_sections else "") + "\n"
    block = original
    if limit_chars is not None:
        if preserve_sections:
            remaining = max(0, limit_chars - len(selected_text) - 3)
            common = _truncate(about_text, remaining)
            block = selected_text + ("\n\n" + common if common else "") + "\n"
        else:
            block = _truncate(original, limit_chars)
    original_chars = len(original)
    truncated = len(block) < original_chars
    over_budget = limit_chars is not None and len(block) > limit_chars
    content_end = len(block) - (1 if truncated and not preserve_sections else 0)
    full = [num for num, start, end in spans if end <= content_end]
    partial = [num for num, start, end in spans if start < content_end < end]
    dropped = len(spans) - len(full) - len(partial)
    emit = log.warning if truncated or over_budget else log.info
    emit("KB prompt: truncated=%s over_budget=%s chars_limit=%s chars_original=%d chars_included=%d chars_removed=%d "
         "sections_requested=%d sections_full=%s sections_partial=%s sections_dropped=%d",
         str(truncated).lower(), str(over_budget).lower(), limit_chars, original_chars, len(block), max(0, original_chars - len(block)),
         len(sections), full, partial, dropped)
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
    qa_policy = filters.uses_qa_policy()
    candidate_label = "QA-кандидата" if qa_policy else "кандидата"
    direction_rules = ("""- Если в вакансии явно про API — обязательно секция «API-тестирование».
- Если про SQL — обязательно секция «SQL».
- Секции про инженерный/maker-бэкграунд (электрика, 3D-печать) — только если в вакансии есть hint на технический бэкграунд.""" if qa_policy else """- Выбирай секции по совпадению задач и обязательных требований вакансии с подтвержденным опытом кандидата.
- Опыт, инструменты, оборудование, образование и допуски учитывай только из данных кандидата, без домыслов.
- Секции других профессий не выбирай только из-за общего технического бэкграунда.""")
    prompt = f"""Из списка секций базы знаний {candidate_label} выбери {max_sections} наиболее релевантных для конкретной вакансии. Релевантные — те которые помогут написать качественный ответ работодателю/cover letter.

Контекст вакансии:
{vacancy_context}

Секции базы знаний:
{titles_block}

Верни ТОЛЬКО валидный JSON. Первый символ `{{`, последний `}}`. Формат:
{{"selected": [<номер_секции>, <номер_секции>, ...]}}

Правила:
- Выбирай только из приведённых номеров.
- Максимум {max_sections} секций.
{direction_rules}"""
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
        log.info("kb section selection skipped, using unfiltered knowledge base fallback: %s", exc)
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
    limit_chars: int | None = 10000,
    selector_model: str | None = None,
) -> str:
    """Retrieve locally by default; flag-off preserves the legacy LLM selector.

    Legacy fallback uses the truncated snapshot read before its model await.
    Retrieval errors propagate so no mandatory boundaries can be lost.
    """
    from kb_retrieval import retrieval_enabled, retrieve_knowledge
    if retrieval_enabled():
        return retrieve_knowledge(
            vacancy_context, profile_dir=candidate_home(),
            limit_chars=limit_chars if limit_chars is not None else 10000,
        )
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
        return _format_kb_block(about_text, sections, limit_chars=limit_chars)
    by_num = {s["num"]: s for s in sections}
    picked = [by_num[n] for n in dict.fromkeys(selected_nums) if n in by_num]
    if not picked:
        return _format_kb_block(about_text, sections, limit_chars=limit_chars)
    return _format_kb_block(about_text, picked, limit_chars=limit_chars, preserve_sections=True)


def build_knowledge_base_block(limit_chars: int = 12000, *, profile_dir: str | None = None) -> str:
    """Подгрузить все .md/.txt из profile/<name>/knowledge/ и склеить как
    приоритетный блок «База знаний кандидата».

    Файлы сортируются по имени (alphabetically), склеиваются с заголовком
    «### <filename>». Общая длина обрезается до limit_chars (по умолчанию ~12 KB).
    """
    import os
    knowledge_dir = _knowledge_dir(profile_dir)
    if not os.path.isdir(knowledge_dir):
        return ""
    parts = []
    preferred = [
        "answering_rules.md", "constraints.md", "candidate_facts.md",
        "projects.md", "about_me.md", "qa_kb.md",
    ]
    names = [name for name in preferred if os.path.isfile(os.path.join(knowledge_dir, name))]
    names.extend(name for name in sorted(os.listdir(knowledge_dir)) if name not in names)
    for fname in names:
        if fname in {"kb_eval_questions.txt", "synonyms.txt"}:
            continue  # Retrieval metadata is neither facts nor prompt material.
        if fname == "profile_note.md":
            continue  # included separately by build_profile_note_block()
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
        parts.append(chunk)
    if not parts:
        return ""
    block = (
        "📚 БАЗА ЗНАНИЙ КАНДИДАТА (приоритетный источник фактов, перекрывает резюме):\n\n"
        + "\n".join(parts)
        + "\n"
    )
    return _truncate(block, limit_chars)
