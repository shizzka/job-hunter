"""Быстрый keyword-фильтр вакансий (до LLM-оценки)."""

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

# Всё, связанное с войной, СВО, боевыми действиями, ВПК — жёсткий отсев
MILITARY_KEYWORDS = {
    "сво", "военн", "боевых действий", "боевые действия",
    "мобилизац", "оборонн", "военкомат", "впк",
    "вооружен", "армейск", "армии", "минобороны",
    "ракетн", "артиллер", "бронетехн", "военнослужащ",
    "контрактн", "контракт на службу", "служба по контракту",
    "оборонзаказ", "гособоронзаказ", "росгвард",
    "нацгвард", "ополчен", "добровольч", "фронт",
}


def check_vacancy(vacancy: dict) -> str | None:
    """
    Проверить вакансию на релевантность по ключевым словам.

    Возвращает:
        None — вакансия прошла фильтр (релевантна)
        str  — причина отсева (note для analytics)
    """
    title_lower = vacancy.get("title", "").lower()
    snippet_lower = vacancy.get("snippet", "").lower()
    combined = title_lower + " " + snippet_lower

    if any(ex in combined for ex in EXCLUDE_KEYWORDS):
        return "exclude_keywords"

    if any(kw in combined for kw in MILITARY_KEYWORDS):
        return "military_redflag"

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
