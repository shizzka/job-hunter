"""Быстрый keyword-фильтр вакансий (до LLM-оценки)."""

import re
import config


def uses_qa_policy(settings=None) -> bool:
    policy = getattr(settings, "VACANCY_FILTER_POLICY", getattr(config, "VACANCY_FILTER_POLICY", "qa"))
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


_WORD_RE = re.compile(r"[a-zа-я0-9+#]+")
# Role words that say nothing about the field: «специалист по закупкам» ≠ «специалист по данным».
_GENERIC_ROLE_WORDS = ("специалист", "менеджер", "руководитель", "ведущий", "старший", "главный", "сотрудник",
                       "группа", "отдел", "начальник", "помощник", "ассистент", "заместитель", "работа",
                       "направление", "координатор", "младший", "senior", "junior", "middle", "lead")


def _words(text: str) -> list[str]:
    return [w for w in _WORD_RE.findall(str(text or "").casefold().replace("ё", "е")) if len(w) >= 3]


def _same_word(a: str, b: str) -> bool:
    """Same word up to a Russian ending: «контроль/контроля/контролер», «данных/данные», not «контракт»."""
    if a == b:
        return True
    if min(len(a), len(b)) < 4:
        return False
    common = 0
    for x, y in zip(a, b):
        if x != y:
            break
        common += 1
    return common >= max(4, min(len(a), len(b)) - 2)


def _meaningful(words: list[str]) -> list[str]:
    return [w for w in words if not any(_same_word(w, g) for g in _GENERIC_ROLE_WORDS)]


def _soft_match(title: str, combined: str, keywords) -> bool:
    """A keyword phrase matches in any word form/order; for a direction, a title matches its target
    roles by a meaningful word («Оператор базы данных» ~ «Оператор обработки данных»)."""
    text_words = _words(combined)
    for phrase in keywords:
        wanted = _meaningful(_words(phrase))
        if wanted and all(any(_same_word(w, t) for t in text_words) for w in wanted):
            return True
    direction = getattr(config, "ACTIVE_DIRECTION", None) or {}
    roles = [str(r) for key in ("goal", "allowed_wording") for r in (direction.get(key) or [])]
    role_words = set(_meaningful([w for role in roles for w in _words(role)]))
    title_words = _meaningful(_words(title))
    return any(_same_word(r, t) for r in role_words for t in title_words)


def _excluded(keyword: str, combined: str) -> bool:
    """Match adjacent whole words, allowing Russian suffixes on long words.

    Hyphens delimit words but stay literal when included in an exclusion.
    Existing stems (e.g. «уборщ») and phrases need no list migration.
    """
    keyword = str(keyword or "").casefold().strip()
    if not keyword:
        return False
    parts = []
    for token in re.findall(r"\w+|\s+|[^\w\s]+", keyword):
        if token.isspace():
            parts.append(r"\s+")
        else:
            suffix = r"[а-яё]*" if re.fullmatch(r"[а-яё]{4,}", token) else ""
            parts.append(re.escape(token) + suffix)
    return re.search(r"(?<!\w)" + "".join(parts) + r"(?!\w)", combined) is not None


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
    if any(_excluded(ex, combined) for ex in excludes):
        return "exclude_keywords"

    if any(pattern.search(combined) for pattern in MILITARY_PATTERNS):
        return "military_redflag"

    keywords = config.VACANCY_RELEVANT_KEYWORDS
    if keywords:
        if any(kw.casefold() in combined for kw in keywords) or _soft_match(title_lower, combined, keywords):
            return None
        return "relevant_keywords"
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
