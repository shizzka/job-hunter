"""Быстрый keyword-фильтр вакансий (до LLM-оценки)."""

import re
import config


def uses_qa_policy() -> bool:
    policy = getattr(config, "VACANCY_FILTER_POLICY", "qa")
    if policy not in {"qa", "generic"}:
        raise ValueError("VACANCY_FILTER_POLICY must be qa or generic")
    return policy == "qa"

RELEVANT_KEYWORDS = {
    "тестиров", "qa", "quality", "тест ", "test",
    "автоматиз", "ручн", "manual", "sdet",
}

SUPERJOB_TITLE_KEYWORDS = {
    "тест",
    "qa",
    "quality engineer",
    "quality assurance",
}

SUPERJOB_QUALITY_TITLE_KEYWORDS = {"качеств"}

SUPERJOB_IT_CONTEXT_KEYWORDS = {
    "программ",
    "software",
    "qa",
    "тест",
    "api",
    "web",
    "веб",
    "прилож",
    "frontend",
    "backend",
    "mobile",
    "автоматиз",
    "manual",
    "selenium",
    "postman",
    "sql",
}

EXCLUDE_KEYWORDS = {
    "директор магазин", "продавец", "кассир", "менеджер по продажам",
    "бухгалтер", "повар", "водитель", "курьер", "охранник",
    "уборщ", "грузчик", "кладовщик",
}

# Короткие маркеры и неоднозначные корни проверяются только в безопасном
# контексте, чтобы «свой продукт», frontend и «контрактная форма» не
# считались военными вакансиями.
MILITARY_PATTERNS = tuple(
    re.compile(pattern, re.I)
    for pattern in (
        r"(?<!\w)сво(?!\w)",
        r"\bвоенн\w*",
        r"\bбоев\w*\s+действ\w*",
        r"\bмобилизац\w*",
        r"\bоборонн\w*",
        r"\bвоенкомат\w*",
        r"(?<!\w)впк(?!\w)",
        r"\bвооруж(?:ение|ений|ённ\w*\s+сил|енн\w*\s+сил)",
        r"\bармейск\w*",
        r"\bармии\b",
        r"\bминобороны\b",
        r"\bракетн\w*",
        r"\bартиллер\w*",
        r"\bбронетехн\w*",
        r"\bвоеннослужащ\w*",
        r"\bконтракт\s+на\s+службу\b",
        r"\bслужба\s+по\s+контракту\b",
        r"\b(?:гос)?оборонзаказ\w*",
        r"\bросгвард\w*",
        r"\bнацгвард\w*",
        r"\bополчен\w*",
        r"\bдобровольч\w*",
        r"\bфронт(?![-–—]?(?:енд|офис))(?:а|е|у|ом|ы|ов|овой|овая|овые|овых)?\b",
    )
)


def check_vacancy(vacancy: dict) -> str | None:
    """
    Проверить вакансию на релевантность по ключевым словам.

    Возвращает:
        None — вакансия прошла фильтр (релевантна)
        str  — причина отсева (note для analytics)
    """
    qa_policy = uses_qa_policy()
    title_lower = str(vacancy.get("title") or "").casefold()
    snippet_lower = str(vacancy.get("snippet") or "").casefold()
    combined = title_lower + " " + snippet_lower

    excludes = config.VACANCY_EXCLUDE_KEYWORDS or (EXCLUDE_KEYWORDS if qa_policy else ())
    if any(ex.casefold() in combined for ex in excludes):
        return "exclude_keywords"

    if any(pattern.search(combined) for pattern in MILITARY_PATTERNS):
        return "military_redflag"

    keywords = config.VACANCY_RELEVANT_KEYWORDS
    if keywords:
        return None if any(kw.casefold() in combined for kw in keywords) else "relevant_keywords"
    if not qa_policy:
        return None

    source = vacancy.get("source", "")

    if source == "superjob":
        if any(kw in title_lower for kw in SUPERJOB_TITLE_KEYWORDS):
            return None
        if (
            any(kw in title_lower for kw in SUPERJOB_QUALITY_TITLE_KEYWORDS)
            and any(kw in combined for kw in SUPERJOB_IT_CONTEXT_KEYWORDS)
        ):
            return None
        return "superjob_title_filter"

    if any(kw in combined for kw in RELEVANT_KEYWORDS):
        return None

    return "relevant_keywords"
